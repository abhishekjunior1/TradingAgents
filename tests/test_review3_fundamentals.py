"""Yahoo insider rows: fourth-round review fixes (found on 30 Oct 2026).

- The closes insider prices are held to are read with no staleness test, since only those
  over the trades' span are kept. A listing whose daily history pauses inside the span (a
  suspension it has since come back from) or stops inside it is checked against the closes
  it has then, on BSE as on NSE, rather than said to have none; a BSE listing is no longer
  held to its NSE listing instead.
- While the price tools' shared 5-year download holds the whole span, the closes are read
  from it, so rows older than about 3.9 years cost no download of their own. Rows that
  download does not hold are still read from one of their own, and checked.

yf.Ticker is stubbed and the clocks are fixed; load_ohlcv runs for real on the test's own
cache. No test reaches Yahoo.
"""

import os
from unittest import mock

import pandas as pd
import pytest

from tradingagents.dataflows import date_window
from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.vendors.yahoo import common, fundamentals, ohlcv
from tradingagents.dataflows.vendors.yahoo.common import price_text

TODAY = "2026-10-30"
NOW = pd.Timestamp("2026-10-30 18:00")     # the machine's clock (naive, local), after NSE's close
FIVE_YEARS_BACK = f"{NOW - pd.DateOffset(years=5):%Y-%m-%d}"     # where the shared download starts


@pytest.fixture(autouse=True)
def clock(monkeypatch, tmp_path):
    """The run is dated TODAY; load_ohlcv reads NOW as the time and a cache of the test's own."""
    for module in (date_window, fundamentals):
        monkeypatch.setattr(module, "get_current_date", lambda: TODAY)
    monkeypatch.setattr(date_window, "listing_date", lambda symbol, now=None: None)
    monkeypatch.setattr(common, "vendor_reachable", lambda url: True)
    monkeypatch.setattr(ohlcv.pd.Timestamp, "today", staticmethod(lambda: NOW))
    monkeypatch.setattr(ohlcv, "get_config", lambda: {"data_cache_dir": str(tmp_path)})


def _days_before(days):
    return pd.Timestamp(TODAY) - pd.Timedelta(days=days)


def _insider_rows(days_ago):
    """RELIANCE.NS's rows of 11-12 Sep 2025 as Yahoo gave them, dated ``days_ago`` before the run."""
    return pd.DataFrame({
        "Shares": [25000, 481905],
        "Value": [392252, 7563595],
        "Text": ["Sale at price 15.69 per share.", "Sale at price 15.70 per share."],
        "Start Date": [_days_before(d) for d in days_ago],
    })


def _bars(keep=lambda day: True, base=1350.0):
    """Yahoo's daily history from 2014 to the run, on the business days ``keep`` passes,
    dated in the index in IST as yfinance gives them."""
    days = [day for day in pd.bdate_range("2014-01-01", TODAY) if keep(day)]
    close = [base + 40 * (i % 20) / 19 for i in range(len(days))]
    index = pd.DatetimeIndex([day.tz_localize("Asia/Kolkata") for day in days], name="Date")
    return pd.DataFrame({"Open": close, "High": [c + 5 for c in close], "Low": [c - 5 for c in close],
                         "Close": close, "Volume": 1_000_000}, index=index)


def _range_over_trades(bars, rows):
    """The lowest and highest close from a week before the first trade to the last."""
    naive = bars.index.tz_localize(None)
    dates = rows["Start Date"]
    closes = bars["Close"][(naive >= dates.min() - pd.Timedelta(days=7)) & (naive <= dates.max())]
    assert len(closes) >= 5          # the listing traded then
    return f"({price_text(closes.min())}-{price_text(closes.max())}"


def _serve(monkeypatch, bars, rows):
    """yf.Ticker serving ``rows`` as every listing's insider rows and ``bars[symbol]`` as its
    daily history; returns the (symbol, start) of each download."""
    downloads = []

    class Ticker:
        insider_transactions = rows

        def __init__(self, symbol):
            self.symbol = symbol

        def history(self, start=None, end=None, **kwargs):
            downloads.append((self.symbol, start))
            frame = bars[self.symbol]
            naive = frame.index.tz_localize(None)
            return frame[(naive >= pd.Timestamp(start)) & (naive < pd.Timestamp(end))].copy()

    monkeypatch.setattr(fundamentals.yf, "Ticker", Ticker)
    return downloads


def _note(ticker, rows):
    out = fundamentals.get_insider_transactions(ticker, TODAY)
    assert out.startswith(f"# Insider Transactions data for {ticker}\n" + rows.to_csv() + "\nNote: ")
    return out[out.index("\nNote: "):]


def _stamp(path, ts):
    """Set ``path``'s mtime to the wall-clock ``ts``, read back in local time as the cache does."""
    t = ts.to_pydatetime().timestamp()
    os.utime(path, (t, t))


# --- a history that pauses or stops inside the trades' span ---------------------------------

SHAPES = {
    # A suspension the listing has come back from: no bars from 79 to 46 days before the run,
    # so the last trade (a Saturday) falls 31 days after the last session before it.
    "suspended_and_back": ((48, 120), lambda day: not _days_before(79) < day < _days_before(46)),
    # Bars that end 100 days before the run, between the first trade and the last.
    "stops_inside_the_span": ((48, 200), lambda day: day <= _days_before(100)),
}


@pytest.mark.unit
@pytest.mark.parametrize("shape", SHAPES)
def test_a_listing_whose_history_pauses_or_stops_inside_the_span_is_checked_against_its_closes(
        monkeypatch, shape):
    days_ago, keep = SHAPES[shape]
    bars, rows = _bars(keep), _insider_rows(days_ago)
    downloads = _serve(monkeypatch, {"RELIANCE.NS": bars}, rows)
    note = _note("RELIANCE.NS", rows)
    assert ("do not match RELIANCE.NS's INR closes over the same period " + _range_over_trades(bars, rows)
            in note)
    assert "there were no" not in note and "could be retrieved" not in note
    assert downloads == [("RELIANCE.NS", FIVE_YEARS_BACK)]


@pytest.mark.unit
@pytest.mark.parametrize("shape", SHAPES)
def test_a_bse_listing_whose_history_pauses_or_stops_inside_the_span_is_checked_against_its_own_closes(
        monkeypatch, shape):
    days_ago, keep = SHAPES[shape]
    bse, rows = _bars(keep), _insider_rows(days_ago)
    downloads = _serve(monkeypatch, {"RELIANCE.BO": bse, "RELIANCE.NS": _bars(base=1500.0)}, rows)
    note = _note("RELIANCE.BO", rows)
    assert "do not match RELIANCE.BO's INR closes over the same period " + _range_over_trades(bse, rows) in note
    assert "RELIANCE.NS" not in note and "Yahoo has none" not in note
    assert [symbol for symbol, _ in downloads] == ["RELIANCE.BO"]      # the NSE listing is not fetched


# A suspension over every trade, which the listing has since come back from.
ACROSS_THE_SPAN = ((48, 120), lambda day: not _days_before(140) < day < _days_before(40))


@pytest.mark.unit
def test_a_suspension_over_the_whole_span_still_has_no_closes_then(monkeypatch):
    days_ago, keep = ACROSS_THE_SPAN
    rows = _insider_rows(days_ago)
    _serve(monkeypatch, {"RELIANCE.NS": _bars(keep)}, rows)
    note = _note("RELIANCE.NS", rows)
    assert "there were no INR closes of RELIANCE.NS over the same period to check them against" in note
    assert "could be retrieved" not in note and "do not match" not in note


@pytest.mark.unit
def test_a_bse_listing_suspended_over_the_whole_span_is_held_to_its_nse_listing(monkeypatch):
    days_ago, keep = ACROSS_THE_SPAN
    rows, nse = _insider_rows(days_ago), _bars(base=1500.0)
    _serve(monkeypatch, {"RELIANCE.BO": _bars(keep), "RELIANCE.NS": nse}, rows)
    note = _note("RELIANCE.BO", rows)
    assert ("do not match the INR closes of RELIANCE.NS, the NSE listing under the same symbol, over the same "
            "period " + _range_over_trades(nse, rows) + "; Yahoo has none for RELIANCE.BO then), so") in note


@pytest.mark.unit
def test_load_ohlcv_serves_a_stale_frame_only_when_told_to(monkeypatch):
    bars = _bars(lambda day: day <= _days_before(100))
    _serve(monkeypatch, {"RELIANCE.NS": bars}, _insider_rows((48, 49)))
    with pytest.raises(NoMarketDataError, match="stale"):
        ohlcv.load_ohlcv("RELIANCE.NS", TODAY, fill_gaps=False)
    frame = ohlcv.load_ohlcv("RELIANCE.NS", TODAY, fill_gaps=False, max_stale_days=None)
    assert frame["Date"].max() == bars.index.tz_localize(None).max()


# --- old rows: which download holds their span ------------------------------------------------

@pytest.mark.unit
def test_rows_the_shared_download_holds_are_read_from_it(monkeypatch, tmp_path):
    """Rows about 4.5 years old: the price tools' 5-year download of the day holds their span,
    so checking them costs no download of its own."""
    bars, rows = _bars(), _insider_rows((1640, 1645))
    downloads = _serve(monkeypatch, {"RELIANCE.NS": bars}, rows)
    ohlcv.load_ohlcv("RELIANCE.NS", TODAY)                  # the price tools' download
    _stamp(tmp_path / "RELIANCE.NS-YFin-data.csv", NOW)
    with mock.patch.object(fundamentals, "load_ohlcv", wraps=ohlcv.load_ohlcv) as loaded:
        note = _note("RELIANCE.NS", rows)
    assert ("do not match RELIANCE.NS's INR closes over the same period " + _range_over_trades(bars, rows)
            in note)
    assert downloads == [("RELIANCE.NS", FIVE_YEARS_BACK)]
    assert os.listdir(tmp_path) == ["RELIANCE.NS-YFin-data.csv"]
    (read_as_of,) = [call.args[1] for call in loaded.call_args_list]
    assert read_as_of <= TODAY                              # never after the run date


@pytest.mark.unit
@pytest.mark.parametrize("days_ago, start", [((2200, 2205), "2019-01-01"), ((1640, 1900), "2021-01-01")],
                         ids=["before_it", "across_its_start"])
def test_rows_the_shared_download_does_not_hold_are_read_from_one_of_their_own(monkeypatch, days_ago, start):
    """Rows from before the 5-year window are checked against their closes, not said to have none."""
    bars, rows = _bars(), _insider_rows(days_ago)
    downloads = _serve(monkeypatch, {"RELIANCE.NS": bars}, rows)
    note = _note("RELIANCE.NS", rows)
    assert ("do not match RELIANCE.NS's INR closes over the same period " + _range_over_trades(bars, rows)
            in note)
    assert downloads == [("RELIANCE.NS", start)]
