"""Intraday fixes: the replay's fill bar and R, volume ratios on other hours, incomplete sessions,
special-session hours, the dividend-adjusted daily close, plans made late, and the report's exit-by.

Synthetic bars and a stubbed Yahoo at pinned moments: nothing reaches the network or depends on today.
"""

from dataclasses import replace
from datetime import date, datetime, time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from tradingagents.agents.intraday_plan import plan_from_fields
from tradingagents.dataflows.vendors.yahoo import intraday as yahoo_intraday
from tradingagents.markets.exchanges import NSE
from tradingagents.markets.intraday import format_intraday_snapshot, intraday_levels, target_session
from tradingagents.markets.intraday_eval import IntradayPlan, evaluate_plan
from tradingagents.memory.intraday_settlement import stored_plan, summarize
from tradingagents.memory.log import describe_outcome
from tradingagents.reporting import horizon_line, write_report_tree

IST = ZoneInfo("Asia/Kolkata")
SESSION = date(2026, 10, 30)


def _bars(rows, day: date = SESSION) -> pd.DataFrame:
    """Bars from (HH:MM, open, high, low, close) rows."""
    return pd.DataFrame([{"Datetime": pd.Timestamp(f"{day} {hhmm}", tz=IST), "Open": o, "High": h, "Low": low,
                          "Close": c, "Volume": 1000} for hhmm, o, h, low, c in rows])


def _plan(direction="long", entry=101.0, stop=99.5, target=104.0, exit_by=time(15, 10), shares=100,
          reference=100.0, valid_from=None) -> IntradayPlan:
    return IntradayPlan(direction, entry, stop, target, exit_by, shares, SESSION, reference, valid_from)


def _ist(day, clock: str) -> datetime:
    return pd.Timestamp(f"{day} {clock}", tz=IST).to_pydatetime()


def _flat(day: str, start: str, end: str, volume: int = 1000) -> pd.DataFrame:
    """Flat 5m bars from ``start`` to ``end`` (exclusive), ``volume`` shares each."""
    index = pd.date_range(f"{day} {start}", f"{day} {end}", freq="5min", inclusive="left", tz=IST)
    return pd.DataFrame({"Datetime": index, "Open": 100.0, "High": 100.5, "Low": 99.5, "Close": 100.0,
                         "Volume": volume})


def _session(day: str, start: str = "09:15", end: str = "15:30") -> pd.DataFrame:
    """One session as yfinance returns it: a tz-aware ``Datetime`` index, OHLC, Adj Close, Volume."""
    index = pd.date_range(f"{day} {start}", f"{day} {end}", freq="5min", inclusive="left", tz=IST,
                          name="Datetime")
    close = 100.0 + pd.Series(range(len(index)), dtype=float).to_numpy() * 0.01
    return pd.DataFrame({"Open": close, "High": close + 0.5, "Low": close - 0.5, "Close": close,
                         "Adj Close": close, "Volume": 1000}, index=index)


def _daily(end: str = "2026-10-28", rows: int = 30) -> pd.DataFrame:
    dates = pd.bdate_range(end=end, periods=rows)
    return pd.DataFrame({"Date": dates, "Open": 100.0, "High": 102.0, "Low": 98.0, "Close": 100.0, "Volume": 1})


@pytest.fixture
def yahoo(tmp_path, monkeypatch):
    """Stubbed Yahoo: set ``frame`` (what history returns), ``now`` (the clock) and ``daily`` (load_ohlcv's rows)."""
    state = SimpleNamespace(frame=None, now=None, daily=_daily(), config={"data_cache_dir": str(tmp_path)})

    def ticker(symbol):
        return SimpleNamespace(history=lambda **kwargs: state.frame.copy())

    monkeypatch.setattr(yahoo_intraday.yf, "Ticker", ticker)
    monkeypatch.setattr(yahoo_intraday, "get_config", lambda: dict(state.config))
    monkeypatch.setattr(yahoo_intraday, "_now", lambda: state.now)
    monkeypatch.setattr(yahoo_intraday, "load_ohlcv", lambda symbol, as_of_date, fill_gaps=True: state.daily)
    return state


# --- #15: a fill bar that closes beyond the target -------------------------------------

@pytest.mark.unit
def test_a_fill_bar_closing_beyond_the_target_exits_at_the_target_in_that_bar():
    # The close comes after any fill in its bar: a long filled at 101 in a bar closing at 110
    # passed its 104 target on the way, where the resting order fills.
    breakout = _bars([("09:15", 100.0, 100.5, 99.8, 100.2), ("09:20", 100.2, 110.5, 100.0, 110.0),
                      ("09:25", 110.0, 111.0, 109.5, 110.5)])
    outcome = evaluate_plan(_plan(), breakout, "none", interval="5m")
    assert (outcome.entry_time, outcome.entry_price) == (_ist(SESSION, "09:20"), 101.0)
    assert (outcome.exit_reason, outcome.exit_time, outcome.exit_price) == ("target", _ist(SESSION, "09:20"), 104.0)
    assert outcome.gross_pnl == pytest.approx(300.0) and outcome.r_multiple == pytest.approx(2.0)
    # Filled at a gap open, and on the session's last bar.
    gap_open = _bars([("09:15", 102.0, 106.0, 101.8, 105.5), ("09:20", 105.6, 106.0, 105.0, 105.8)])
    gapped = evaluate_plan(_plan(), gap_open, "none", interval="5m")
    assert (gapped.entry_price, gapped.exit_reason, gapped.exit_time, gapped.exit_price) == (
        102.0, "target", _ist(SESSION, "09:15"), 104.0)
    last = evaluate_plan(_plan(exit_by=None), breakout.iloc[:2], "none", interval="5m")
    assert (last.exit_reason, last.exit_price, last.gross_pnl) == ("target", 104.0, pytest.approx(300.0))
    # A short whose fill bar closes under its target.
    breakdown = _bars([("09:15", 100.0, 100.2, 99.4, 99.6), ("09:20", 99.6, 99.7, 92.0, 92.5),
                       ("09:25", 92.4, 92.8, 92.0, 92.6)])
    short = evaluate_plan(_plan("short", entry=99.0, stop=100.5, target=97.0), breakdown, "none", interval="5m")
    assert (short.exit_reason, short.exit_time, short.exit_price) == ("target", _ist(SESSION, "09:20"), 97.0)
    assert short.gross_pnl == pytest.approx(200.0)
    # A fill bar that also reaches the stop is still stopped: its low may have come first.
    both = _bars([("09:15", 100.0, 100.5, 99.8, 100.2), ("09:20", 100.2, 110.5, 99.0, 110.0)])
    assert evaluate_plan(_plan(), both, "none", interval="5m").exit_reason == "stop"


# --- #16: R on the risk the plan was sized on ------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("open_, exit_price, r", [
    (98.01, 101.0, 2.99),      # 2.99 a share on the planned 1.00, not on the 0.01 left to the stop
    (98.0, 98.0, 0.0),         # filled at the stop: out at once, nothing gained or lost
    (97.0, 97.0, 0.0),         # filled through it
])
def test_r_is_measured_on_the_risk_the_plan_was_sized_on(open_, exit_price, r):
    plan = _plan(entry=99.0, stop=98.0, target=101.0)            # a buy limit at 99 (a pullback)
    bars = _bars([("09:15", open_, open_ + 0.3, open_, open_ + 0.2),
                  ("09:20", open_ + 0.2, 101.2, open_ + 0.1, 101.0)])
    outcome = evaluate_plan(plan, bars, "none", interval="5m")
    assert (outcome.entry_price, outcome.exit_price) == (open_, exit_price)
    assert outcome.r_multiple == pytest.approx(r)
    summary = summarize(plan, outcome, bars)
    assert "no stop" not in summary and f"{r:+.2f}R." in summary
    # With charges, the loss is the round trip's on the planned 100.00 at risk.
    charged = evaluate_plan(plan, bars, "india_equity_intraday", "NSE", "5m")
    assert charged.r_multiple == pytest.approx(charged.net_pnl / 100.0)


@pytest.mark.unit
def test_r_keeps_the_larger_risk_of_a_worse_fill_and_a_plan_without_a_stop_has_none():
    # A buy stop filled at a 102 gap open risked 2.50 a share, not the planned 1.50.
    gapped = _bars([("09:15", 102.0, 102.5, 101.8, 102.2), ("09:20", 102.2, 102.4, 102.0, 102.3)])
    assert evaluate_plan(_plan(exit_by=None), gapped, "none").r_multiple == pytest.approx(0.3 / 2.5)
    no_stop = evaluate_plan(_plan(stop=None, exit_by=None), gapped, "none")
    assert no_stop.r_multiple is None
    assert "no stop, so no R multiple" in summarize(_plan(stop=None, exit_by=None), no_stop, gapped)


# --- #17: a session on other hours gets no volume ratio --------------------------------

REGULAR = [_flat(day, "09:15", "15:30") for day in ("2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06")]
MUHURAT = date(2026, 11, 8)


@pytest.mark.unit
def test_a_session_on_other_hours_is_given_no_volume_ratio_against_the_usual_ones():
    # Muhurat trading (14:20-15:30) at the regular pace read 0.09x against the regular sessions to 14:50.
    now = _ist(MUHURAT, "14:50")
    bars = pd.concat([*REGULAR, _flat("2026-11-08", "14:20", "14:50")], ignore_index=True)
    levels = intraday_levels(bars, None, MUHURAT, now, interval="5m", exchange=NSE)
    assert levels["current"]["volume"] == 6000 and levels["relative_volume"] is None
    text = format_intraday_snapshot("RELIANCE.NS", levels, target_session(NSE, MUHURAT, now), time(15, 10), [], [])
    assert ("| Relative volume | n/a (a special session on other hours: it trades 14:20-15:30, the usual "
            "sessions 09:15-15:30, so its volume so far cannot be compared like for like with theirs) |") in text
    assert "0.09x" not in text
    # The morning after, Muhurat is the last session: no ratio for it either.
    monday = date(2026, 11, 9)
    after = pd.concat([*REGULAR, _flat("2026-11-08", "14:20", "15:30")], ignore_index=True)
    morning = intraday_levels(after, None, monday, _ist(monday, "09:05"), interval="5m", exchange=NSE)
    assert morning["last_session"]["date"] == MUHURAT and morning["last_session_relative_volume"] is None
    text = format_intraday_snapshot("RELIANCE.NS", morning, target_session(NSE, monday, _ist(monday, "09:05")),
                                    time(15, 10), [], [])
    assert "| 14,000 | n/a (a session on other hours) |" in text and "0.19x" not in text


# --- #18: the hours in force on a special session --------------------------------------

@pytest.mark.unit
def test_a_muhurat_snapshot_gives_that_sessions_hours_as_in_force(yahoo):
    yahoo.frame = pd.concat([*(_session(day) for day in ("2026-11-04", "2026-11-05", "2026-11-06")),
                             _session("2026-11-08", start="14:20", end="14:50")])
    yahoo.now = _ist(MUHURAT, "14:50")
    text = yahoo_intraday.get_intraday_bars("RELIANCE.NS", "2026-11-08")
    assert ("- NSE hours in force on 08 Nov 2026: Muhurat trading (Diwali), a special session: pre-open "
            "14:05-14:20 IST, trading 14:20-15:30.") in text
    assert "stocks with derivatives trade continuously" not in text
    # A regular day keeps the regular rules.
    yahoo.now = _ist("2026-11-06", "11:00")
    assert "- NSE hours in force on 06 Nov 2026: pre-open 09:00-09:15 IST" in yahoo_intraday.get_intraday_bars(
        "RELIANCE.NS", "2026-11-06")


# --- #19: a session the bars' cut-off falls inside is not complete ---------------------

@pytest.mark.unit
def test_a_session_cut_short_by_the_feeds_delay_is_not_called_complete(yahoo):
    # BSE is 15 minutes delayed: at 15:35 its bars stop at 15:20, before the 15:30 close,
    # and the last ten minutes, not yet served, make the session's high.
    friday = _session("2026-10-30")
    friday.iloc[-2:, friday.columns.get_loc("High")] = 120.0
    yahoo.frame = pd.concat([*(_session(day) for day in ("2026-10-27", "2026-10-28", "2026-10-29")), friday])
    yahoo.now = _ist(SESSION, "15:35")
    text = yahoo_intraday.get_intraday_bars("RELIANCE.BO", "2026-10-30")
    assert "## Last session: Fri 30 Oct 2026, through 15:20 IST only (from the 5m bars)" in text
    assert "Last complete session" not in text
    assert ("The data was cut before its 15:30 close, so its bars after 15:20 are missing and its high, low, close "
            "and volume are not final; its volume is set against the sessions before it to 15:20.") in text
    assert "| 73,000 | 1.00x |" in text                   # against the sessions before it to 15:20
    assert "## Classic floor pivots from the 30 Oct 2026 high, low and close through 15:20 (not final)" in text
    # Once the bars reach the close, it is complete, with the high its last bars made.
    yahoo.now = _ist(SESSION, "15:50")
    text = yahoo_intraday.get_intraday_bars("RELIANCE.BO", "2026-10-30")
    assert "## Last complete session: Fri 30 Oct 2026 (from the 5m bars)" in text
    assert "| 100.00 | 120.00 |" in text and "not final" not in text


@pytest.mark.unit
def test_a_last_session_ending_early_without_a_cut_off_is_still_complete():
    # An illiquid listing's last bars may be missing: that alone does not make a session incomplete.
    thursday = _flat("2026-10-29", "09:15", "15:00")
    levels = intraday_levels(pd.concat([*REGULAR[:2], thursday]), None, SESSION, _ist(SESSION, "09:05"),
                             interval="5m", exchange=NSE, cutoff=_ist("2026-10-29", "15:30"))
    assert levels["last_session"]["date"] == date(2026, 10, 29) and levels["last_session_through"] is None


# --- #20: the opening-price gap's daily close is dividend-adjusted ---------------------

@pytest.mark.unit
def test_the_gap_at_the_opening_price_says_its_daily_close_is_dividend_adjusted(yahoo):
    yahoo.daily = _daily(end="2026-10-29")
    yahoo.daily.loc[yahoo.daily["Date"] == pd.Timestamp("2026-10-29"), "Open"] = 101.0
    yahoo.frame = pd.concat([_session("2026-10-27"), _session("2026-10-28"), _session("2026-10-29", end="10:10")])
    yahoo.now = _ist("2026-10-29", "10:07")
    text = yahoo_intraday.get_intraday_bars("RELIANCE.NS", "2026-10-29")
    assert ("| Gap at the opening price (daily bar, 101.00) vs the previous daily close, dividend-adjusted (100.00) "
            "| +1.00% |") in text
    assert ("The gap at the opening price is measured against Yahoo's daily close, which is dividend-adjusted "
            "(the intraday bars are not)") in text
    # Without that row there is nothing to explain.
    yahoo.now = _ist("2026-10-29", "09:05")
    early = yahoo_intraday.get_intraday_bars("RELIANCE.NS", "2026-10-29")
    assert "Gap at the opening price" not in early and "The gap at the opening price" not in early


# --- #21: a plan made after the square-off --------------------------------------------

TODAY = {"symbol": "RELIANCE.NS", "session": "2026-10-30", "why": "the rest of today's session", "opens": "09:15",
         "exit_by": "15:10", "timezone": "Asia/Kolkata", "tz_label": "IST", "reference_price": 100.0,
         "reference_through": None, "interval": "5m", "source": "Yahoo Finance", "note": None, "past_dated": False}
LONG = {"direction": "long", "entry_price": 101.0, "stop_loss": 99.5, "target_price": 104.0}


@pytest.mark.unit
@pytest.mark.parametrize("exit_by", ["15:10", "15:00", "15:25", None])
def test_a_plan_made_after_the_square_off_is_said_to_be_unable_to_trade(exit_by):
    plan = plan_from_fields({**LONG, "exit_by": exit_by}, TODAY, decided_at=_ist(SESSION, "15:20"))
    assert plan["exit_by"] == "15:10"
    assert plan["notes"] == ["the 15:10 square-off is not after 15:20, when the plan was made, so the plan "
                             "cannot trade and is scored as not triggered"]
    # The replay agrees, without a time between the two.
    bars = _bars([("15:15", 100.0, 101.5, 99.9, 101.2), ("15:20", 101.2, 101.6, 101.0, 101.4),
                  ("15:25", 101.4, 101.8, 101.2, 101.6)])
    outcome = evaluate_plan(stored_plan({"intraday_plan": plan}), bars, "none", interval="5m")
    assert outcome.exit_reason == "not_triggered"
    assert outcome.note == "the exit-by time 15:10 had passed when the plan was made (15:20)"


@pytest.mark.unit
def test_a_plan_made_after_the_square_off_notes_no_score_it_cannot_have():
    # A plan the replay cannot score as a trade is not said to be "not triggered".
    wrong_side = plan_from_fields({**LONG, "stop_loss": 102.0}, TODAY, decided_at=_ist(SESSION, "15:20"))
    assert wrong_side["notes"] == [
        "the 15:10 square-off is not after 15:20, when the plan was made, so the plan cannot trade",
        "the stop is not below the entry for a long, so it is scored as no trade"]
    assert plan_from_fields({"direction": "none"}, TODAY, decided_at=_ist(SESSION, "15:20"))["notes"] == []
    # Made before the square-off, an exit-by it leaves no bar still falls back to the square-off.
    early = plan_from_fields({**LONG, "exit_by": "10:30"}, TODAY, decided_at=_ist(SESSION, "10:42"))
    assert early["notes"] == ["the plan's exit-by 10:30 is not after 10:42, when the plan was made, so it is "
                              "scored to 15:10"]


# --- #43: a plan made during its session is judged on the bars after it ----------------

def _midsession_bars() -> pd.DataFrame:
    """A flat session at 100 that traded up to 103.50 between 10:00 and 10:30."""
    frame = _flat("2026-10-30", "09:15", "15:30")
    frame["High"], frame["Low"] = 100.3, 99.8
    morning = (frame["Datetime"].dt.hour == 10) & (frame["Datetime"].dt.minute <= 30)
    frame.loc[morning, ["High", "Close"]] = [103.5, 102.0]
    return frame


@pytest.mark.unit
def test_a_plan_made_during_its_session_is_not_said_to_have_missed_the_morning():
    made = _ist(SESSION, "13:02")
    plan = _plan(target=103.0, exit_by=None, shares=50, valid_from=made)
    bars = _midsession_bars()
    outcome = evaluate_plan(plan, bars, "none", interval="5m")
    assert outcome.exit_reason == "not_triggered"
    assert outcome.note == "the price never reached the entry after 13:02, when the plan was made"
    assert describe_outcome({"session": "2026-10-30"}, {**outcome.to_dict(), "scored": True}) == (
        "2026-10-30: no trade (the price never reached the entry after 13:02, when the plan was made)")
    summary = summarize(plan, outcome, bars)
    assert "read against 100.00 when it was made, at 13:02 IST (bars before then are not used)." in summary
    assert ("From 13:05, after the plan was made, the session traded: open 100.00, high 100.30, low 99.80, close "
            "100.00 (Yahoo Finance 5m bars); the whole session, with the trading before the plan, had high 103.50 "
            "and low 99.80.") in summary
    assert "The session traded:" not in summary
    # With an exit-by, the window it was looked for in.
    timed = evaluate_plan(replace(plan, exit_by=time(15, 10)), bars, "none", interval="5m")
    assert timed.note == "the entry did not fill between 13:02, when the plan was made, and the exit-by time 15:10"
    # A plan made before its session's first bar is judged on all of it, as before.
    early = replace(plan, valid_from=_ist(SESSION, "08:00"))
    assert evaluate_plan(early, bars, "none", interval="5m").exit_reason == "target"
    assert "The session traded: open 100.00, high 103.50" in summarize(
        early, evaluate_plan(early, bars, "none", interval="5m"), bars)


@pytest.mark.unit
def test_a_plan_made_after_its_sessions_last_bar_says_no_bar_was_left():
    plan = _plan(exit_by=None, valid_from=_ist(SESSION, "15:40"))
    bars = _midsession_bars()
    outcome = evaluate_plan(plan, bars, "none", interval="5m")
    assert outcome.note == "no bar starts after the plan was made"
    summary = summarize(plan, outcome, bars)
    assert ("No bar of the session started after the plan was made at 15:40 IST; before then it traded: open "
            "100.00, high 103.50, low 99.80, close 100.00 (Yahoo Finance 5m bars).") in summary


# --- #44: the report header's exit-by is the plan's ------------------------------------

CONTEXT = {"symbol": "RELIANCE.NS", "session": "2026-11-02", "opens": "09:15", "exit_by": "15:10",
           "why": "the next trading day after the analysis date Fri 30 Oct 2026", "timezone": "Asia/Kolkata",
           "tz_label": "IST"}
HEAD = "intraday paper plan for Mon 02 Nov 2026 (the next trading day after the analysis date Fri 30 Oct 2026); "


@pytest.mark.unit
def test_the_report_header_gives_the_plans_own_exit_by_time(tmp_path):
    state = {"trade_date": "2026-10-30", "decision_horizon": "intraday", "intraday_context": CONTEXT,
             "intraday_plan": {"exit_by": "12:30"}, "final_trade_decision": "**Rating**: Buy", "final_rating": "Buy"}
    assert horizon_line(state) == HEAD + "exit by 12:30 IST (the configured square-off is 15:10 IST)"
    report = write_report_tree(state, "RELIANCE.NS", tmp_path, settings={"decision_horizon": "intraday"})
    assert "- Horizon: " + HEAD + "exit by 12:30 IST (the configured square-off is 15:10 IST)" in report.read_text()
    assert "exit by 12:30 IST (the configured square-off is 15:10 IST)" in (
        tmp_path / "complete_report.html").read_text()
    # A plan out at the square-off names it once; before the plan exists (the run's start), the square-off.
    assert horizon_line({**state, "intraday_plan": {"exit_by": "15:10"}}) == HEAD + "exit by 15:10 IST"
    assert horizon_line({**state, "intraday_plan": None}) == HEAD + "exit by 15:10 IST"
    # A listing without a square-off: the plan's time, else the session close.
    us = {**CONTEXT, "exit_by": None, "tz_label": "America/New_York"}
    assert horizon_line({**state, "intraday_context": us}) == HEAD + "exit by 12:30 America/New_York"
    assert horizon_line({**state, "intraday_context": us, "intraday_plan": {"exit_by": None}}) == (
        HEAD + "exit by the session close")
