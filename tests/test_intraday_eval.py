"""Scoring an intraday paper plan on its session's bars: fills, exits and charges, every rule leaning against the plan.

Pure: synthetic 5m bars on one NSE session, hand-computed results.
"""

from dataclasses import replace
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from tradingagents.markets import costs
from tradingagents.markets.intraday_eval import IntradayOutcome, IntradayPlan, evaluate_plan

IST = ZoneInfo("Asia/Kolkata")
SESSION = date(2026, 10, 30)


def _bars(rows, day: date = SESSION) -> pd.DataFrame:
    """Bars from (HH:MM, open, high, low, close) rows."""
    return pd.DataFrame([{"Datetime": pd.Timestamp(f"{day} {hhmm}", tz=IST), "Open": o, "High": h, "Low": low,
                          "Close": c, "Volume": 1000} for hhmm, o, h, low, c in rows])


def _plan(direction="long", entry=101.0, stop=99.5, target=104.0, exit_by=time(15, 10), shares=100,
          reference=100.0, valid_from=None) -> IntradayPlan:
    return IntradayPlan(direction, entry, stop, target, exit_by, shares, SESSION, reference, valid_from)


def _ist(hh: int, mm: int) -> datetime:
    return datetime(SESSION.year, SESSION.month, SESSION.day, hh, mm, tzinfo=IST)


# A breakout through 101 at 09:20, then 104 at 09:30.
BREAKOUT = _bars([
    ("09:15", 100.0, 100.5, 99.8, 100.2),
    ("09:20", 100.2, 101.2, 100.0, 101.0),
    ("09:25", 101.0, 102.0, 100.8, 101.8),
    ("09:30", 101.8, 104.5, 101.5, 104.2),
])


@pytest.mark.unit
def test_a_long_breakout_fills_at_the_entry_and_exits_at_the_target():
    outcome = evaluate_plan(_plan(), BREAKOUT, "none")
    assert (outcome.triggered, outcome.entry_type, outcome.exit_reason) == (True, "stop", "target")
    assert (outcome.entry_time, outcome.entry_price) == (_ist(9, 20), 101.0)
    assert (outcome.exit_time, outcome.exit_price) == (_ist(9, 30), 104.0)
    assert (outcome.gross_pnl, outcome.charges, outcome.net_pnl) == (pytest.approx(300.0), 0.0, pytest.approx(300.0))
    assert outcome.net_return_pct == pytest.approx(300 / 10_100 * 100)
    assert outcome.r_multiple == pytest.approx(2.0)          # 3.00 gained on 1.50 risked
    assert outcome.shares == 100 and outcome.note == ""


@pytest.mark.unit
def test_charges_are_the_intraday_round_trip():
    # Buy 10,100: brokerage 3.03, exchange 0.31007, SEBI 0.0101, stamp 0.303, GST 0.6030306 = 4.2562006.
    # Sell 10,400: brokerage 3.12, STT 2.60, exchange 0.31928, SEBI 0.0104, GST 0.6209424 = 6.6706224.
    outcome = evaluate_plan(_plan(), BREAKOUT, "india_equity_intraday", "NSE")
    assert outcome.charges == pytest.approx(10.926823)
    assert outcome.net_pnl == pytest.approx(289.073177)
    assert outcome.net_return_pct == pytest.approx(2.8621107, abs=1e-6)
    assert outcome.r_multiple == pytest.approx(1.92715451, abs=1e-6)
    profile = costs.get_cost_profile("india_equity_intraday")
    bse = evaluate_plan(_plan(), BREAKOUT, profile, "BSE")
    assert bse.charges == pytest.approx(costs.round_trip_costs(profile, 10_100, 10_400, "BSE"))


@pytest.mark.unit
def test_a_long_pullback_is_a_limit_that_fills_as_the_price_falls_to_it():
    bars = _bars([
        ("09:15", 100.0, 100.4, 99.5, 99.8),
        ("09:20", 99.8, 99.9, 98.9, 99.2),
        ("09:25", 99.2, 101.3, 99.1, 101.0),
    ])
    outcome = evaluate_plan(_plan(entry=99.0, stop=98.0, target=101.0), bars, "none")
    assert (outcome.entry_type, outcome.entry_time, outcome.entry_price) == ("limit", _ist(9, 20), 99.0)
    assert (outcome.exit_reason, outcome.exit_price, outcome.gross_pnl) == ("target", 101.0, pytest.approx(200.0))
    assert outcome.r_multiple == pytest.approx(2.0)


@pytest.mark.unit
def test_a_short_breakdown_and_a_short_into_a_rally():
    bars = _bars([
        ("09:15", 100.0, 100.2, 99.4, 99.6),
        ("09:20", 99.6, 99.7, 98.8, 99.0),
        ("09:25", 99.0, 99.2, 96.8, 97.1),
    ])
    outcome = evaluate_plan(_plan("short", entry=99.0, stop=100.5, target=97.0), bars, "none")
    assert (outcome.entry_type, outcome.entry_price, outcome.exit_reason) == ("stop", 99.0, "target")
    assert outcome.gross_pnl == pytest.approx(200.0) and outcome.r_multiple == pytest.approx(2.0 / 1.5)

    rally = _bars([("09:15", 100.0, 101.2, 99.9, 101.0), ("09:20", 101.0, 101.1, 97.9, 98.2)])
    short = evaluate_plan(_plan("short", entry=101.0, stop=102.0, target=98.0), rally, "none")
    assert (short.entry_type, short.entry_time, short.exit_reason) == ("limit", _ist(9, 15), "target")
    # A short pays the sell-side STT on its opening sale.
    taxed = evaluate_plan(_plan("short", entry=101.0, stop=102.0, target=98.0), rally, "india_equity_intraday", "NSE")
    profile = costs.get_cost_profile("india_equity_intraday")
    assert taxed.charges == pytest.approx(costs.round_trip_costs(profile, 9_800, 10_100, "NSE"))


@pytest.mark.unit
def test_a_gap_through_the_entry_fills_at_the_open():
    gapped = _bars([("09:15", 102.0, 102.5, 101.8, 102.2), ("09:20", 102.2, 102.4, 102.0, 102.3)])
    outcome = evaluate_plan(_plan(exit_by=None), gapped, "none")
    assert (outcome.entry_time, outcome.entry_price) == (_ist(9, 15), 102.0)       # worse than the 101 stop entry
    assert outcome.r_multiple == pytest.approx((102.3 - 102.0) / (102.0 - 99.5))
    below = _bars([("09:15", 98.5, 99.2, 98.4, 99.0), ("09:20", 99.0, 99.4, 98.9, 99.3)])
    limit = evaluate_plan(_plan(entry=99.0, stop=98.0, target=101.0, exit_by=None), below, "none")
    assert (limit.entry_type, limit.entry_price) == ("limit", 98.5)                # a limit fills at the better open


@pytest.mark.unit
def test_a_gap_through_the_entry_and_the_target_exits_at_the_fill():
    # A results-day open at 110 fills the 101 buy stop there, already beyond the 104 target:
    # the target order fills at once, not at 104 in a later bar that never traded there.
    gap_up = _bars([("09:15", 110.0, 111.0, 109.5, 110.5), ("09:20", 110.5, 111.5, 110.2, 111.0)])
    outcome = evaluate_plan(_plan(), gap_up, "none")
    assert (outcome.entry_time, outcome.entry_price) == (_ist(9, 15), 110.0)
    assert (outcome.exit_reason, outcome.exit_time, outcome.exit_price) == ("target", _ist(9, 15), 110.0)
    assert outcome.gross_pnl == pytest.approx(0.0)
    gap_down = _bars([("09:15", 90.0, 90.5, 89.0, 89.5), ("09:20", 89.5, 89.8, 88.0, 88.5)])
    short = evaluate_plan(_plan("short", entry=99.0, stop=100.5, target=97.0), gap_down,
                          "india_equity_intraday", "NSE")
    assert (short.exit_reason, short.entry_price, short.exit_price) == ("target", 90.0, 90.0)
    assert short.net_pnl == pytest.approx(-short.charges)        # the round trip's charges, nothing more


@pytest.mark.unit
def test_a_bar_opening_beyond_the_target_fills_it_at_the_open():
    bars = pd.concat([BREAKOUT.iloc[:3], _bars([("09:30", 105.0, 105.5, 104.6, 105.2)])], ignore_index=True)
    outcome = evaluate_plan(_plan(), bars, "none")
    assert (outcome.exit_reason, outcome.exit_time, outcome.exit_price) == ("target", _ist(9, 30), 105.0)
    rally = _bars([("09:15", 100.0, 100.2, 99.4, 99.6), ("09:20", 99.6, 99.7, 98.8, 99.0),
                   ("09:25", 96.5, 96.8, 96.0, 96.2)])
    short = evaluate_plan(_plan("short", entry=99.0, stop=100.5, target=97.0), rally, "none")
    assert (short.exit_reason, short.exit_price) == ("target", 96.5)


@pytest.mark.unit
def test_a_bar_opening_beyond_the_target_exits_there_before_its_stop():
    # The open comes first: the target order fills there, before the bar falls through the stop.
    bars = pd.concat([BREAKOUT.iloc[:3], _bars([("09:30", 105.0, 105.5, 99.0, 99.2)])], ignore_index=True)
    outcome = evaluate_plan(_plan(), bars, "none")
    assert (outcome.exit_reason, outcome.exit_time, outcome.exit_price) == ("target", _ist(9, 30), 105.0)
    whipsaw = _bars([("09:15", 100.0, 100.2, 99.4, 99.6), ("09:20", 99.6, 99.7, 98.8, 99.0),
                     ("09:25", 96.5, 100.8, 96.0, 100.6)])
    short = evaluate_plan(_plan("short", entry=99.0, stop=100.5, target=97.0), whipsaw, "none")
    assert (short.exit_reason, short.exit_price) == ("target", 96.5)
    # Opening between the stop and the target, a bar reaching both is still a stop.
    both = pd.concat([BREAKOUT.iloc[:3], _bars([("09:30", 102.0, 105.5, 99.0, 99.2)])], ignore_index=True)
    assert evaluate_plan(_plan(), both, "none").exit_reason == "stop"


@pytest.mark.unit
def test_a_bar_reaching_both_the_stop_and_the_target_is_a_stop():
    bars = pd.concat([BREAKOUT.iloc[:2], _bars([("09:25", 101.0, 104.5, 99.4, 102.0)])], ignore_index=True)
    outcome = evaluate_plan(_plan(), bars, "none")
    assert (outcome.exit_reason, outcome.exit_time, outcome.exit_price) == ("stop", _ist(9, 25), 99.5)
    assert outcome.gross_pnl == pytest.approx(-150.0) and outcome.r_multiple == pytest.approx(-1.0)


@pytest.mark.unit
def test_the_fill_bar_is_checked_for_the_stop_only():
    # The fill bar also reaches the target, but its high may have come before the fill.
    through_target = _bars([("09:15", 100.0, 100.5, 99.8, 100.2), ("09:20", 100.2, 104.5, 100.0, 103.0),
                            ("09:25", 103.0, 103.5, 102.5, 103.0)])
    held = evaluate_plan(_plan(exit_by=None), through_target, "none")
    assert (held.entry_time, held.exit_reason, held.exit_price) == (_ist(9, 20), "session_close", 103.0)
    assert "closed at the last bar's close (09:25)" in held.note
    # Its low may have come after the fill: stopped in the same bar.
    through_stop = _bars([("09:15", 100.0, 100.5, 99.8, 100.2), ("09:20", 100.2, 101.2, 99.4, 100.0)])
    stopped = evaluate_plan(_plan(), through_stop, "none")
    assert (stopped.entry_time, stopped.exit_time, stopped.exit_reason, stopped.exit_price) == (
        _ist(9, 20), _ist(9, 20), "stop", 99.5)


@pytest.mark.unit
def test_a_stop_gapped_through_exits_at_the_open():
    bars = pd.concat([BREAKOUT.iloc[:2], _bars([("09:25", 99.0, 99.6, 98.5, 99.2)])], ignore_index=True)
    outcome = evaluate_plan(_plan(), bars, "none")
    assert (outcome.exit_reason, outcome.exit_price) == ("stop", 99.0)


def _day_bars(last_close: float = 102.5) -> pd.DataFrame:
    return _bars([("09:15", 100.0, 100.5, 99.8, 100.2), ("09:20", 100.2, 101.2, 100.0, 101.0),
                  ("15:05", 102.0, 102.4, 101.9, 102.2), ("15:10", 102.3, 102.6, 102.1, 102.4),
                  ("15:15", 102.4, 102.7, 102.3, 102.6), ("15:25", 102.6, 102.8, 102.4, last_close)])


@pytest.mark.unit
def test_an_open_position_is_closed_at_the_exit_by_time():
    outcome = evaluate_plan(_plan(), _day_bars(), "none")
    assert (outcome.exit_reason, outcome.exit_time, outcome.exit_price) == ("exit_by", _ist(15, 10), 102.3)
    # 15:12 falls inside the 15:10-15:15 bar, part of which comes after the square-off: out at its open.
    later = evaluate_plan(_plan(exit_by=time(15, 12)), _day_bars(), "none")
    assert (later.exit_reason, later.exit_time, later.exit_price) == ("exit_by", _ist(15, 10), 102.3)
    assert evaluate_plan(_plan(exit_by=time(15, 12)), _day_bars(), "none", interval="5m") == later


@pytest.mark.unit
def test_the_bar_an_exit_by_time_falls_in_takes_no_target_and_fills_no_entry():
    day = _bars([("09:15", 100.0, 100.4, 99.8, 100.1), ("15:05", 100.6, 100.9, 100.5, 100.8),
                 ("15:10", 100.8, 104.3, 100.7, 104.1), ("15:15", 104.1, 104.2, 104.0, 104.1)])
    held = evaluate_plan(_plan(entry=100.5, stop=99.5, exit_by=time(15, 12)), day, "none")
    assert (held.entry_time, held.exit_reason, held.exit_time, held.exit_price) == (
        _ist(15, 5), "exit_by", _ist(15, 10), 100.8)               # not the 104 its high reached, maybe at 15:13
    late = evaluate_plan(_plan(entry=102.0, stop=101.0, target=105.0, exit_by=time(15, 12)), day, "none")
    assert late.exit_reason == "not_triggered" and "exit-by time 15:12" in late.note


@pytest.mark.unit
def test_without_an_exit_by_time_the_position_is_held_to_the_close():
    outcome = evaluate_plan(_plan(exit_by=None), _day_bars(last_close=102.7), "none")
    assert (outcome.exit_reason, outcome.exit_time, outcome.exit_price) == ("session_close", _ist(15, 25), 102.7)


@pytest.mark.unit
def test_an_entry_never_reached_is_not_triggered():
    outcome = evaluate_plan(_plan(entry=105.0, target=108.0), BREAKOUT, "none")
    assert (outcome.triggered, outcome.exit_reason, outcome.entry_type) == (False, "not_triggered", "stop")
    assert (outcome.entry_price, outcome.gross_pnl, outcome.net_pnl, outcome.r_multiple) == (None, None, None, None)
    # Reached only after the exit-by time: no entry fills from then on.
    late = _bars([("09:15", 100.0, 100.5, 99.8, 100.2), ("15:15", 100.2, 101.5, 100.1, 101.2)])
    outcome = evaluate_plan(_plan(), late, "none")
    assert outcome.exit_reason == "not_triggered" and "exit-by time 15:10" in outcome.note


@pytest.mark.unit
def test_a_plan_made_during_its_session_ignores_the_bars_before_it():
    bars = _bars([("09:15", 100.0, 100.4, 99.5, 99.8), ("09:20", 99.8, 99.9, 98.9, 99.2),
                  ("09:25", 99.2, 99.6, 99.1, 99.5)])
    pullback = _plan(entry=99.0, stop=98.0, target=101.0, reference=99.5)
    assert evaluate_plan(pullback, bars, "none").entry_time == _ist(9, 20)
    made_later = replace(pullback, valid_from=_ist(9, 25))
    assert evaluate_plan(made_later, bars, "none").exit_reason == "not_triggered"
    after_the_last = replace(pullback, valid_from=_ist(9, 30))
    assert "after the plan was made" in evaluate_plan(after_the_last, bars, "none").note


@pytest.mark.unit
@pytest.mark.parametrize("plan,why", [
    (_plan("none"), "no trade was planned"),
    (_plan(entry=None), "no entry price"),
    (_plan(stop=101.5), "the stop is not below the entry for a long"),
    (_plan("short", entry=99.0, stop=98.0, target=97.0), "the stop is not above the entry for a short"),
    (_plan(target=100.0), "the target is not above the entry for a long"),
])
def test_a_plan_that_is_not_a_trade_is_scored_as_none(plan, why):
    outcome = evaluate_plan(plan, BREAKOUT, "none")
    assert (outcome.triggered, outcome.exit_reason) == (False, "no_plan") and why in outcome.note


@pytest.mark.unit
def test_no_bars_for_the_session_is_no_data():
    for bars in (None, BREAKOUT.iloc[:0], _bars([("09:15", 100.0, 101.0, 99.0, 100.0)], day=date(2026, 10, 29))):
        outcome = evaluate_plan(_plan(), bars, "none")
        assert (outcome.triggered, outcome.exit_reason) == (False, "no_data")


@pytest.mark.unit
def test_without_shares_the_results_are_per_share():
    outcome = evaluate_plan(_plan(shares=None), BREAKOUT, "india_equity_intraday", "NSE")
    assert outcome.shares is None and outcome.note.startswith("per share")
    assert outcome.gross_pnl == pytest.approx(3.0)
    assert outcome.charges == pytest.approx(0.10926823)       # one share: no brokerage cap reached
    assert outcome.net_return_pct == pytest.approx(2.8621107, abs=1e-6)
    assert outcome.r_multiple == pytest.approx(2.89073177 / 1.5)


@pytest.mark.unit
def test_without_a_reference_price_the_sessions_first_open_sets_the_order_type():
    assert evaluate_plan(_plan(reference=None), BREAKOUT, "none").entry_type == "stop"      # 101 above the 100 open
    pullback = _plan(entry=99.0, stop=98.0, target=101.0, reference=None)
    assert evaluate_plan(pullback, BREAKOUT, "none").entry_type == "limit"


@pytest.mark.unit
def test_plans_and_outcomes_round_trip_through_plain_dicts():
    plan = _plan(valid_from=_ist(10, 5))
    data = plan.to_dict()
    assert data["exit_by"] == "15:10" and data["session"] == "2026-10-30"
    assert IntradayPlan.from_dict(data) == plan
    loose = IntradayPlan.from_dict({"direction": "LONG", "entry": "101", "stop": "99.5", "target": "",
                                    "exit_by": "", "shares": "100", "session": "2026-10-30"})
    assert (loose.direction, loose.entry, loose.target, loose.exit_by, loose.shares) == ("long", 101.0, None, None, 100)
    with pytest.raises(ValueError, match="direction"):
        IntradayPlan.from_dict({"direction": "buy", "session": "2026-10-30"})
    with pytest.raises(ValueError, match="session"):
        IntradayPlan.from_dict({"direction": "none"})
    outcome = evaluate_plan(plan, BREAKOUT, "none")
    assert isinstance(outcome, IntradayOutcome)
    assert outcome.to_dict()["exit_reason"] == "not_triggered"          # nothing after 10:05 in these bars
    assert evaluate_plan(_plan(), BREAKOUT, "none").to_dict()["entry_time"] == "2026-10-30T09:20:00+05:30"
