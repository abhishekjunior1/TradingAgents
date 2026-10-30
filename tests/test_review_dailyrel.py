"""Daily bars and relative performance, as reviewed after the first round of fixes (30 Oct 2026).

- R4: the market context gave no end date for the relative figures when the stock's series
  stopped before the index's, whose later close its table shows.
- R5: Yahoo's holiday filler was still served as a session when yfinance's dividend
  adjustment left its open, high and low an ulp away from its close.
- R6: a volume-weighted reading over sessions without volume said Yahoo reports none for
  the whole series, when only those sessions lacked it.
- R7: a cache test named a file whose name depends on today's date.

No test reaches Yahoo: its answers are stubbed and the clocks are fixed.
"""

from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

import tradingagents.dataflows.vendors.yahoo.market as market
from tradingagents.dataflows.vendors.yahoo import market_context, ohlcv, snapshot
from tradingagents.markets import exchanges

NOW = pd.Timestamp("2026-10-30 18:00")                 # the machine's clock (naive, local)
CLOSED = datetime(2026, 10, 30, 12, 30, tzinfo=UTC)    # 18:00 IST: NSE's 30 Oct close is final
AS_OF = "2026-10-28"
HOLIDAY = "2026-01-15"                                 # NSE: Municipal Corporation Election, Maharashtra
SERIES_NO_VOLUME = "N/A: no traded volume in this window (Yahoo reports none for this series)"
WINDOW_NO_VOLUME = ("N/A: no volume reported in the 14 sessions this reading weighs "
                    "(no trades, a suspension, or a gap in Yahoo's data)")


@pytest.fixture(autouse=True)
def clock(monkeypatch, tmp_path):
    """A fixed clock for the exchanges and for load_ohlcv, and a cache of the test's own."""
    monkeypatch.setattr(market, "session_status", lambda exchange: exchanges.session_status(exchange, now=CLOSED))
    monkeypatch.setattr(ohlcv.pd.Timestamp, "today", staticmethod(lambda: NOW))
    monkeypatch.setattr(ohlcv, "get_config", lambda: {"data_cache_dir": str(tmp_path)})


def _bars(days, volume=1_000_000) -> pd.DataFrame:
    """Daily bars for ``days``, as yfinance gives them: rising with a wobble, dated in the index."""
    dates = pd.DatetimeIndex(pd.to_datetime(list(days)), name="Date")
    steps = np.arange(len(dates))
    close = 100.0 + steps * 0.8 + np.sin(steps) * 1.5
    return pd.DataFrame({"Open": close - 0.5, "High": close + 1.0, "Low": close - 1.0, "Close": close,
                         "Volume": volume}, index=dates)


def _flat(dates, closes) -> pd.DataFrame:
    """Daily bars with every price at the close, as yfinance's Ticker.history returns them."""
    closes = [float(c) for c in closes]
    return pd.DataFrame({"Open": closes, "High": closes, "Low": closes, "Close": closes, "Volume": 1000},
                        index=pd.DatetimeIndex(dates, name="Date"))


def _nse_days(start: str, end: str) -> list[pd.Timestamp]:
    nse = exchanges.get_exchange("NSE")
    return [day for day in pd.date_range(start, end) if exchanges.is_trading_day(nse, day.date())]


def _serve(monkeypatch, frame: pd.DataFrame) -> None:
    """Have yf.Ticker serve ``frame``'s rows in the window asked for, dated in IST as yfinance dates them."""
    frame = frame.tz_localize("Asia/Kolkata")

    class Ticker:
        def __init__(self, symbol):
            pass

        def history(self, start=None, end=None, **kwargs):
            naive = frame.index.tz_localize(None)
            return frame[(naive >= pd.Timestamp(start)) & (naive < pd.Timestamp(end))].copy()

    monkeypatch.setattr(ohlcv.yf, "Ticker", Ticker)
    monkeypatch.setattr(market.yf, "Ticker", Ticker)


def _line(text: str, start: str) -> str:
    return next(line for line in text.splitlines() if line.startswith(start))


# --- R4: the relative figures' end when the stock stops first ---------------------------------

@pytest.fixture
def yahoo(monkeypatch):
    """Daily history by Yahoo symbol, read through the real load_ohlcv; a symbol not set is flat to AS_OF."""
    histories = {}

    class Ticker:
        def __init__(self, symbol):
            self.symbol = symbol

        def history(self, **kwargs):
            dates = pd.bdate_range(end=AS_OF, periods=300)
            return histories.get(self.symbol, _flat(dates, [100.0] * len(dates)))

    monkeypatch.setattr(ohlcv.yf, "Ticker", Ticker)
    monkeypatch.setattr(market_context, "_classification", lambda symbol: ("Energy", "Oil & Gas Refining & Marketing"))
    monkeypatch.setattr(market_context, "expected_last_session", lambda exchange, as_of: as_of)
    return histories


@pytest.mark.unit
@pytest.mark.parametrize("symbol,benchmark,name,as_of", [
    ("RELIANCE.NS", "^NSEI", "NIFTY 50", AS_OF),     # its 28 Oct bar not yet out, NIFTY 50's is
    ("AAPL", "SPY", "Benchmark", "2026-10-23"),      # a US listing against the configured benchmark
])
def test_relative_figures_say_when_the_stock_stops_before_the_index(yahoo, symbol, benchmark, name, as_of):
    days = pd.bdate_range(end=as_of, periods=300)
    yahoo[symbol] = _flat(days[:-1], [100.0] * (len(days) - 1))
    # The index rose 3% on the day the stock has no close for, which the figures leave out.
    yahoo[benchmark] = _flat(days, [100.0] * (len(days) - 1) + [103.0])
    stopped = f"(to {days[-2]:%d %b}, the last close both have)"

    text = market_context.get_market_context(symbol, as_of)
    assert f"| {benchmark} | 103.00 | {as_of} |" in text                # the later close the table shows
    assert f"- vs {name} ({benchmark}): 1M +0.0 pts, 3M +0.0 pts, 1Y +0.0 pts {stopped}" in text.splitlines()
    relative = [line for line in text.splitlines() if line.startswith("- vs ")]
    assert all(line.endswith(stopped) for line in relative)            # the sector index's too

    # A watchlist row shows the stock's own close date, which is where its figures stop.
    row = _line(market_context.get_watchlist_snapshot([symbol], as_of), f"| {symbol} |")
    assert f"| {days[-2]:%Y-%m-%d}" in row and "(to " not in row


# --- R5: a holiday filler the dividend adjustment left not quite flat ------------------------

def _adjusted_flat(raw: float, ratio: float) -> tuple[float, float]:
    """A flat raw price as yfinance's ``auto_adjust`` leaves it under a later dividend that makes
    Yahoo's Adj Close ``ratio`` x the close: (open, high and low; close).

    The open, high and low become raw x (Adj Close / raw), the close Adj Close itself, and in
    floating point the two can differ in the last bit.
    """
    raw = float(np.float32(raw))                      # Yahoo sends prices in single precision
    adjusted_close = float(np.float32(raw * ratio))
    return raw * (adjusted_close / raw), adjusted_close


@pytest.mark.unit
def test_a_holiday_filler_left_an_ulp_off_flat_by_the_dividend_adjustment_is_still_left_out(monkeypatch):
    others, close = _adjusted_flat(130.01, 0.97)
    assert others != close and others == pytest.approx(close, rel=1e-15)     # one ulp apart
    filler = pd.DataFrame({"Open": others, "High": others, "Low": others, "Close": close, "Volume": 0},
                          index=pd.DatetimeIndex([pd.Timestamp(HOLIDAY)], name="Date"))
    _serve(monkeypatch, pd.concat([_bars(_nse_days("2025-10-01", "2026-01-20")), filler]).sort_index())

    for fill_gaps in (True, False):
        dates = set(ohlcv.load_ohlcv("IOC.NS", "2026-01-20", fill_gaps=fill_gaps)["Date"].dt.strftime("%Y-%m-%d"))
        assert HOLIDAY not in dates and {"2026-01-14", "2026-01-16"} <= dates

    text = market.get_YFin_data_online("IOC.NS", "2026-01-12", "2026-01-20")
    assert f"\n{HOLIDAY}," not in text
    assert "# Note: left out Yahoo's filler for 15 Jan 2026 (Municipal Corporation Election, Maharashtra)" in text

    atr = market.get_stock_stats_indicators_window("IOC.NS", "atr", "2026-01-20", 8)
    assert _line(atr, f"{HOLIDAY}: ") == f"{HOLIDAY}: N/A: Not a trading day (Municipal Corporation Election, Maharashtra)"

    text = snapshot.build_verified_market_snapshot("IOC.NS", HOLIDAY)
    assert "- Latest trading row used: 2026-01-14\n" in text and "| Volume | 0 |" not in text

    closes = market.get_closes("IOC.NS", "2026-01-12", "2026-01-21")
    assert HOLIDAY not in {stamp.strftime("%Y-%m-%d") for stamp in closes.index}


# --- R6: no volume in the window, not in the series ------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("indicator", ["vwma", "mfi"])
def test_a_window_without_volume_is_not_reported_as_a_series_without_volume(monkeypatch, indicator):
    days = _nse_days("2026-03-02", "2026-10-29")
    # Volume on every session but the last 14: a suspension, or a run of days without a trade.
    frame = _bars(days, np.where(np.arange(len(days)) >= len(days) - 14, 0, 250_000)).reset_index()
    monkeypatch.setattr(market, "load_ohlcv", lambda symbol, as_of: frame.copy())
    monkeypatch.setattr(snapshot, "load_ohlcv", lambda symbol, as_of, fill_gaps=True: frame.copy())

    text = market.get_stock_stats_indicators_window("SOMESCRIP.NS", indicator, "2026-10-29", 1)
    assert _line(text, "2026-10-29: ") == f"2026-10-29: {WINDOW_NO_VOLUME}"
    assert not _line(text, "2026-10-28: ").split(": ", 1)[1].startswith("N/A")   # one traded session in its window
    assert market.get_stock_stats("SOMESCRIP.NS", indicator, "2026-10-29") == WINDOW_NO_VOLUME
    text = snapshot.build_verified_market_snapshot("SOMESCRIP.NS", "2026-10-29", indicators=(indicator,))
    assert f"| {indicator} | {WINDOW_NO_VOLUME} |" in text

    # A series Yahoo gives no volume at all, such as India VIX, is still said to have none.
    silent = _bars(days, 0).reset_index()
    monkeypatch.setattr(market, "load_ohlcv", lambda symbol, as_of: silent.copy())
    monkeypatch.setattr(snapshot, "load_ohlcv", lambda symbol, as_of, fill_gaps=True: silent.copy())
    assert market.get_stock_stats("^INDIAVIX", indicator, "2026-10-29") == SERIES_NO_VOLUME
    text = snapshot.build_verified_market_snapshot("^INDIAVIX", "2026-10-29", indicators=(indicator,))
    assert f"| {indicator} | {SERIES_NO_VOLUME} |" in text


# --- R7: the price-cache test and the real date -------------------------------------------------

@pytest.mark.unit
def test_the_price_cache_test_passes_whatever_the_date(monkeypatch, tmp_path):
    """From about 3 Dec 2029 the 5-year window no longer reaches 400 days before the test's
    6 Jan 2026, and load_ohlcv names its file for the year it downloads from instead."""
    from tests import test_cache_writes

    monkeypatch.setattr(pd.Timestamp, "today", staticmethod(lambda: pd.Timestamp("2030-01-01 12:00")))
    test_cache_writes.test_the_price_cache_is_replaced_whole(monkeypatch, tmp_path)
