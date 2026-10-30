"""Review of the intraday fixes: the hours every agent is told on a Muhurat day, the reason a Muhurat
session gets no volume ratio, and the label on a previous close taken from Yahoo's dividend-adjusted rows.

Calendar arithmetic, synthetic bars and a stubbed Yahoo at pinned moments: nothing reaches the network or
depends on today.
"""

from datetime import date, datetime, time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from tradingagents.agents import context
from tradingagents.dataflows.vendors.yahoo import intraday as yahoo_intraday
from tradingagents.markets import india
from tradingagents.markets.exchanges import BSE, NSE
from tradingagents.markets.intraday import format_intraday_snapshot, intraday_levels, target_session

IST = ZoneInfo("Asia/Kolkata")
MUHURAT = date(2026, 11, 8)
MUHURAT_HOURS = "Muhurat trading (Diwali), a special session: pre-open 14:05-14:20 IST, trading 14:20-15:30"


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


def _daily(end: str = "2026-10-29", rows: int = 30) -> pd.DataFrame:
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


# --- R8: the briefing every NSE/BSE agent reads gives a special session's own hours ---------

@pytest.mark.unit
def test_the_hours_in_force_on_a_day_are_a_special_sessions_own_when_it_keeps_other_hours():
    assert india.hours_in_force(NSE, MUHURAT) == MUHURAT_HOURS
    assert india.hours_in_force(BSE, MUHURAT) == MUHURAT_HOURS
    # A special session on the regular hours (the Union Budget's), and a usual day, keep the usual rules.
    for day in (date(2026, 2, 1), date(2026, 11, 6), date(2026, 12, 7)):
        assert india.hours_in_force(NSE, day) == india.session_rules(day)


@pytest.mark.unit
def test_the_briefing_on_a_muhurat_day_gives_that_sessions_hours_apart_from_the_usual_ones():
    text = india.market_briefing("NSE", on=MUHURAT)
    assert f"Hours in force on 08 Nov 2026: {MUHURAT_HOURS}." in text
    # The usual hours stay (the next session keeps them), but no longer follow "Muhurat trading at Diwali:".
    assert "Muhurat trading at Diwali:" not in text
    assert "such as Muhurat trading at Diwali. Usual hours: pre-open 09:00-09:15 IST; stocks with derivatives" in text
    assert text.index("Usual hours:") < text.index("Hours in force on 08 Nov 2026")
    # Other days name no hours in force but the usual ones.
    for day in (date(2026, 2, 1), date(2026, 11, 6), date(2026, 11, 7), date(2025, 6, 2)):
        other = india.market_briefing("NSE", on=day)
        assert "Hours in force" not in other and f"Usual hours: {india.session_rules(day)}." in other


@pytest.mark.unit
def test_every_agents_instrument_context_on_a_muhurat_day_gives_that_sessions_hours(monkeypatch):
    # A live run during the session.
    monkeypatch.setattr(context, "is_historical", lambda trade_date, ticker=None: False)
    live = context.build_instrument_context("RELIANCE.NS", trade_date="2026-11-08", now=_ist(MUHURAT, "14:50"))
    assert "NSE is open (Muhurat trading (Diwali)) at Sun 08 Nov 2026, 14:50 IST" in live
    assert f"Hours in force on 08 Nov 2026: {MUHURAT_HOURS}." in live
    assert "Muhurat trading at Diwali:" not in live
    # A run dated that day, read later, on BSE.
    monkeypatch.setattr(context, "is_historical", lambda trade_date, ticker=None: True)
    past = context.build_instrument_context("RELIANCE.BO", trade_date="2026-11-08")
    assert "BSE special session (Muhurat trading (Diwali), 14:20-15:30 IST)." in past
    assert f"Hours in force on 08 Nov 2026: {MUHURAT_HOURS}." in past


# --- R9: why a Muhurat session gets no volume ratio -------------------------------------------

REGULAR = [_flat(day, "09:15", "15:30") for day in ("2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06")]


@pytest.mark.unit
def test_a_muhurat_sessions_missing_volume_ratio_gives_a_reason_that_is_true():
    now = _ist(MUHURAT, "14:50")
    bars = pd.concat([*REGULAR, _flat("2026-11-08", "14:20", "14:50")], ignore_index=True)
    # The regular sessions did trade at these times of day; they had been open since 09:15.
    earlier = bars[bars["Datetime"].dt.date < MUHURAT]
    at_these_times = earlier["Datetime"].dt.time.between(time(14, 20), time(14, 45))
    assert at_these_times.sum() == 24 and earlier.loc[at_these_times, "Volume"].sum() == 24_000
    levels = intraday_levels(bars, None, MUHURAT, now, interval="5m", exchange=NSE)
    assert levels["relative_volume"] is None     # still no ratio, as the earlier fix made it
    text = format_intraday_snapshot("RELIANCE.NS", levels, target_session(NSE, MUHURAT, now), time(15, 10), [], [])
    assert ("| Relative volume | n/a (a special session on other hours: it trades 14:20-15:30, the usual "
            "sessions 09:15-15:30, so its volume so far cannot be compared like for like with theirs) |") in text
    assert "no prior session traded" not in text
    doc = " ".join(intraday_levels.__doc__.split())
    assert "no usual session traded at those times of day" not in doc


# --- R10: a previous close from the dividend-adjusted daily rows says so ------------------------

@pytest.mark.unit
def test_the_gap_at_the_opening_price_labels_its_previous_close_dividend_adjusted(yahoo):
    # Thursday 29 Oct is an ex-dividend day (2.00 a share): the daily rows, adjusted, put Wednesday's
    # close at 98.74 where its last bar closed, as traded, at 100.74.
    yahoo.daily = _daily(end="2026-10-29")
    yahoo.daily.loc[yahoo.daily["Date"] == pd.Timestamp("2026-10-28"), "Close"] = 98.74
    yahoo.daily.loc[yahoo.daily["Date"] == pd.Timestamp("2026-10-29"), "Open"] = 101.0
    yahoo.frame = pd.concat([_session("2026-10-27"), _session("2026-10-28"), _session("2026-10-29", end="10:10")])
    yahoo.now = _ist("2026-10-29", "10:07")
    text = yahoo_intraday.get_intraday_bars("RELIANCE.NS", "2026-10-29")
    assert ("| Gap at the opening price (daily bar, 101.00) vs the previous daily close, dividend-adjusted "
            "(98.74) | +2.29% |") in text
    assert "## Last complete session: Wed 28 Oct 2026 (from the 5m bars)" in text
    assert "| 100.00 | 101.24 | 99.50 | 100.74 |" in text      # the close as traded, beside it
    assert "| Gap at the first bar's open vs the previous close | -0.73% |" in text
    assert "The gap at the opening price is measured against Yahoo's daily close, which is dividend-adjusted" in text
    # With one session of bars the last session comes from the same adjusted rows, and says so,
    # as does the gap at the first bar's open measured against its close.
    one = yahoo_intraday.get_intraday_bars("RELIANCE.NS", "2026-10-29", sessions=1)
    assert "## Last complete session: Wed 28 Oct 2026 (from the daily bar, dividend-adjusted)" in one
    assert "| 100.00 | 102.00 | 98.00 | 98.74 |" in one
    assert "| Gap at the first bar's open vs the previous close, dividend-adjusted | +1.28% |" in one


@pytest.mark.unit
def test_rows_from_daily_bars_not_known_to_be_adjusted_carry_no_such_label():
    # The pure formatter is told when the daily rows are adjusted; synthetic ones are not.
    daily = _daily(end="2026-10-29")
    daily.loc[daily["Date"] == pd.Timestamp("2026-10-29"), "Open"] = 101.0
    bars = pd.concat([_session("2026-10-28").reset_index(), _session("2026-10-29", end="10:10").reset_index()])
    now = _ist("2026-10-29", "10:07")
    levels = intraday_levels(bars, daily, date(2026, 10, 29), now, interval="5m", exchange=NSE)
    text = format_intraday_snapshot("RELIANCE.NS", levels, target_session(NSE, date(2026, 10, 29), now),
                                    time(15, 10), [], [])
    assert "| Gap at the opening price (daily bar, 101.00) vs the previous daily close (100.00) | +1.00% |" in text
    assert "dividend-adjusted" not in text
