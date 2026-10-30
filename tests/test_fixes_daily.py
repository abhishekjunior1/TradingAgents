"""Daily bars as the price tools, indicators and verified snapshot serve them (found on 30 Oct 2026).

- Yahoo fills a day NSE was closed with a flat, volume-less row (RELIANCE.NS on the 15 Jan 2026
  holiday); it was served as a session.
- A date before the 5-year download read as "no closing prices", a holiday, or a delisted symbol.
- The daily prices are Yahoo's dividend-adjusted series, and nothing said so.
- VWMA and MFI printed 0.0 and 0.5 for India VIX, which Yahoo gives no volume, and MFI came on
  stockstats' 0-1 scale under guidance written for 0-100.
"""

import os
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

import tradingagents.dataflows.vendors.yahoo.market as market
from tradingagents.dataflows.vendors.yahoo import market_context, ohlcv, snapshot
from tradingagents.markets import exchanges

NOW = pd.Timestamp("2026-10-30 18:00")                 # the machine's clock (naive, local)
CLOSED = datetime(2026, 10, 30, 12, 30, tzinfo=UTC)    # 18:00 IST: NSE's 30 Oct close is final
HOLIDAY = "2026-01-15"                                 # NSE: Municipal Corporation Election, Maharashtra
NO_VOLUME = "N/A: no traded volume in this window (Yahoo reports none for this series)"


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


def _with_filler(frame: pd.DataFrame, day: str) -> pd.DataFrame:
    """``frame`` plus Yahoo's filler on ``day``: the previous close as all four prices, no volume."""
    before = frame[frame.index < pd.Timestamp(day)].iloc[-1]["Close"]
    row = pd.DataFrame({"Open": before, "High": before, "Low": before, "Close": before, "Volume": 0},
                       index=pd.DatetimeIndex([pd.Timestamp(day)], name="Date"))
    return pd.concat([frame, row]).sort_index()


def _nse_days(start: str, end: str) -> list[pd.Timestamp]:
    nse = exchanges.get_exchange("NSE")
    return [day for day in pd.date_range(start, end) if exchanges.is_trading_day(nse, day.date())]


class _Yahoo:
    """A yf.Ticker stand-in: ``history`` serves the frame's rows in the requested window,
    dated in the listing's time zone ``tz`` as yfinance dates them, and records each request."""

    def __init__(self, frame: pd.DataFrame, tz: str | None = "Asia/Kolkata"):
        self.frame = frame.tz_localize(tz) if tz else frame
        self.requests = []

    def __call__(self, symbol):
        yahoo = self

        class Ticker:
            def history(self, start=None, end=None, **kwargs):
                yahoo.requests.append({"symbol": symbol, "start": start, "end": end, **kwargs})
                naive = yahoo.frame.index.tz_localize(None) if yahoo.frame.index.tz else yahoo.frame.index
                keep = (naive >= pd.Timestamp(start)) & (naive < pd.Timestamp(end))
                return yahoo.frame[keep].copy()

        return Ticker()


def _serve(monkeypatch, frame, tz="Asia/Kolkata") -> _Yahoo:
    yahoo = _Yahoo(frame, tz)
    monkeypatch.setattr(ohlcv.yf, "Ticker", yahoo)
    monkeypatch.setattr(market.yf, "Ticker", yahoo)
    return yahoo


def _line(text: str, day: str) -> str:
    return next(line for line in text.splitlines() if line.startswith(f"{day}: "))


def _stamp(path, ts: pd.Timestamp) -> None:
    """Give a cache file the machine-local mtime ``ts``, as if written then."""
    t = ts.to_pydatetime().timestamp()
    os.utime(path, (t, t))


# --- Yahoo's filler for a closed day (finding 4) -------------------------------------------

def _reliance_with_holiday_filler() -> pd.DataFrame:
    return _with_filler(_bars(_nse_days("2025-10-01", "2026-01-20")), HOLIDAY)


@pytest.mark.unit
def test_load_ohlcv_leaves_out_the_filler_for_an_nse_holiday(monkeypatch):
    _serve(monkeypatch, _reliance_with_holiday_filler())

    for fill_gaps in (True, False):
        dates = set(ohlcv.load_ohlcv("RELIANCE.NS", "2026-01-20", fill_gaps=fill_gaps)["Date"].dt.strftime("%Y-%m-%d"))
        assert HOLIDAY not in dates and {"2026-01-14", "2026-01-16"} <= dates


@pytest.mark.unit
def test_rows_that_are_not_filler_are_kept(monkeypatch):
    # A row that traded on a day outside the calendar (NSE's Saturday budget session of 1 Feb
    # 2025), a flat volume-less bar on a trading day (an illiquid listing's day without trades),
    # and the same filler for a listing without an exchange calendar here.
    days = _nse_days("2025-01-01", "2026-03-30") + [pd.Timestamp("2025-02-01")]
    frame = _with_filler(_bars(sorted(days)).drop(pd.Timestamp("2026-03-05")), "2026-03-05")
    _serve(monkeypatch, frame)
    dates = set(ohlcv.load_ohlcv("SOMESCRIP.BO", "2026-03-30")["Date"].dt.strftime("%Y-%m-%d"))
    assert {"2025-02-01", "2026-03-05"} <= dates

    _serve(monkeypatch, _reliance_with_holiday_filler(), tz="America/New_York")
    assert HOLIDAY in set(ohlcv.load_ohlcv("AAPL", "2026-01-20")["Date"].dt.strftime("%Y-%m-%d"))


@pytest.mark.unit
def test_stock_data_leaves_out_the_filler_and_says_so(monkeypatch):
    _serve(monkeypatch, _reliance_with_holiday_filler())
    text = market.get_YFin_data_online("RELIANCE.NS", "2026-01-12", "2026-01-20")
    header = text.split("\n\n")[0]

    assert f"\n{HOLIDAY}," not in text and "\n2026-01-14," in text and "\n2026-01-16," in text
    assert "# Total records: 6" in header
    assert ("# Note: left out Yahoo's filler for 15 Jan 2026 (Municipal Corporation Election, Maharashtra), "
            "when NSE was closed: one price as open, high, low and close and no volume, not a session") in header


@pytest.mark.unit
def test_stock_data_for_the_holiday_alone_has_no_rows_and_says_why(monkeypatch):
    _serve(monkeypatch, _reliance_with_holiday_filler())
    text = market.get_YFin_data_online("RELIANCE.NS", HOLIDAY, HOLIDAY)

    assert "# Total records: 0" in text
    assert "left out Yahoo's filler for 15 Jan 2026 (Municipal Corporation Election, Maharashtra)" in text


@pytest.mark.unit
def test_indicators_read_the_holiday_as_not_a_trading_day_and_skip_its_bar(monkeypatch):
    frame = _reliance_with_holiday_filler()
    _serve(monkeypatch, frame)
    text = market.get_stock_stats_indicators_window("RELIANCE.NS", "atr", "2026-01-20", 8)

    assert _line(text, HOLIDAY) == f"{HOLIDAY}: N/A: Not a trading day (Municipal Corporation Election, Maharashtra)"
    # The next day's ATR is the one from the sessions alone, not pulled down by a zero-range bar.
    from stockstats import wrap
    sessions = wrap(frame.drop(pd.Timestamp(HOLIDAY)).reset_index())
    expected = sessions["atr"].iloc[list(sessions["Date"]).index(pd.Timestamp("2026-01-16"))]
    assert float(_line(text, "2026-01-16").split(": ", 1)[1]) == pytest.approx(expected)


@pytest.mark.unit
def test_the_snapshot_for_the_holiday_uses_the_session_before_and_says_why(monkeypatch):
    _serve(monkeypatch, _reliance_with_holiday_filler())
    text = snapshot.build_verified_market_snapshot("RELIANCE.NS", HOLIDAY)

    assert "- Latest trading row used: 2026-01-14\n" in text
    assert "- No row for 2026-01-15: Not a trading day (Municipal Corporation Election, Maharashtra)" in text
    assert "| Volume | 0 |" not in text

    # Without an exchange calendar a weekday's missing row is only a guess (the US session may
    # not have opened yet), so nothing is said about it.
    _serve(monkeypatch, _bars(pd.bdate_range("2025-10-01", "2026-01-14")), tz="America/New_York")
    text = snapshot.build_verified_market_snapshot("AAPL", HOLIDAY)
    assert "- Latest trading row used: 2026-01-14\n" in text and "No row for" not in text


@pytest.mark.unit
def test_settlement_closes_do_not_count_the_filler_as_a_session(monkeypatch):
    _serve(monkeypatch, _reliance_with_holiday_filler())
    closes = market.get_closes("RELIANCE.NS", "2026-01-12", "2026-01-21")

    assert [stamp.strftime("%Y-%m-%d") for stamp in closes.index] == [
        "2026-01-12", "2026-01-13", "2026-01-14", "2026-01-16", "2026-01-19", "2026-01-20"]


# --- dates before the 5-year window (finding 8) ---------------------------------------------

def _long_history() -> pd.DataFrame:
    return _bars(pd.bdate_range("2015-01-01", "2026-10-30"))


@pytest.mark.unit
def test_an_old_date_is_downloaded_from_far_enough_back_and_cached_under_that_year(monkeypatch, tmp_path):
    yahoo = _serve(monkeypatch, _long_history())
    frame = ohlcv.load_ohlcv("RELIANCE.NS", "2020-03-23")

    assert yahoo.requests[-1]["start"] == "2019-01-01"
    assert frame["Date"].iloc[-1] == pd.Timestamp("2020-03-23") and len(frame) > 250
    assert (tmp_path / "RELIANCE.NS-YFin-data-from-2019.csv").exists()

    # The same day, another date that needs history from 2019 reads that file...
    _stamp(tmp_path / "RELIANCE.NS-YFin-data-from-2019.csv", NOW)
    assert ohlcv.load_ohlcv("RELIANCE.NS", "2020-06-01")["Date"].iloc[-1] == pd.Timestamp("2020-06-01")
    assert len(yahoo.requests) == 1
    # ...and a recent date keeps the 5-year window and its file.
    ohlcv.load_ohlcv("RELIANCE.NS", "2026-10-29")
    assert yahoo.requests[-1]["start"] == "2021-10-30" and (tmp_path / "RELIANCE.NS-YFin-data.csv").exists()


@pytest.mark.unit
def test_old_dates_get_values_not_holidays_or_no_data(monkeypatch):
    _serve(monkeypatch, _long_history())

    rsi = market.get_stock_stats_indicators_window("RELIANCE.NS", "rsi", "2020-03-23", 1)
    assert not _line(rsi, "2020-03-23").split(": ", 1)[1].startswith("N/A")
    # Near the window's start, a 200-day SMA has its 200 sessions behind it.
    sma = market.get_stock_stats_indicators_window("RELIANCE.NS", "close_200_sma", "2022-03-01", 0)
    assert not _line(sma, "2022-03-01").split(": ", 1)[1].startswith("N/A")
    assert "- Latest trading row used: 2020-03-23\n" in snapshot.build_verified_market_snapshot(
        "RELIANCE.NS", "2020-03-23")
    assert market_context._closes("^NSEI", "2020-03-23").index[-1].isoformat() == "2020-03-23"


# --- what the daily prices are (finding 12) --------------------------------------------------

@pytest.mark.unit
def test_stock_data_and_the_snapshot_say_the_prices_are_dividend_adjusted(monkeypatch):
    _serve(monkeypatch, _bars(_nse_days("2025-10-01", "2026-01-20")))
    label = "adjusted by Yahoo for splits and dividends: rows dated before an ex-dividend date, even one after"

    header = market.get_YFin_data_online("RELIANCE.NS", "2026-01-12", "2026-01-20").split("\n\n")[0]
    assert f"# Prices: {label}" in header and "exchange's printed prices" in header
    text = snapshot.build_verified_market_snapshot("RELIANCE.NS", "2026-01-20")
    assert f"- Prices: {label}" in text


# --- volume-weighted indicators (finding 14) -------------------------------------------------

def _daily_frame(days, volume) -> pd.DataFrame:
    """load_ohlcv's shape: dates in a ``Date`` column."""
    return _bars(days, volume).reset_index()


@pytest.mark.unit
@pytest.mark.parametrize("indicator", ["vwma", "mfi"])
def test_a_series_without_volume_has_no_volume_weighted_reading(monkeypatch, indicator):
    days = _nse_days("2026-07-01", "2026-10-29")
    monkeypatch.setattr(market, "load_ohlcv", lambda symbol, as_of: _daily_frame(days, 0))
    text = market.get_stock_stats_indicators_window("^INDIAVIX", indicator, "2026-10-29", 3)

    for day in ("2026-10-29", "2026-10-28", "2026-10-27", "2026-10-26"):
        assert _line(text, day) == f"{day}: {NO_VOLUME}"
    assert market.get_stock_stats("^INDIAVIX", indicator, "2026-10-29") == NO_VOLUME


@pytest.mark.unit
def test_mfi_is_on_the_0_to_100_scale_its_guidance_uses(monkeypatch):
    days = _nse_days("2026-07-01", "2026-10-29")
    volume = 1_000_000 + 250_000 * (np.arange(len(days)) % 5)
    frame = _daily_frame(days, volume)
    monkeypatch.setattr(market, "load_ohlcv", lambda symbol, as_of: frame)

    # The textbook MFI over the last 14 sessions.
    typical = (frame["High"] + frame["Low"] + frame["Close"]) / 3
    flow = typical * frame["Volume"]
    window = slice(len(frame) - 14, len(frame))
    rising = typical.diff().iloc[window] > 0
    falling = typical.diff().iloc[window] < 0
    expected = 100 * flow.iloc[window][rising].sum() / (flow.iloc[window][rising].sum() + flow.iloc[window][falling].sum())

    text = market.get_stock_stats_indicators_window("^BSESN", "mfi", "2026-10-29", 0)
    assert float(_line(text, "2026-10-29").split(": ", 1)[1]) == pytest.approx(expected)
    assert float(market.get_stock_stats("^BSESN", "mfi", "2026-10-29")) == pytest.approx(expected)
    assert 1 < expected < 100
    assert "overbought (>80) or oversold (<20)" in text


@pytest.mark.unit
def test_the_snapshot_reports_volume_indicators_the_same_way(monkeypatch):
    days = _nse_days("2026-07-01", "2026-10-29")
    monkeypatch.setattr(snapshot, "load_ohlcv", lambda symbol, as_of, fill_gaps=True: _daily_frame(days, 0))
    text = snapshot.build_verified_market_snapshot("^INDIAVIX", "2026-10-29", indicators=("vwma", "mfi"))
    assert f"| vwma | {NO_VOLUME} |" in text and f"| mfi | {NO_VOLUME} |" in text

    frame = _daily_frame(days, 1_000_000 + 250_000 * (np.arange(len(days)) % 5))
    monkeypatch.setattr(snapshot, "load_ohlcv", lambda symbol, as_of, fill_gaps=True: frame)
    monkeypatch.setattr(market, "load_ohlcv", lambda symbol, as_of: frame)
    mfi = float(market.get_stock_stats("^BSESN", "mfi", "2026-10-29"))
    text = snapshot.build_verified_market_snapshot("^BSESN", "2026-10-29", indicators=("mfi",))
    assert f"| mfi | {snapshot._fmt(mfi)} |" in text and mfi > 1


@pytest.mark.unit
def test_vwma_with_some_volume_in_its_window_still_has_a_value(monkeypatch):
    days = _nse_days("2026-07-01", "2026-10-29")
    volume = np.where(np.arange(len(days)) % 3 == 0, 0, 1_000_000)
    monkeypatch.setattr(market, "load_ohlcv", lambda symbol, as_of: _daily_frame(days, volume))
    text = market.get_stock_stats_indicators_window("^NSEI", "vwma", "2026-10-29", 0)

    assert float(_line(text, "2026-10-29").split(": ", 1)[1]) > 0
