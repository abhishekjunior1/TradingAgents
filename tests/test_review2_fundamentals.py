"""Yahoo corporate actions and insider rows: third-round review fixes (found on 30 Oct 2026).

- Yahoo divides each dividend by the ratio of every later split, bonus issue or share
  consolidation, so after a consolidation a past run's amounts are above the ones declared:
  PATANJALI.NS's 2019 capital reduction turns ₹0.16 into 5.33, GE's 1-for-8 of 2021 turns
  $0.01 into 0.05. The past-run label said they could only be below; it now says either, in
  the same words whatever came after the run.
- Insider rows are checked against closes read as of the last trade, never after the run
  date, so a listing whose history stops between the trades and the run date (suspended, or
  a feed Yahoo stopped) is checked against the closes it has, not said to have none.

yf.Ticker is stubbed and the clocks are fixed; load_ohlcv runs for real on the test's own
cache. No test reaches Yahoo.
"""

from types import SimpleNamespace
from unittest import mock

import pandas as pd
import pytest

from tradingagents.dataflows import date_window
from tradingagents.dataflows.vendors.yahoo import common, corporate_actions, fundamentals, ohlcv
from tradingagents.dataflows.vendors.yahoo.common import price_text

TODAY = "2026-10-30"
NOW = pd.Timestamp("2026-10-30 18:00")     # the machine's clock (naive, local), after NSE's close


@pytest.fixture(autouse=True)
def clock(monkeypatch, tmp_path):
    """The run is dated TODAY; load_ohlcv reads NOW as the time and a cache of the test's own."""
    for module in (date_window, fundamentals, corporate_actions):
        monkeypatch.setattr(module, "get_current_date", lambda: TODAY)
    monkeypatch.setattr(date_window, "listing_date", lambda symbol, now=None: None)
    monkeypatch.setattr(common, "vendor_reachable", lambda url: True)
    monkeypatch.setattr(ohlcv.pd.Timestamp, "today", staticmethod(lambda: NOW))
    monkeypatch.setattr(ohlcv, "get_config", lambda: {"data_cache_dir": str(tmp_path)})


# --- dividends: the basis a past run is told, after a consolidation -------------------------

def _series(points):
    index = pd.DatetimeIndex([pd.Timestamp(d, tz="Asia/Kolkata") for d, _ in points])
    return pd.Series([v for _, v in points], index=index, dtype=float)


# As Yahoo gave them on 30 Oct 2026: run date, dividends on Yahoo's present-day basis, splits.
# PATANJALI.NS (Ruchi Soya until 2022): the 2019 resolution plan's 99% capital reduction is
# Yahoo's 0.01 split, so 5.33333 was declared as 5.33333 x 0.01 x 3 = ₹0.16 (8% of the ₹2 face
# value). GE: the 1-for-8 of 2 Aug 2021 is Yahoo's 0.125, so 0.049841 was declared as $0.01.
PAST_RUNS = {
    "PATANJALI.NS": ("2019-06-01", [("2014-09-22", 5.33333), ("2015-09-16", 5.33333)],
                     [("2007-10-29", 5.0), ("2019-11-14", 0.01), ("2025-09-11", 3.0)]),
    "GE": ("2021-06-01", [("2016-06-16", 1.10226), ("2020-12-18", 0.049841), ("2021-03-05", 0.049841)],
           [("2019-02-26", 1.04), ("2021-08-02", 0.125), ("2023-01-04", 1.281), ("2024-04-02", 1.253)]),
}


def _corporate_actions(monkeypatch, symbol, as_of, dividends, splits):
    """``symbol``'s corporate actions up to ``as_of``, and their dividends line."""
    monkeypatch.setattr(corporate_actions.yf, "Ticker", lambda s: SimpleNamespace(
        dividends=_series(dividends), splits=_series(splits), calendar={}))
    out = corporate_actions.get_corporate_actions(symbol, as_of)
    return out, next(line for line in out.splitlines() if line.startswith("Dividends"))


@pytest.mark.unit
@pytest.mark.parametrize("symbol", PAST_RUNS)
def test_a_past_run_is_told_its_dividends_can_be_above_the_declared_ones(monkeypatch, symbol):
    as_of, dividends, splits = PAST_RUNS[symbol]
    out, label = _corporate_actions(monkeypatch, symbol, as_of, dividends, splits)
    assert all(f"- {day}: {price_text(amount)}" in out for day, amount in dividends)   # Yahoo's, as before
    assert not any(day in out for day, _ in splits if day > as_of)     # the later consolidation stays unseen
    assert "scales each dividend down" not in label
    assert "consolidation" in label
    assert "below the amounts declared (after a split or bonus issue)" in label
    assert "above them (after a consolidation)" in label


@pytest.mark.unit
def test_every_past_run_reads_the_same_label_whatever_came_after_it(monkeypatch):
    """A label that changed with the later actions would tell the run what came after it."""
    as_of, dividends, splits = PAST_RUNS["PATANJALI.NS"]
    later = {"consolidation": splits, "split": [("2007-10-29", 5.0), ("2025-09-11", 3.0)], "none": splits[:1]}
    labels = {kind: _corporate_actions(monkeypatch, "PATANJALI.NS", as_of, dividends, actions)[1]
              for kind, actions in later.items()}
    assert len(set(labels.values())) == 1


# --- insider rows: a history that stops after the trades ----------------------------------

def _insider_rows(days_ago=(48, 49)):
    """RELIANCE.NS's rows of 11-12 Sep 2025 as Yahoo gave them, dated ``days_ago`` before the run."""
    today = pd.Timestamp(TODAY)
    return pd.DataFrame({
        "Shares": [25000, 481905],
        "Value": [392252, 7563595],
        "Text": ["Sale at price 15.69 per share.", "Sale at price 15.70 per share."],
        "Start Date": [today - pd.Timedelta(days=d) for d in days_ago],
    })


def _bars(end, periods=200, base=1350.0):
    """Daily bars to ``end``, dated in the index in IST as yfinance gives them."""
    days = pd.bdate_range(end=end, periods=periods)
    close = [base + 40 * (i % 20) / 19 for i in range(periods)]
    index = pd.DatetimeIndex([day.tz_localize("Asia/Kolkata") for day in days], name="Date")
    return pd.DataFrame({"Open": close, "High": [c + 5 for c in close], "Low": [c - 5 for c in close],
                         "Close": close, "Volume": 1_000_000}, index=index)


def _days_before(days):
    return pd.Timestamp(TODAY) - pd.Timedelta(days=days)


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


@pytest.mark.unit
def test_a_listing_whose_history_stops_after_the_trades_is_checked_against_its_closes(monkeypatch):
    """Suspended 15 days before the run: load_ohlcv refuses that history as stale for the run
    date, but it holds the closes over the trades, which the rows are held to."""
    bars, rows = _bars(_days_before(15)), _insider_rows()
    downloads = _serve(monkeypatch, {"RELIANCE.NS": bars}, rows)
    note = _note("RELIANCE.NS", rows)
    assert ("do not match RELIANCE.NS's INR closes over the same period " + _range_over_trades(bars, rows)
            in note)
    assert "there were no" not in note and "could be retrieved" not in note
    # The 5-year download the price tools make, once: reading as of the trades costs no other.
    assert downloads == [("RELIANCE.NS", f"{NOW - pd.DateOffset(years=5):%Y-%m-%d}")]


@pytest.mark.unit
def test_a_bse_listing_whose_history_stops_after_the_trades_is_checked_against_its_own_closes(monkeypatch):
    bse, rows = _bars(_days_before(15)), _insider_rows()
    downloads = _serve(monkeypatch, {"RELIANCE.BO": bse, "RELIANCE.NS": _bars(TODAY, base=1500.0)}, rows)
    note = _note("RELIANCE.BO", rows)
    assert "do not match RELIANCE.BO's INR closes over the same period " + _range_over_trades(bse, rows) in note
    assert "RELIANCE.NS" not in note and "Yahoo has none" not in note
    assert [symbol for symbol, _ in downloads] == ["RELIANCE.BO"]


@pytest.mark.unit
def test_a_history_that_stops_before_the_trades_still_has_no_closes_then(monkeypatch):
    rows = _insider_rows()
    _serve(monkeypatch, {"RELIANCE.NS": _bars(_days_before(120))}, rows)
    note = _note("RELIANCE.NS", rows)
    assert "there were no INR closes of RELIANCE.NS over the same period to check them against" in note
    assert "could be retrieved" not in note and "do not match" not in note


@pytest.mark.unit
def test_a_bse_listing_with_only_today_s_bar_is_still_held_to_its_nse_listing(monkeypatch):
    """RELIANCE.BO on 30 Oct 2026: Yahoo's daily history is the one bar of the day."""
    rows, nse = _insider_rows(), _bars(TODAY)
    _serve(monkeypatch, {"RELIANCE.BO": _bars(TODAY, periods=1), "RELIANCE.NS": nse}, rows)
    note = _note("RELIANCE.BO", rows)
    assert ("do not match the INR closes of RELIANCE.NS, the NSE listing under the same symbol, over the same "
            "period " + _range_over_trades(nse, rows) + "; Yahoo has none for RELIANCE.BO then), so") in note


@pytest.mark.unit
@pytest.mark.parametrize("days_ago, read_as_of", [((48, 49), "2026-09-12"), ((-1, 49), TODAY)],
                         ids=["the_last_trade", "a_row_dated_after_the_run"])
def test_closes_are_read_as_of_the_last_trade_and_never_after_the_run_date(monkeypatch, days_ago, read_as_of):
    rows = _insider_rows(days_ago)
    _serve(monkeypatch, {"RELIANCE.NS": _bars(TODAY)}, rows)
    with mock.patch.object(fundamentals, "load_ohlcv", wraps=ohlcv.load_ohlcv) as loaded:
        fundamentals.get_insider_transactions("RELIANCE.NS", TODAY)
    assert [call.args[:2] for call in loaded.call_args_list] == [("RELIANCE.NS", read_as_of)]
