"""Relative returns measured on the sessions both series have, and a watchlist that keeps
its rows when one entry is not a symbol.

Found on 30 Oct 2026 in Yahoo's Indian series: stocks carry a row for the 15 Jan 2026
holiday that NIFTY 50 lacks, sector indices miss 1 Jan and special sessions, and an index
close can come a day late. Cutting each series' window on its own shifted one against the
other: for a run dated 20 Jan 2026, RELIANCE.NS read -3.1 pts against NIFTY 50 over 3M
where the shared sessions give +0.2. A watchlist that named USD/INR lost every row to one
notice written for a model.

No test reaches Yahoo: its answers are stubbed and the loader's clock is fixed.
"""

from datetime import date

import pandas as pd
import pytest
from rich.console import Console
from typer.testing import CliRunner

import cli.main as cli_main
import cli.market as cli_market
from tradingagents.dataflows.router import route_to_vendor
from tradingagents.dataflows.vendors.yahoo import market_context, ohlcv
from tradingagents.markets import risk

AS_OF = "2026-10-28"
NOW = pd.Timestamp("2026-10-30 18:00")       # the clock load_ohlcv reads


def _history(dates, closes) -> pd.DataFrame:
    """Daily bars as yfinance's Ticker.history returns them: indexed by date, OHLC at the close."""
    closes = [float(c) for c in closes]
    return pd.DataFrame({"Open": closes, "High": closes, "Low": closes, "Close": closes, "Volume": 1000},
                        index=pd.DatetimeIndex(dates, name="Date"))


@pytest.fixture
def yahoo(monkeypatch, tmp_path):
    """Daily history by Yahoo symbol, read through the real load_ohlcv; a symbol not set is flat to AS_OF."""
    histories = {}

    class Ticker:
        def __init__(self, symbol):
            self.symbol = symbol

        def history(self, **kwargs):
            dates = pd.bdate_range(end=AS_OF, periods=300)
            return histories.get(self.symbol, _history(dates, [100.0] * len(dates)))

    monkeypatch.setattr(ohlcv.yf, "Ticker", Ticker)
    monkeypatch.setattr(ohlcv, "get_config", lambda: {"data_cache_dir": str(tmp_path)})
    monkeypatch.setattr(ohlcv.pd.Timestamp, "today", staticmethod(lambda: NOW))
    monkeypatch.setattr(market_context, "_classification", lambda symbol: ("Energy", "Oil & Gas Refining & Marketing"))
    monkeypatch.setattr(market_context, "expected_last_session", lambda exchange, as_of: as_of)
    return histories


def _series(points: dict[int, float]) -> pd.Series:
    """Closes on 2026 January days, by day of the month."""
    return pd.Series(list(points.values()), index=[date(2026, 1, d) for d in points], dtype=float)


def _row(text: str, symbol: str) -> str:
    return next(line for line in text.splitlines() if line.startswith(f"| {symbol} |"))


# --- relative return (finding 2) -----------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("asset,bench,expected", [
    # A holiday filler row only the stock has (NSE's 15 Jan 2026): the stock rose in the
    # first of the last 3 shared sessions, which its own last 3 rows start after.
    ({5: 100, 6: 110, 7: 110, 8: 110, 9: 110}, {5: 100, 6: 100, 8: 100, 9: 100}, 0.10),
    # A session only the index has (the stock did not trade): the index's own last 3 rows
    # start after its rise, the shared ones before it.
    ({5: 100, 6: 100, 8: 100, 9: 100}, {5: 100, 6: 110, 7: 110, 8: 110, 9: 110}, -0.10),
    # The index's latest close not yet published: the stock's move on the 9th has nothing
    # to be measured against, so both stop on the 8th.
    ({5: 100, 6: 100, 7: 100, 8: 100, 9: 120}, {5: 100, 6: 100, 7: 100, 8: 100}, 0.0),
])
def test_relative_return_measures_both_series_over_the_sessions_they_share(asset, bench, expected):
    assert risk.relative_return(_series(asset), _series(bench), 3) == pytest.approx(expected)


@pytest.mark.unit
def test_relative_return_needs_enough_shared_sessions_not_enough_rows_in_each():
    asset, bench = _series({5: 100, 6: 101, 7: 102, 8: 103}), _series({6: 100, 7: 101, 8: 102, 9: 103})
    assert risk.relative_return(asset, bench, 3) is None          # four rows each, three shared
    assert risk.relative_return(asset, bench, 2) == pytest.approx(103 / 101 - 102 / 100)


@pytest.mark.unit
def test_a_date_given_twice_counts_once_with_its_last_close():
    asset = pd.Series([100.0, 100.0, 100.0, 105.0], index=[date(2026, 1, d) for d in (5, 6, 7, 7)])
    bench = _series({5: 100, 6: 100, 7: 100})
    assert risk.relative_return(asset, bench, 2) == pytest.approx(0.05)
    assert risk.relative_return(asset, bench, 3) is None


@pytest.mark.unit
def test_the_market_context_and_the_watchlist_compare_the_same_sessions(yahoo):
    as_of = "2026-01-20"
    days = pd.bdate_range("2024-10-01", as_of)
    holiday, new_year = pd.Timestamp("2026-01-15"), pd.Timestamp("2026-01-01")
    nifty_days = days.drop([holiday])                    # Yahoo gives stocks a 15 Jan row and indices none
    energy_days = days.drop([holiday, new_year])          # and NIFTY Energy no 1 Jan row either
    # The stock rose 10% in the first of the last 21 sessions it shares with NIFTY 50.
    jump = nifty_days[-21]
    yahoo["RELIANCE.NS"] = _history(days, [110 if d >= jump else 100 for d in days])
    yahoo["^NSEI"] = _history(nifty_days, [20000] * len(nifty_days))
    yahoo["^CNXENERGY"] = _history(energy_days, [30000] * len(energy_days))

    text = market_context.get_market_context("RELIANCE.NS", as_of)
    assert "- vs NIFTY 50 (^NSEI): 1M +10.0 pts, 3M +10.0 pts, 1Y +10.0 pts" in text
    assert "- vs NIFTY Energy (^CNXENERGY): 1M +10.0 pts, 3M +10.0 pts, 1Y +10.0 pts" in text
    row = _row(market_context.get_watchlist_snapshot(["RELIANCE.NS"], as_of), "RELIANCE.NS")
    assert row.endswith("| +10.0 pts | +10.0 pts |")


@pytest.mark.unit
def test_relative_figures_say_when_the_index_stops_before_the_stock(yahoo):
    late = pd.bdate_range(end="2026-10-27", periods=300)              # NIFTY 50's 28 Oct close not yet out
    yahoo["^NSEI"] = _history(late, [20000] * len(late))
    yahoo["TCS.NS"] = _history(late, [3000] * len(late))              # a stock as late as the index

    text = market_context.get_market_context("RELIANCE.NS", AS_OF)
    nifty = next(line for line in text.splitlines() if line.startswith("- vs NIFTY 50"))
    energy = next(line for line in text.splitlines() if line.startswith("- vs NIFTY Energy"))
    assert nifty.endswith("(to 27 Oct, the last close both have)")
    assert "(to " not in energy                                       # the sector index has the 28th

    watchlist = market_context.get_watchlist_snapshot(["RELIANCE.NS", "TCS.NS"], AS_OF)
    assert _row(watchlist, "RELIANCE.NS").endswith("| +0.0 pts (to 27 Oct) | +0.0 pts (to 27 Oct) |")
    assert "(to " not in _row(watchlist, "TCS.NS")                    # its own close date says it is late


# --- a watchlist entry that is not a symbol (finding 10) --------------------------------

@pytest.mark.unit
def test_a_watchlist_entry_that_is_not_a_symbol_gets_a_row_and_the_others_stay(yahoo):
    text = route_to_vendor("get_watchlist_snapshot", ["RELIANCE.NS", "USD/INR", "TATA MOTORS", "TCS.NS"], AS_OF)

    assert text.startswith("# Watchlist as of 2026-10-28") and "DATA_UNAVAILABLE" not in text
    for symbol in ("RELIANCE.NS", "TCS.NS"):
        assert _row(text, symbol).startswith(f"| {symbol} | 100.00 | 2026-10-28 |")
    rupee, tata = _row(text, "USD/INR"), _row(text, "TATA MOTORS")
    assert "unavailable" in rupee and "did you mean USDINR=X?" in rupee
    assert "unavailable" in tata and "did you mean" not in tata


@pytest.mark.unit
def test_the_market_command_shows_the_watchlist_rows_beside_a_bad_entry(yahoo, monkeypatch):
    monkeypatch.setattr(cli_market, "console", Console(width=250))    # a table squeezed to 80 columns cuts words
    result = CliRunner().invoke(cli_main.app, ["market", "--exchange", "nse", "--watchlist", "RELIANCE,USD/INR",
                                               "--date", AS_OF])
    # Rendered at a terminal, printed as markdown without one (cli/market.py): compare the words.
    output = " ".join(result.output.replace("|", " ").split())
    assert result.exit_code == 0, result.output
    assert "RELIANCE.NS 100.00 2026-10-28" in output
    assert "USD/INR unavailable (" in output and "did you mean USDINR=X?" in output
    assert "DATA_UNAVAILABLE" not in output
