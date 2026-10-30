"""Daily prices and indicators for an NSE/BSE listing say what they are: a row of a session
still trading, history the vendor lacks, and dates that were not trading days. Found live on
30 Oct 2026, when Yahoo served RELIANCE.BO one daily bar and the indicator tool read it as an
RSI of 50.0 with every October weekday "Not a trading day (weekend or holiday)"."""

from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

import tradingagents.dataflows.vendors.yahoo.market as yfin
from tradingagents.markets import exchanges

TRADING = datetime(2026, 10, 30, 8, 0, tzinfo=UTC)    # Fri 30 Oct 2026, 13:30 IST: NSE and BSE are open
CLOSED = datetime(2026, 10, 30, 11, 0, tzinfo=UTC)    # 16:30 IST: the day's close is final


@pytest.fixture
def clock(monkeypatch):
    """The exchanges' session state at a fixed moment; call it again to move the clock."""
    def at(now):
        monkeypatch.setattr(yfin, "session_status", lambda exchange: exchanges.session_status(exchange, now=now))
    at(TRADING)
    return at


def _daily(days, start: float = 100.0) -> pd.DataFrame:
    """Daily rows for ``days`` in load_ohlcv's shape, rising with a wobble so every indicator has data."""
    dates = pd.to_datetime(list(days))
    steps = np.arange(len(dates))
    close = start + steps * 0.8 + np.sin(steps) * 1.5
    return pd.DataFrame({"Date": dates, "Open": close - 0.5, "High": close + 1.0, "Low": close - 1.0,
                         "Close": close, "Volume": 1_000_000})


def _yahoo(days):
    """A yf.Ticker stand-in whose history holds rows for ``days``."""
    frame = _daily(days).set_index("Date")
    frame.index.name = "Date"

    class Ticker:
        def __init__(self, symbol):
            pass

        def history(self, start, end):
            return frame.copy()

    return Ticker


def _line(text: str, day: str) -> str:
    return next(line for line in text.splitlines() if line.startswith(f"{day}: "))


@pytest.mark.unit
def test_one_bar_of_history_gives_no_indicator_value_and_each_missing_day_says_why(monkeypatch, clock):
    monkeypatch.setattr(yfin, "load_ohlcv", lambda symbol, as_of: _daily(["2026-10-30"]))
    text = yfin.get_stock_stats_indicators_window("RELIANCE.NS", "rsi", "2026-10-30", 12)

    assert _line(text, "2026-10-30") == ("2026-10-30: N/A: not enough price history "
                                         "(rsi needs 15 sessions; the data has 1 through this date)")
    assert "50.0" not in text
    assert _line(text, "2026-10-29") == "2026-10-29: N/A: no price data from the vendor for this NSE trading day"
    assert _line(text, "2026-10-25") == "2026-10-25: N/A: Not a trading day (weekend)"
    assert _line(text, "2026-10-20") == "2026-10-20: N/A: Not a trading day (Dussehra)"


@pytest.mark.unit
@pytest.mark.parametrize("indicator,need", [("close_50_sma", 50), ("macds", 34), ("boll", 20), ("atr", 15)])
def test_each_indicator_waits_for_its_own_window(monkeypatch, clock, indicator, need):
    days = pd.bdate_range(end="2026-10-30", periods=need)
    monkeypatch.setattr(yfin, "load_ohlcv", lambda symbol, as_of: _daily(days))
    text = yfin.get_stock_stats_indicators_window("TCS.NS", indicator, "2026-10-30", 3)
    first, second = days[-2].strftime("%Y-%m-%d"), days[-1].strftime("%Y-%m-%d")

    assert f"{indicator} needs {need} sessions; the data has {need - 1} through this date" in _line(text, first)
    assert not _line(text, second).split(": ", 1)[1].startswith("N/A")


@pytest.mark.unit
def test_todays_value_is_provisional_only_while_the_session_trades(monkeypatch, clock):
    monkeypatch.setattr(yfin, "load_ohlcv", lambda symbol, as_of: _daily(pd.bdate_range(end="2026-10-30", periods=60)))
    text = yfin.get_stock_stats_indicators_window("TCS.NS", "rsi", "2026-10-30", 3)

    assert _line(text, "2026-10-30").endswith(
        " (provisional: NSE is still trading at 13:30 IST; today's bar is not a close)")
    assert "provisional" not in _line(text, "2026-10-29")
    clock(CLOSED)
    assert "provisional" not in yfin.get_stock_stats_indicators_window("TCS.NS", "rsi", "2026-10-30", 3)


@pytest.mark.unit
def test_a_listing_without_an_exchange_calendar_does_not_call_a_missing_weekday_a_holiday(monkeypatch, clock):
    days = [day for day in pd.bdate_range(end="2026-10-29", periods=60) if day != pd.Timestamp("2026-10-26")]
    monkeypatch.setattr(yfin, "load_ohlcv", lambda symbol, as_of: _daily(days))
    text = yfin.get_stock_stats_indicators_window("AAPL", "rsi", "2026-10-29", 5)

    assert _line(text, "2026-10-26") == ("2026-10-26: N/A: no data for this date "
                                         "(a market holiday or a gap in the vendor's data)")
    assert _line(text, "2026-10-25") == "2026-10-25: N/A: Not a trading day (weekend)"
    assert "provisional" not in text


@pytest.mark.unit
def test_the_per_day_path_says_the_same(monkeypatch, clock):
    monkeypatch.setattr(yfin, "load_ohlcv", lambda symbol, as_of: _daily(["2026-10-29", "2026-10-30"]))

    assert yfin.get_stock_stats("RELIANCE.NS", "rsi", "2026-10-30") == (
        "N/A: not enough price history (rsi needs 15 sessions; the data has 2 through this date)")
    assert yfin.get_stock_stats("RELIANCE.NS", "rsi", "2026-10-20") == "N/A: Not a trading day (Dussehra)"


@pytest.mark.unit
def test_stock_data_names_a_row_still_trading_and_the_trading_days_yahoo_lacks(monkeypatch, clock):
    monkeypatch.setattr(yfin.yf, "Ticker", _yahoo(["2026-10-30"]))
    text = yfin.get_YFin_data_online("RELIANCE.BO", "2026-10-01", "2026-10-30")
    header = text.split("\n\n")[0]
    bse = exchanges.get_exchange("BSE")
    expected = sum(exchanges.is_trading_day(bse, day.date()) for day in pd.date_range("2026-10-01", "2026-10-29"))

    assert ("# Note: the 2026-10-30 row is BSE's session still trading at 13:30 IST: its Close is the latest "
            "price, not a close (final after 16:00 IST)") in header
    assert (f"# Note: no row for {expected} of the {expected} BSE trading days in this range (01 Oct 2026, "
            in header)
    assert "not holidays" in header and "\n\nDate," in text


@pytest.mark.unit
def test_a_complete_closed_range_has_no_notes(monkeypatch, clock):
    clock(CLOSED)
    nse = exchanges.get_exchange("NSE")
    days = [day for day in pd.date_range("2026-10-01", "2026-10-30") if exchanges.is_trading_day(nse, day.date())]
    monkeypatch.setattr(yfin.yf, "Ticker", _yahoo(days))

    assert "# Note" not in yfin.get_YFin_data_online("TCS.NS", "2026-10-01", "2026-10-30")
