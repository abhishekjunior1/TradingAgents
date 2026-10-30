"""Day-trading arithmetic on synthetic bars: the plan's session, the square-off, levels and paper sizing.

Pure: every figure is checked against a hand computation, at fixed moments, so
nothing depends on today or reaches a market.
"""

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from tradingagents.markets import costs, risk
from tradingagents.markets.exchanges import BSE, NSE
from tradingagents.markets.intraday import (
    DEFAULT_SQUARE_OFF,
    bar_ends,
    format_intraday_snapshot,
    in_regular_session,
    infer_session_hours,
    intraday_levels,
    intraday_position,
    square_off_time,
    target_session,
)

IST = ZoneInfo("Asia/Kolkata")
NEW_YORK = ZoneInfo("America/New_York")

THURSDAY = date(2026, 10, 29)
FRIDAY = date(2026, 10, 30)


def _at(day: date, hh: int, mm: int, zone=IST) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=zone)


def _bars(day: str, rows, zone=IST) -> pd.DataFrame:
    """Bars on ``day`` from (HH:MM, open, high, low, close, volume) rows."""
    return pd.DataFrame([{"Datetime": pd.Timestamp(f"{day} {hhmm}", tz=zone), "Open": o, "High": h, "Low": low,
                          "Close": c, "Volume": v} for hhmm, o, h, low, c, v in rows])


def _morning(volume: int = 100):
    """Ten flat 5m bars, 09:15 to 10:00 (the last ends 10:05)."""
    return [(f"{(555 + 5 * i) // 60:02d}:{(555 + 5 * i) % 60:02d}", 100, 101, 99, 100, volume) for i in range(10)]


# Tue 27 Oct: 1,500 shares. Wed 28 Oct, the last complete session: high 110,
# low 98, close 104, 2,000 shares. Thu 29 Oct, the plan's session, through 10:05.
TUESDAY_BARS = _bars("2026-10-27", _morning() + [("15:25", 100, 101, 99, 100, 500)])
WEDNESDAY_BARS = _bars("2026-10-28", _morning() + [
    ("11:00", 105, 110, 104, 109, 300), ("13:00", 100, 101, 98, 99, 300), ("15:25", 103, 104.5, 102.5, 104, 400)])
THURSDAY_BARS = _bars("2026-10-29", [
    # typical price (H+L+C)/3 in the comment
    ("09:15", 106.08, 107, 105, 106, 0),     # 106; NSE's first bar can carry no volume
    ("09:20", 106, 108, 106, 107, 200),      # 107
    ("09:25", 107, 109, 104, 105, 100),      # 106
    ("09:30", 105, 106, 102, 104, 300),      # 104
    ("09:35", 104, 105, 103, 104, 100),      # 104
    ("09:40", 104, 110, 104, 107, 200),      # 107
    ("09:45", 107, 110, 105, 109, 300),      # 108
    ("09:50", 109, 109, 107, 108, 100),      # 108
    ("09:55", 108, 108, 106, 107, 100),      # 107
    ("10:00", 107, 108, 106, 107, 200),      # 107
])
BARS = pd.concat([TUESDAY_BARS, WEDNESDAY_BARS, THURSDAY_BARS], ignore_index=True)


def _daily(end: str = "2026-10-28", rows: int = 20) -> pd.DataFrame:
    """Daily rows with a true range of 4 every day (load_ohlcv's shape: naive midnight dates)."""
    dates = pd.bdate_range(end=end, periods=rows)
    return pd.DataFrame({"Date": dates, "Open": 100.0, "High": 102.0, "Low": 98.0, "Close": 100.0, "Volume": 1})


# --- the session a plan is for -----------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("hh,mm,expected,phrase", [
    (8, 0, THURSDAY, "today's session, which opens at 09:15 IST"),          # before the pre-open
    (9, 5, THURSDAY, "today's session, which opens at 09:15 IST"),          # pre-open
    (11, 0, THURSDAY, "the rest of today's session, open until 15:30 IST"),
    (15, 45, FRIDAY, "today's session closed at 15:30 IST"),                # the closing session
    (20, 0, FRIDAY, "today's session closed at 15:30 IST"),
])
def test_a_plan_made_today_is_for_today_until_the_close(hh, mm, expected, phrase):
    day, why = target_session(NSE, THURSDAY, _at(THURSDAY, hh, mm))
    assert day == expected and phrase in why


@pytest.mark.unit
def test_a_holiday_or_a_weekend_plans_the_next_trading_day():
    diwali = date(2026, 11, 10)   # Diwali-Balipratipada, NSE/CMTR/71775
    day, why = target_session(NSE, diwali, _at(diwali, 11, 0))
    assert day == date(2026, 11, 11) and "an NSE holiday (Diwali-Balipratipada)" in why
    saturday = date(2026, 10, 31)
    assert target_session(BSE, saturday, _at(saturday, 11, 0)) == (
        date(2026, 11, 2), "the next trading day; today (Sat 31 Oct 2026) is a weekend")


@pytest.mark.unit
def test_a_past_date_plans_the_next_trading_day_after_it():
    assert target_session(NSE, date(2026, 10, 23), _at(THURSDAY, 11, 0)) == (
        date(2026, 10, 26), "the next trading day after the analysis date Fri 23 Oct 2026")
    # Dussehra (Tue 20 Oct) is skipped.
    assert target_session(NSE, date(2026, 10, 19), _at(THURSDAY, 11, 0))[0] == date(2026, 10, 21)


@pytest.mark.unit
def test_the_budget_sunday_is_a_session_of_its_own():
    day, why = target_session(NSE, date(2026, 1, 30), _at(THURSDAY, 11, 0))
    assert day == date(2026, 2, 1)
    assert "(Union Budget special session, 09:15-15:30 IST)" in why
    sunday = date(2026, 2, 1)
    day, why = target_session(NSE, sunday, _at(sunday, 10, 0))
    assert day == sunday and why.startswith("the rest of today's session (Union Budget special session")
    # The Muhurat Sunday opens at 14:20, not 09:15.
    muhurat = date(2026, 11, 8)
    assert "opens at 14:20 IST" in target_session(NSE, muhurat, _at(muhurat, 11, 0))[1]


@pytest.mark.unit
def test_a_listing_without_a_calendar_counts_weekdays_on_its_own_clock():
    hours = (time(9, 30), time(16, 0))
    # 20:00 in New York on Thursday: that session has closed.
    assert target_session(None, THURSDAY, _at(THURSDAY, 20, 0, NEW_YORK), tz=NEW_YORK, hours=hours)[0] == FRIDAY
    # 01:00 IST on Friday is 15:30 in New York on Thursday: a run dated by the
    # Indian calendar still plans the rest of New York's Thursday session.
    day, why = target_session(None, FRIDAY, _at(FRIDAY, 1, 0), tz="America/New_York", hours=hours)
    assert day == THURSDAY and "open until 16:00 America/New_York" in why
    assert target_session(None, date(2026, 10, 31), _at(date(2026, 10, 31), 12, 0, NEW_YORK),
                          tz=NEW_YORK, hours=hours)[0] == date(2026, 11, 2)
    assert target_session(None, date(2026, 10, 23), _at(THURSDAY, 12, 0, NEW_YORK), tz=NEW_YORK)[0] == date(2026, 10, 26)
    assert "hours are not known" in target_session(None, THURSDAY, _at(THURSDAY, 12, 0, NEW_YORK), tz=NEW_YORK)[1]


# --- square-off --------------------------------------------------------------------

@pytest.mark.unit
def test_square_off_is_the_configured_broker_time_on_nse_and_bse():
    assert DEFAULT_SQUARE_OFF == "15:10"
    assert square_off_time(NSE, {}) == time(15, 10)
    assert square_off_time(BSE, {"intraday_square_off": None}) == time(15, 10)
    assert square_off_time(NSE, {"intraday_square_off": "15:12"}) == time(15, 12)
    assert square_off_time(NSE, {"intraday_square_off": time(14, 55)}) == time(14, 55)
    assert square_off_time(None, {"intraday_square_off": "15:12"}) is None      # elsewhere: the session close
    with pytest.raises(ValueError, match="HH:MM"):
        square_off_time(NSE, {"intraday_square_off": "3:10 PM"})


# --- sessions and bar ends ---------------------------------------------------------

@pytest.mark.unit
def test_a_bar_ends_at_its_start_plus_the_interval_cut_at_the_close():
    muhurat = pd.DataFrame({"Datetime": pd.date_range("2026-11-08 14:20", periods=5, freq="15min", tz=IST)})
    ends = bar_ends(muhurat, "15m", NSE)
    assert ends.iloc[0] == pd.Timestamp("2026-11-08 14:35", tz=IST)
    assert ends.iloc[-1] == pd.Timestamp("2026-11-08 15:30", tz=IST)   # the 15:20 bar, cut at the 15:30 close
    assert bar_ends(muhurat, "15m").iloc[-1] == pd.Timestamp("2026-11-08 15:35", tz=IST)

    stray = pd.DataFrame({"Datetime": [pd.Timestamp(f"2026-10-29 {t}", tz=IST) for t in ("09:10", "09:15", "15:25", "15:30")]})
    assert in_regular_session(stray, NSE).tolist() == [False, True, True, False]


@pytest.mark.unit
def test_session_hours_are_read_from_completed_sessions_elsewhere():
    sessions = pd.concat([_bars(day, [("09:30", 1, 1, 1, 1, 1), ("12:00", 1, 1, 1, 1, 1), ("15:55", 1, 1, 1, 1, 1)],
                                NEW_YORK) for day in ("2026-10-27", "2026-10-28")])
    assert infer_session_hours(sessions, "5m") == (time(9, 30), time(16, 0))
    assert infer_session_hours(sessions.iloc[:0], "5m") is None


# --- levels ------------------------------------------------------------------------

@pytest.mark.unit
def test_levels_of_a_session_in_progress_against_hand_computed_values():
    levels = intraday_levels(BARS, _daily(), THURSDAY, _at(THURSDAY, 10, 7), interval="5m", exchange=NSE)

    assert levels["last_session"] == {"date": date(2026, 10, 28), "open": 100.0, "high": 110.0, "low": 98.0,
                                      "close": 104.0, "volume": 2000.0, "source": "5m bars"}
    # Classic floor pivots: P = (110 + 98 + 104) / 3 = 104.
    pivots = levels["pivots"]
    assert {k: pivots[k] for k in ("P", "R1", "R2", "R3", "S1", "S2", "S3")} == {
        "P": 104.0, "R1": 110.0, "R2": 116.0, "R3": 122.0, "S1": 98.0, "S2": 92.0, "S3": 86.0}

    current = levels["current"]
    assert (current["open"], current["high"], current["low"], current["last"], current["volume"]) == (
        106.08, 110.0, 102.0, 107.0, 1600.0)
    assert current["through"] == pd.Timestamp("2026-10-29 10:05", tz=IST)
    # VWAP = sum(typical x volume) / volume = 170,300 / 1,600.
    assert current["vwap"] == pytest.approx(106.4375)
    assert levels["last_vs_vwap_pct"] == pytest.approx(0.5284791, abs=1e-6)
    assert levels["gap_pct"] == pytest.approx(2.0)                       # 106.08 against 104
    assert levels["opening_range"][15] == {"high": 109.0, "low": 104.0, "start": pd.Timestamp("2026-10-29 09:15", tz=IST),
                                           "end": pd.Timestamp("2026-10-29 09:30", tz=IST)}
    assert (levels["opening_range"][30]["high"], levels["opening_range"][30]["low"]) == (110.0, 102.0)
    # 1,600 by 10:05 against the 1,000 each prior session had traded by then.
    assert (levels["relative_volume"], levels["relative_volume_sessions"]) == (pytest.approx(1.6), 2)
    assert levels["last_session_relative_volume"] == pytest.approx(2000 / 1500)
    assert levels["atr_intraday"] == pytest.approx(risk.average_true_range(BARS))
    assert (levels["reference_price"], levels["data_through"]) == (107.0, pd.Timestamp("2026-10-29 10:05", tz=IST))
    assert levels["sessions"] == [date(2026, 10, 27), date(2026, 10, 28), THURSDAY] and levels["target_started"]


@pytest.mark.unit
def test_volume_averages_leave_out_a_special_session_on_other_hours():
    # Muhurat trading on Sun 8 Nov 2026 (14:20-15:30) had not opened by Monday's 10:05, and its
    # hour of trading is no session's worth of volume. Every session here trades 1,000 by 10:05.
    regular = [_bars(day, _morning()) for day in ("2026-11-04", "2026-11-05", "2026-11-06")]
    muhurat = _bars("2026-11-08", [("14:20", 100, 101, 99, 100, 100), ("14:25", 100, 101, 99, 100, 100)])
    monday = date(2026, 11, 9)
    bars = pd.concat([*regular, muhurat, _bars("2026-11-09", _morning())], ignore_index=True)
    levels = intraday_levels(bars, None, monday, _at(monday, 10, 7), interval="5m", exchange=NSE)
    assert (levels["relative_volume"], levels["relative_volume_sessions"]) == (pytest.approx(1.0), 3)
    assert levels["last_session"]["date"] == date(2026, 11, 8)     # it is still the last session
    # Planning Wed 11 Nov (10 Nov is a holiday): Monday against the regular sessions before it.
    after = intraday_levels(bars, None, date(2026, 11, 11), _at(monday, 20, 0), interval="5m", exchange=NSE)
    assert after["last_session_relative_volume"] == pytest.approx(1.0)


@pytest.mark.unit
def test_an_opening_range_waits_until_the_bars_cover_it():
    early = intraday_levels(pd.concat([WEDNESDAY_BARS, THURSDAY_BARS.iloc[:4]]), None, THURSDAY,
                            _at(THURSDAY, 9, 36), interval="5m", exchange=NSE)
    assert early["opening_range"][15]["high"] == 109.0       # 09:15-09:30 is covered by the 09:30 bar's end
    assert early["opening_range"][30] is None                 # through 09:35 only


@pytest.mark.unit
def test_before_the_session_trades_only_completed_sessions_are_read():
    levels = intraday_levels(pd.concat([TUESDAY_BARS, WEDNESDAY_BARS]), _daily(), THURSDAY, _at(THURSDAY, 9, 5),
                             interval="5m", exchange=NSE)
    assert levels["current"] is None and not levels["target_started"]
    assert levels["opening_range"] == {15: None, 30: None}
    assert levels["gap_pct"] is None and levels["relative_volume"] is None and levels["last_vs_vwap_pct"] is None
    assert levels["pivots"]["P"] == 104.0 and levels["reference_price"] == 104.0
    assert levels["last_session_relative_volume"] == pytest.approx(2000 / 1500)


@pytest.mark.unit
def test_the_last_session_falls_back_to_the_daily_bar_when_the_bars_lack_it():
    daily = _daily()
    daily.loc[daily.index[-1], ["Open", "High", "Low", "Close"]] = [100.0, 110.0, 98.0, 104.0]
    levels = intraday_levels(THURSDAY_BARS, daily, THURSDAY, _at(THURSDAY, 10, 7), interval="5m", exchange=NSE)
    assert levels["last_session"]["source"] == "daily bar" and levels["last_session"]["date"] == date(2026, 10, 28)
    assert levels["pivots"]["P"] == 104.0 and levels["gap_pct"] == pytest.approx(2.0)
    assert levels["relative_volume"] is None                  # no prior session in the bars


@pytest.mark.unit
def test_atr_is_wilders_on_the_bars_and_on_daily_rows_before_the_plans_session():
    # Fifteen bars with a true range of 2, then one of 16: 2 + (16 - 2) / 14 = 3.
    calm = [(f"{(555 + 5 * i) // 60:02d}:{(555 + 5 * i) % 60:02d}", 100, 101, 99, 100, 10) for i in range(15)]
    bars = _bars("2026-10-29", calm + [("10:30", 100, 108, 92, 100, 10)])
    # Daily: fifteen ranges of 4 to 27 Oct, then 32 on 28 Oct. Today's row is
    # still forming (a range of 100), so it is not read.
    later = pd.DataFrame({"Date": pd.to_datetime(["2026-10-28", "2026-10-29"]), "Open": 100.0,
                          "High": [116.0, 150.0], "Low": [84.0, 50.0], "Close": 100.0, "Volume": 1})
    daily = pd.concat([_daily(end="2026-10-27", rows=15), later], ignore_index=True)
    levels = intraday_levels(bars, daily, THURSDAY, _at(THURSDAY, 10, 40), interval="5m", exchange=NSE)
    assert levels["atr_intraday"] == pytest.approx(3.0)
    # 4 + (32 - 4) / 14 = 6.
    assert levels["atr_daily"] == pytest.approx(6.0) and levels["atr_daily_through"] == date(2026, 10, 28)
    assert intraday_levels(bars.iloc[:14], None, THURSDAY, _at(THURSDAY, 10, 40), interval="5m")["atr_intraday"] is None


# --- sizing ------------------------------------------------------------------------

@pytest.mark.unit
def test_shares_follow_the_risk_budget():
    sized = intraday_position(100_000, 0.5, 100.0, 98.0, leverage_cap=1.0, profile="none", venue=None)
    assert (sized["direction"], sized["shares"], sized["risk_amount"], sized["capped_by"]) == ("long", 250, 500.0, None)
    assert (sized["exposure"], sized["margin"], sized["money_at_risk"], sized["charges"]) == (25_000.0, 25_000.0, 500.0, 0.0)
    short = intraday_position(100_000, 0.5, 100.0, 102.0, leverage_cap=1.0, profile=None, venue=None)
    assert (short["direction"], short["shares"]) == ("short", 250)


@pytest.mark.unit
def test_the_leverage_cap_limits_what_the_capital_buys():
    # Risking 1,000 with a 0.50 stop would buy 2,000 shares (200,000) on 100,000 of capital.
    capped = intraday_position(100_000, 1.0, 100.0, 99.5, leverage_cap=1.0, profile="none", venue=None)
    assert (capped["shares"], capped["shares_by_risk"], capped["capped_by"]) == (1000, 2000, "leverage cap")
    assert (capped["exposure"], capped["margin"], capped["money_at_risk"]) == (100_000.0, 100_000.0, 500.0)
    levered = intraday_position(100_000, 1.0, 100.0, 99.5, leverage_cap=5.0, profile="none", venue=None)
    assert (levered["shares"], levered["capped_by"], levered["margin"]) == (2000, None, 40_000.0)


@pytest.mark.unit
def test_charges_come_out_of_the_risk_budget():
    # Entry 1,000, stop 990, risking 500: 50 shares before charges. At 45 the
    # round trip (buy 45,000, sell 44,550) costs 18.96327 + 28.5746373 = 47.5379073,
    # a loss of 497.54 at the stop; at 46 it would be 460 + 48.59 = 508.59.
    sized = intraday_position(100_000, 0.5, 1000.0, 990.0, leverage_cap=1.0, profile="india_equity_intraday",
                              venue="NSE")
    assert sized["shares"] == 45 and sized["profile"] == "india_equity_intraday"
    assert sized["charges"] == pytest.approx(47.5379073)
    assert sized["money_at_risk"] == pytest.approx(497.5379073)
    profile = costs.get_cost_profile("india_equity_intraday")
    assert sized["charges"] == pytest.approx(costs.round_trip_costs(profile, 45_000, 44_550, "NSE"))
    assert 460 + costs.round_trip_costs(profile, 46_000, 45_540, "NSE") > 500
    bse = intraday_position(100_000, 0.5, 1000.0, 990.0, leverage_cap=1.0, profile=profile, venue="BSE")
    assert bse["charges"] == pytest.approx(costs.round_trip_costs(profile, 1000.0 * bse["shares"],
                                                                  990.0 * bse["shares"], "BSE"))


@pytest.mark.unit
def test_inputs_that_cannot_size_a_trade():
    assert intraday_position(100_000, 0.5, 100.0, 100.0, leverage_cap=1.0, profile="none", venue=None) is None
    assert intraday_position(0, 0.5, 100.0, 98.0, leverage_cap=1.0, profile="none", venue=None) is None
    assert intraday_position(100_000, 0, 100.0, 98.0, leverage_cap=1.0, profile="none", venue=None) is None
    assert intraday_position(100_000, 0.5, None, 98.0, leverage_cap=1.0, profile="none", venue=None) is None
    assert intraday_position(1_000, 0.5, 100.0, 90.0, leverage_cap=1.0, profile="none", venue=None)["shares"] == 0
    with pytest.raises(ValueError, match="leverage_cap"):
        intraday_position(100_000, 0.5, 100.0, 98.0, leverage_cap=0, profile="none", venue=None)


# --- the snapshot ------------------------------------------------------------------

@pytest.mark.unit
def test_the_snapshot_names_the_bars_behind_every_number():
    now = _at(THURSDAY, 10, 7)
    levels = intraday_levels(BARS, _daily(), THURSDAY, now, interval="5m", exchange=NSE)
    sizing = intraday_position(100_000, 0.5, 107.0, 105.0, leverage_cap=1.0, profile="india_equity_intraday",
                               venue="NSE")
    sizing["label"] = "Long at the newest close, stop 2 below"
    text = format_intraday_snapshot("RELIANCE.NS", levels, target_session(NSE, THURSDAY, now), time(15, 10),
                                    [sizing, None], ["A note."], recent_bars=BARS, fetched_at=now)

    assert text.startswith("# Intraday snapshot for RELIANCE.NS")
    assert ("Data: Yahoo Finance 5m bars (regular session), through Thu 29 Oct 2026 10:05 IST "
            "(fetched 10:07:00 IST); sessions in the bars: 27 Oct to 29 Oct 2026 (3)") in text
    assert "Plan for: Thu 29 Oct 2026, the rest of today's session, open until 15:30 IST." in text
    assert "Exit by: 15:10 IST" in text
    assert "## Last complete session: Wed 28 Oct 2026 (from the 5m bars)" in text
    assert "| VWAP | 106.438 |" in text and "| Gap at the open vs the previous close | +2.00% |" in text
    assert "| Opening range, first 15 minutes | high 109.00, low 104.00 |" in text
    assert "| 122.00 | 116.00 | 110.00 | 104.00 | 98.00 | 92.00 | 86.00 |" in text
    assert "₹1,00,000.00" in text and "'india_equity_intraday'" in text and "no order is placed" in text
    assert "| 29 Oct 10:00 | 107.00 | 108.00 | 106.00 | 107.00 | 200 |" in text
    assert "- A note." in text
    assert text.rstrip().endswith("tool's output.") and "source of truth for intraday prices" in text

    past = format_intraday_snapshot("RELIANCE.NS", levels, (FRIDAY, "the next trading day"), None, [], [])
    assert "fetched" not in past and "Exit by: the session close" in past
    assert "Unavailable: too few bars for an ATR-based stop" in past
