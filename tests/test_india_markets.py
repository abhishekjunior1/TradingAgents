"""Indian market support at its base: tickers, exchanges, sessions, indices, money and risk.

Every case here is pure: no network, no vendor library. A session is judged at
a fixed moment, and holidays are given explicitly, so nothing depends on today.
"""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from tradingagents.dataflows.symbols import normalize_symbol, resolve_symbol, safe_ticker_component
from tradingagents.markets import exchanges, india, risk
from tradingagents.markets.exchanges import BSE, NSE, exchange_for_symbol, session_status
from tradingagents.markets.money import (
    format_inr,
    format_inr_compact,
    format_money,
    format_percent,
    indian_grouping,
)

IST = ZoneInfo("Asia/Kolkata")


# --- tickers ---------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("typed,yahoo", [
    ("RELIANCE.NS", "RELIANCE.NS"), ("reliance.ns", "RELIANCE.NS"), ("TCS.BO", "TCS.BO"),
    ("NSE:RELIANCE", "RELIANCE.NS"), ("nse: infy", "INFY.NS"), ("BSE:RELIANCE", "RELIANCE.BO"),
    ("BOM:TCS", "TCS.BO"), ("RELIANCE.NSE", "RELIANCE.NS"), ("RELIANCE.BSE", "RELIANCE.BO"),
    ("RELIANCE-EQ", "RELIANCE.NS"), ("M&M.NS", "M&M.NS"), ("BAJAJ-AUTO.NS", "BAJAJ-AUTO.NS"),
])
def test_indian_listings_take_yahoos_suffix_however_they_are_written(typed, yahoo):
    assert normalize_symbol(typed) == yahoo


@pytest.mark.unit
@pytest.mark.parametrize("typed,yahoo", [
    ("NIFTY", "^NSEI"), ("Nifty 50", "^NSEI"), ("NIFTY50", "^NSEI"), ("BANKNIFTY", "^NSEBANK"),
    ("Nifty Bank", "^NSEBANK"), ("FINNIFTY", "NIFTY_FIN_SERVICE.NS"),
    ("NIFTY FINANCIAL SERVICES", "NIFTY_FIN_SERVICE.NS"), ("NIFTY IT", "^CNXIT"), ("SENSEX", "^BSESN"),
    ("India VIX", "^INDIAVIX"), ("NSE:NIFTY", "^NSEI"), ("^NSEI", "^NSEI"), ("nifty next 50", "^NSMIDCP"),
])
def test_indian_index_names_resolve_to_yahoo_index_symbols(typed, yahoo):
    assert normalize_symbol(typed) == yahoo


@pytest.mark.unit
@pytest.mark.parametrize("typed,yahoo", [
    ("AAPL", "AAPL"), ("XAUUSD", "GC=F"), ("BTCUSD", "BTC-USD"), ("700.HK", "0700.HK"),
    ("^GSPC", "^GSPC"), ("BRK-B", "BRK-B"), ("USDINR", "USDINR=X"), ("600519.SH", "600519.SS"),
])
def test_other_markets_resolve_as_before(typed, yahoo):
    assert normalize_symbol(typed) == yahoo


@pytest.mark.unit
@pytest.mark.parametrize("typed,exchange,yahoo", [
    ("RELIANCE", "NSE", "RELIANCE.NS"), ("TCS", "bse", "TCS.BO"), ("M&M", "nse", "M&M.NS"),
    ("BAJAJ-AUTO", "NSE", "BAJAJ-AUTO.NS"), ("3MINDIA", "NSE", "3MINDIA.NS"),
    # A symbol that names its market keeps it.
    ("RELIANCE.BO", "NSE", "RELIANCE.BO"), ("NIFTY", "NSE", "^NSEI"), ("BTCUSD", "NSE", "BTC-USD"),
    ("USDINR", "NSE", "USDINR=X"), ("GOLD", "NSE", "GC=F"),
    # No default exchange: a bare symbol stays bare, as before.
    ("AAPL", None, "AAPL"), ("RELIANCE", None, "RELIANCE"),
])
def test_a_default_exchange_lists_a_bare_symbol(typed, exchange, yahoo):
    assert resolve_symbol(typed, exchange) == yahoo


@pytest.mark.unit
@pytest.mark.parametrize("typed,exchange", [("500325", "BSE"), ("500325.BO", None), ("BSE:500325", None)])
def test_bse_scrip_codes_are_refused_with_the_fix(typed, exchange):
    # Yahoo returns no data for a BSE scrip code (checked 2026-10-29); it serves RELIANCE.BO.
    with pytest.raises(ValueError, match="RELIANCE.BO"):
        resolve_symbol(typed, exchange)


@pytest.mark.unit
def test_an_unknown_exchange_is_named_in_the_error():
    with pytest.raises(ValueError, match="NSE, BSE"):
        resolve_symbol("RELIANCE", "LSE")


@pytest.mark.unit
def test_an_ampersand_listing_is_safe_in_a_path_and_traversal_still_is_not():
    assert safe_ticker_component("M&M.NS") == "M&M.NS"
    for bad in ("../etc", "M&M NS", "a/b", ".."):
        with pytest.raises(ValueError):
            safe_ticker_component(bad)


# --- exchanges and sessions ------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("symbol,code", [
    ("RELIANCE.NS", "NSE"), ("RELIANCE.BO", "BSE"), ("^NSEI", "NSE"), ("^BSESN", "BSE"),
    ("NIFTY_FIN_SERVICE.NS", "NSE"), ("^INDIAVIX", "NSE"), ("AAPL", None), ("^GSPC", None), ("", None),
])
def test_the_exchange_of_a_symbol(symbol, code):
    exchange = exchange_for_symbol(symbol)
    assert (exchange.code if exchange else None) == code


@pytest.mark.unit
def test_nse_and_bse_trade_in_inr_on_india_standard_time():
    for exchange in (NSE, BSE):
        assert (exchange.currency, exchange.timezone) == ("INR", "Asia/Kolkata")
        assert exchange.session_hours() == "09:15-15:30 IST"


def _at(day: date, hh: int, mm: int) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=IST)


THURSDAY = date(2031, 10, 30)   # a year no holiday list covers, so only weekends are known


@pytest.mark.unit
@pytest.mark.parametrize("hh,mm,state,reason", [
    (8, 30, "closed", "before the pre-open session"),
    (9, 5, "pre-open", "pre-open order session"),
    (9, 15, "open", "regular session"),
    (15, 29, "open", "regular session"),
    # The close is final only after the closing auction and post-close session.
    (15, 30, "closing", "closing auction and post-close session"),
    (15, 59, "closing", "closing auction and post-close session"),
    (16, 0, "closed", "after the close"),
])
def test_a_weekday_session_by_the_clock(hh, mm, state, reason):
    status = session_status(NSE, _at(THURSDAY, hh, mm))
    assert (status.state, status.reason) == (state, reason)
    assert status.session_complete is (state == "closed" and reason == "after the close")


@pytest.mark.unit
@pytest.mark.parametrize("day,name", [
    (date(2026, 1, 26), "Republic Day"), (date(2026, 10, 20), "Dussehra"),
    (date(2026, 11, 10), "Diwali-Balipratipada"), (date(2026, 1, 15), "Municipal Corporation Election, Maharashtra"),
])
def test_the_2026_holidays_from_nses_circulars_close_both_exchanges(day, name):
    for exchange in (NSE, BSE):
        status = session_status(exchange, _at(day, 11, 0))
        assert (status.state, status.reason) == ("closed", f"exchange holiday: {name}")
        assert status.calendar_known


@pytest.mark.unit
def test_the_2026_sunday_sessions_trade_with_their_own_hours():
    budget = session_status(NSE, _at(date(2026, 2, 1), 10, 0))
    assert (budget.state, budget.reason) == ("open", "Union Budget special session")
    muhurat = date(2026, 11, 8)
    assert session_status(NSE, _at(muhurat, 11, 0)).state == "closed"           # before its 14:05 pre-open
    assert session_status(NSE, _at(muhurat, 14, 10)).state == "pre-open"
    assert session_status(NSE, _at(muhurat, 14, 30)).reason == "Muhurat trading (Diwali)"
    # The Monday after the Muhurat Sunday reaches back to it, not to Friday.
    assert exchanges.previous_trading_day(NSE, date(2026, 11, 9)) == muhurat
    assert "special session (Muhurat trading (Diwali), 14:20-15:30 IST)" in exchanges.day_status(NSE, muhurat)


@pytest.mark.unit
def test_an_exchange_holiday_is_skipped_when_finding_the_last_session():
    # Mon 19 Oct 2026, a trading day, before the Dussehra holiday on Tue 20 Oct.
    assert session_status(NSE, _at(date(2026, 10, 21), 11, 0)).last_complete_session == date(2026, 10, 19)


@pytest.mark.unit
def test_the_latest_complete_session_is_today_only_after_the_close():
    assert session_status(NSE, _at(THURSDAY, 11, 0)).last_complete_session == date(2031, 10, 29)
    assert session_status(NSE, _at(THURSDAY, 16, 0)).last_complete_session == THURSDAY


@pytest.mark.unit
def test_a_utc_moment_is_judged_in_ist():
    # 04:00 UTC is 09:30 IST: open in Mumbai while it is still night in New York.
    status = session_status(NSE, datetime(2031, 10, 30, 4, 0, tzinfo=UTC))
    assert status.state == "open" and status.local_time.utcoffset().total_seconds() == 5.5 * 3600
    # A naive time is taken as UTC, not as the machine's local time.
    assert session_status(NSE, datetime(2031, 10, 30, 4, 0)).state == "open"


@pytest.mark.unit
def test_a_weekend_and_a_holiday_are_closed_and_skipped():
    saturday = date(2031, 11, 1)
    status = session_status(NSE, _at(saturday, 11, 0))
    assert (status.state, status.reason, status.last_complete_session) == ("closed", "weekend", date(2031, 10, 31))
    assert status.next_open == date(2031, 11, 3)

    holidays = {"2031-11-03": "Test holiday"}
    monday = session_status(NSE, _at(date(2031, 11, 3), 11, 0), extra=holidays)
    assert (monday.state, monday.reason) == ("closed", "exchange holiday: Test holiday")
    assert monday.next_open == date(2031, 11, 4)
    assert exchanges.previous_trading_day(NSE, date(2031, 11, 4), holidays) == date(2031, 10, 31)


@pytest.mark.unit
def test_a_year_the_holiday_list_does_not_cover_is_said_so():
    text = session_status(NSE, _at(THURSDAY, 11, 0)).describe()
    assert "does not cover 2031" in text and "still forming" in text
    assert "09:15-15:30 IST" in text


@pytest.mark.unit
def test_a_past_day_is_described_as_trading_weekend_or_holiday():
    assert "weekend" in exchanges.day_status(NSE, date(2031, 11, 1))
    assert "holiday (Test holiday)" in exchanges.day_status(NSE, date(2031, 11, 3), {"2031-11-03": "Test holiday"})


# --- indices -----------------------------------------------------------------------

@pytest.mark.unit
def test_index_aliases_are_unique_and_point_at_listed_symbols():
    seen = {}
    for index in india.INDICES:
        for alias in index.aliases:
            assert alias not in seen, f"{alias} names both {seen[alias]} and {index.name}"
            seen[alias] = index.name
        assert index.exchange in exchanges.EXCHANGES


@pytest.mark.unit
@pytest.mark.parametrize("sector,industry,symbol", [
    ("Technology", "Information Technology Services", "^CNXIT"),
    ("Financial Services", "Banks - Regional", "^NSEBANK"),
    ("Financial Services", "Insurance - Life", "NIFTY_FIN_SERVICE.NS"),
    ("Energy", "Oil & Gas Refining & Marketing", "^CNXENERGY"),
    ("Basic Materials", "Steel", "^CNXMETAL"),
    ("Consumer Cyclical", "Auto Manufacturers", "^CNXAUTO"),
    ("Healthcare", "Drug Manufacturers - Specialty & Generic", "^CNXPHARMA"),
    ("Communication Services", "Telecom Services", None),
    (None, None, None),
])
def test_a_company_maps_to_its_closest_sector_index(sector, industry, symbol):
    index = india.sector_index_for(sector, industry)
    assert (index.symbol if index else None) == symbol
    assert index is None or index.history   # never a series Yahoo has no history for


@pytest.mark.unit
def test_the_briefing_states_rules_and_what_is_unavailable_without_figures():
    text = india.market_briefing("NSE", on=date(2026, 10, 29))
    for rule in ("INR", "IST", "T+1", "price bands", "10%, 15% or 20%", "45 days", "RBI", "Union Budget",
                 "SEBI", "FII", "crude", "unavailable"):
        assert rule in text
    assert "BSE" in text   # the other venue is named
    assert "closing auction 15:15-15:35" in text                  # the rules in force on that date
    assert "next scheduled meeting is 02-04 Dec 2026" in text


@pytest.mark.unit
def test_the_briefing_names_no_rule_or_meeting_from_after_the_run():
    import re

    for day in (date(2025, 6, 2), date(2026, 3, 1)):
        text = india.market_briefing("NSE", on=day)
        assert "closing auction" not in text and "next scheduled meeting" not in text
        assert all(found <= day.isoformat() for found in re.findall(r"\d{4}-\d{2}-\d{2}", text))
    assert "closing auction 15:30-15:40" in india.market_briefing("NSE", on=date(2026, 12, 7))


# --- money -------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("value,text", [
    (0, "0"), (999, "999"), (1000, "1,000"), (100000, "1,00,000"), (12345678, "1,23,45,678"),
    (-1234567, "-12,34,567"),
])
def test_indian_digit_grouping(value, text):
    assert indian_grouping(value) == text


@pytest.mark.unit
def test_inr_amounts_in_full_and_in_lakh_or_crore():
    assert format_inr(1234567.891) == "₹12,34,567.89"
    assert format_inr(-2500) == "-₹2,500.00"
    assert format_inr_compact(123456789) == "₹12.35 crore"
    assert format_inr_compact(12349999999) == "₹1,235.00 crore"   # rounding carries into the whole part
    assert format_inr_compact(250000) == "₹2.50 lakh"
    assert format_inr_compact(999.5) == "₹999.50"
    assert format_inr(float("nan")) == "n/a" and format_inr(None) == "n/a"


@pytest.mark.unit
def test_other_currencies_keep_international_grouping():
    assert format_money(1500, "USD") == "1,500.00 USD"
    assert format_money(1500000, "INR") == "₹15,00,000.00"
    assert format_money(1500000, "inr", compact=True) == "₹15.00 lakh"
    assert format_percent(0.0123) == "+1.23%" and format_percent(None) == "n/a"


# --- risk --------------------------------------------------------------------------

def _series(values, start="2026-01-01"):
    return pd.Series(values, index=pd.bdate_range(start, periods=len(values)), dtype=float)


@pytest.mark.unit
def test_drawdown_return_and_volatility():
    prices = _series([100, 120, 90, 110])
    drawdown = risk.max_drawdown(prices)
    assert drawdown.depth == pytest.approx(-0.25)
    assert (drawdown.peak, drawdown.trough) == (prices.index[1], prices.index[2])
    assert risk.total_return(prices) == pytest.approx(0.10)
    assert risk.total_return(prices, sessions=1) == pytest.approx(110 / 90 - 1)
    assert risk.total_return(prices, sessions=10) is None          # too short to say
    assert risk.annualized_volatility(prices) is None               # under 20 returns
    steady = _series([100 * 1.001 ** i for i in range(40)])
    assert risk.annualized_volatility(steady) == pytest.approx(0.0, abs=1e-9)


@pytest.mark.unit
def test_beta_and_relative_return_on_aligned_days():
    import numpy as np

    rng = np.random.default_rng(7)
    bench_returns = rng.normal(0, 0.01, 120)
    bench = _series(100 * np.cumprod(1 + bench_returns))
    asset = _series(100 * np.cumprod(1 + 2 * bench_returns))
    assert risk.beta(asset, bench) == pytest.approx(2.0, rel=1e-6)
    assert risk.beta(asset.iloc[:30], bench.iloc[:30]) is None      # too few shared days
    assert risk.relative_return(asset, bench, 20) == pytest.approx(
        risk.total_return(asset, 20) - risk.total_return(bench, 20))


@pytest.mark.unit
def test_risk_reward_and_position_sizing():
    assert risk.risk_reward(100, 95, 110) == pytest.approx(2.0)
    assert risk.risk_reward(100, 105, 90) == pytest.approx(2.0)     # a short
    assert risk.risk_reward(100, 95, 98) == 0.0                     # target on the wrong side
    assert risk.risk_reward(100, 100, 110) is None

    size = risk.position_size(capital=1_000_000, risk_fraction=0.01, entry=100, stop=95)
    assert (size.quantity, size.capital_at_risk, size.capped_by) == (2000, 10_000, None)
    capped = risk.position_size(1_000_000, 0.01, 100, 95, max_position_fraction=0.10)
    assert (capped.quantity, capped.position_value, capped.capped_by) == (1000, 100_000, "max position")
    assert risk.position_size(1_000_000, 0.01, 100, 100) is None


@pytest.mark.unit
def test_concentration_and_sector_exposure():
    book = {"RELIANCE.NS": 75_000, "TCS.NS": 25_000, "SHORT.NS": -10_000}
    found = risk.concentration(book)
    assert (found.largest, found.largest_weight) == ("RELIANCE.NS", pytest.approx(0.75))
    assert found.hhi == pytest.approx(0.625) and found.effective_holdings == pytest.approx(1.6)
    assert risk.exposure_by(book, {"RELIANCE.NS": "Energy"}) == {
        "Energy": pytest.approx(0.75), "Unclassified": pytest.approx(0.25)}
    assert risk.concentration({}) is None
