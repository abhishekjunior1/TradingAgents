"""Second review of the intraday fixes: the pivots from Yahoo's dividend-adjusted daily bar say so, the
note on what that label means comes with every figure that carries it, and an instrument that pays no
dividend (an index, a currency, a future, a crypto pair) is not told its daily close is dividend-adjusted.

Synthetic bars and a stubbed Yahoo at pinned moments: nothing reaches the network or depends on today.
"""

from datetime import date, datetime, time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from tradingagents.dataflows.vendors.yahoo import intraday as yahoo_intraday
from tradingagents.markets.exchanges import NSE
from tradingagents.markets.intraday import format_intraday_snapshot, intraday_levels, target_session

IST = ZoneInfo("Asia/Kolkata")
NEW_YORK = ZoneInfo("America/New_York")

GAP_NOTE = "The gap at the opening price is measured against Yahoo's daily close, which is dividend-adjusted"
DAILY_BAR_NOTE = ("The bars hold no session before the plan's, so the last session was read from Yahoo's daily "
                  "bar, which is dividend-adjusted (the intraday bars are not): on an ex-dividend day its prices "
                  "are the ones that traded less about the dividend, so the pivots from it sit about the dividend "
                  "lower, and a gap measured against its close reads higher than it traded by about the dividend "
                  "yield.")

# Wednesday 28 Oct as traded, in the bars ``_session`` makes, and pivots from it.
TRADED_PIVOTS = "| 103.227 | 102.233 | 101.487 | 100.493 | 99.7467 | 98.7533 | 98.0067 |"
# The same session in the daily rows on Thursday 29 Oct, an ex-dividend day (2.00 a share): Yahoo scales
# it by 1 - 2.00 / 100.74, so its close is 98.74 and its pivots sit about 2.00 lower.
ADJUSTED_WEDNESDAY = {"Open": 98.01, "High": 99.23, "Low": 97.52, "Close": 98.74}
ADJUSTED_PIVOTS = "| 101.183 | 100.207 | 99.4733 | 98.4967 | 97.7633 | 96.7867 | 96.0533 |"


def _at(day, clock: str, zone=IST) -> datetime:
    return pd.Timestamp(f"{day} {clock}", tz=zone).to_pydatetime()


def _session(day: str, start: str = "09:15", end: str = "15:30", zone=IST, price: float = 100.0,
             volume: int = 1000) -> pd.DataFrame:
    """One session as yfinance returns it: a tz-aware ``Datetime`` index, OHLC, Adj Close, Volume."""
    index = pd.date_range(f"{day} {start}", f"{day} {end}", freq="5min", inclusive="left", tz=zone,
                          name="Datetime")
    close = price + pd.Series(range(len(index)), dtype=float).to_numpy() * 0.01
    return pd.DataFrame({"Open": close, "High": close + 0.5, "Low": close - 0.5, "Close": close,
                         "Adj Close": close, "Volume": volume}, index=index)


def _daily(end: str, rows: int = 30, price: float = 100.0) -> pd.DataFrame:
    dates = pd.bdate_range(end=end, periods=rows)
    return pd.DataFrame({"Date": dates, "Open": price, "High": price + 2, "Low": price - 2, "Close": price,
                         "Volume": 1})


def _ex_dividend_daily(end: str) -> pd.DataFrame:
    """load_ohlcv's rows on Thursday 29 Oct, an ex-dividend day, ending on ``end``: Wednesday adjusted,
    and Thursday's own row (when there is one) opening at 101.00."""
    daily = _daily(end=end)
    wednesday = daily["Date"] == pd.Timestamp("2026-10-28")
    for column, value in ADJUSTED_WEDNESDAY.items():
        daily.loc[wednesday, column] = value
    daily.loc[daily["Date"] == pd.Timestamp("2026-10-29"), "Open"] = 101.0
    return daily


def _thursday_bars() -> pd.DataFrame:
    return pd.concat([_session("2026-10-27"), _session("2026-10-28"), _session("2026-10-29", end="10:10")])


def _pivots(text: str) -> tuple[str, str]:
    """The pivots' header, and their row of values ('' when there is none)."""
    lines = text.splitlines()
    at = next(i for i, line in enumerate(lines) if line.startswith("## Classic floor pivots"))
    return lines[at], lines[at + 4] if lines[at + 2].startswith("| R3 |") else ""


@pytest.fixture
def yahoo(tmp_path, monkeypatch):
    """Stubbed Yahoo: set ``frame`` (what history returns), ``now`` (the clock) and ``daily`` (load_ohlcv's rows)."""
    state = SimpleNamespace(frame=None, now=None, daily=None, config={"data_cache_dir": str(tmp_path)})

    def ticker(symbol):
        return SimpleNamespace(history=lambda **kwargs: state.frame.copy())

    monkeypatch.setattr(yahoo_intraday.yf, "Ticker", ticker)
    monkeypatch.setattr(yahoo_intraday, "get_config", lambda: dict(state.config))
    monkeypatch.setattr(yahoo_intraday, "_now", lambda: state.now)
    monkeypatch.setattr(yahoo_intraday, "load_ohlcv", lambda symbol, as_of_date, fill_gaps=True: state.daily)
    return state


# --- R3: the pivots from the dividend-adjusted daily bar carry its label ------------------------

@pytest.mark.unit
def test_pivots_from_the_dividend_adjusted_daily_bar_say_so(yahoo):
    yahoo.daily = _ex_dividend_daily(end="2026-10-29")
    yahoo.frame = _thursday_bars()
    yahoo.now = _at("2026-10-29", "10:07")
    # One session of bars holds no earlier session: the last session, and the pivots from it, are
    # read from Wednesday's adjusted daily row.
    one = yahoo_intraday.get_intraday_bars("RELIANCE.NS", "2026-10-29", sessions=1)
    assert "## Last complete session: Wed 28 Oct 2026 (from the daily bar, dividend-adjusted)" in one
    assert _pivots(one) == ("## Classic floor pivots from the 28 Oct 2026 high, low and close, dividend-adjusted",
                            ADJUSTED_PIVOTS)
    # Five sessions read them from the bars, as traded, under no such label.
    five = yahoo_intraday.get_intraday_bars("RELIANCE.NS", "2026-10-29")
    assert "## Last complete session: Wed 28 Oct 2026 (from the 5m bars)" in five
    assert _pivots(five) == ("## Classic floor pivots from the 28 Oct 2026 high, low and close", TRADED_PIVOTS)


@pytest.mark.unit
def test_the_formatter_labels_a_last_session_from_adjusted_daily_rows_its_pivots_and_both_gaps():
    daily = _ex_dividend_daily(end="2026-10-29")
    bars = _session("2026-10-29", end="10:10").reset_index()
    now = _at("2026-10-29", "10:07")
    levels = intraday_levels(bars, daily, date(2026, 10, 29), now, interval="5m", exchange=NSE)
    target = target_session(NSE, date(2026, 10, 29), now)
    text = format_intraday_snapshot("RELIANCE.NS", levels, target, time(15, 10), [], [], daily_adjusted=True)
    assert "## Last complete session: Wed 28 Oct 2026 (from the daily bar, dividend-adjusted)" in text
    assert "| Gap at the first bar's open vs the previous close, dividend-adjusted | +1.28% |" in text
    assert ("| Gap at the opening price (daily bar, 101.00) vs the previous daily close, dividend-adjusted "
            "(98.74) | +2.29% |") in text
    assert _pivots(text) == ("## Classic floor pivots from the 28 Oct 2026 high, low and close, dividend-adjusted",
                             ADJUSTED_PIVOTS)
    # Those four only: the daily ATR is a range, which the adjustment keeps in today's price terms and
    # free of a dividend's drop, so it is not labelled.
    assert text.count(", dividend-adjusted") == 4
    plain = format_intraday_snapshot("RELIANCE.NS", levels, target, time(15, 10), [], [])
    assert "dividend-adjusted" not in plain
    assert _pivots(plain) == ("## Classic floor pivots from the 28 Oct 2026 high, low and close", ADJUSTED_PIVOTS)


# --- R3: the note on the label comes whenever a figure carries it --------------------------------

@pytest.mark.unit
def test_the_note_on_the_dividend_adjustment_comes_with_a_last_session_read_from_the_daily_bar(yahoo):
    # The daily rows end on Wednesday, so there is no gap at the opening price; one session of bars
    # still reads the last session, its pivots and the gap at the first bar's open from Wednesday's row.
    yahoo.daily = _ex_dividend_daily(end="2026-10-28")
    yahoo.frame = _thursday_bars()
    yahoo.now = _at("2026-10-29", "10:07")
    one = yahoo_intraday.get_intraday_bars("RELIANCE.NS", "2026-10-29", sessions=1)
    assert "Gap at the opening price" not in one
    assert "| Gap at the first bar's open vs the previous close, dividend-adjusted | +1.28% |" in one
    assert _pivots(one)[0].endswith(", dividend-adjusted")
    assert f"- {DAILY_BAR_NOTE}" in one
    # With the opening price's row as well, one note covers both gaps: they share that close.
    yahoo.daily = _ex_dividend_daily(end="2026-10-29")
    both = yahoo_intraday.get_intraday_bars("RELIANCE.NS", "2026-10-29", sessions=1)
    assert "vs the previous daily close, dividend-adjusted (98.74) | +2.29% |" in both
    assert f"- {DAILY_BAR_NOTE}" in both and GAP_NOTE not in both
    # Five sessions of bars take nothing from those rows here: no label, and nothing to explain.
    yahoo.daily = _ex_dividend_daily(end="2026-10-28")
    five = yahoo_intraday.get_intraday_bars("RELIANCE.NS", "2026-10-29")
    assert ", dividend-adjusted" not in five
    assert DAILY_BAR_NOTE not in five and GAP_NOTE not in five


@pytest.mark.unit
def test_the_gap_note_alone_explains_the_opening_price_gap_when_the_last_session_is_from_the_bars(yahoo):
    yahoo.daily = _ex_dividend_daily(end="2026-10-29")
    yahoo.frame = _thursday_bars()
    yahoo.now = _at("2026-10-29", "10:07")
    five = yahoo_intraday.get_intraday_bars("RELIANCE.NS", "2026-10-29")
    assert "vs the previous daily close, dividend-adjusted (98.74) | +2.29% |" in five
    assert GAP_NOTE in five and DAILY_BAR_NOTE not in five


# --- R4: no dividend, no dividend-adjusted label --------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("symbol", ["^NSEI", "NIFTY_FIN_SERVICE.NS", "^BSESN"])
def test_an_index_daily_close_is_not_called_dividend_adjusted(yahoo, symbol):
    # Yahoo's index rows are the printed levels (Adj Close equals Close): no dividend, no ex-date.
    yahoo.daily = _daily(end="2026-10-30", price=24900.0)
    yahoo.daily.loc[yahoo.daily["Date"] == pd.Timestamp("2026-10-30"), "Open"] = 25050.0
    yahoo.frame = pd.concat([_session(day, price=24900.0, volume=0) for day in ("2026-10-27", "2026-10-28",
                                                                                 "2026-10-29")]
                            + [_session("2026-10-30", end="11:00", price=25050.0, volume=0)])
    yahoo.now = _at("2026-10-30", "11:00")
    five = yahoo_intraday.get_intraday_bars(symbol, "2026-10-30")
    assert "| Gap at the opening price (daily bar, 25050.00) vs the previous daily close (24900.00) | +0.60% |" in five
    one = yahoo_intraday.get_intraday_bars(symbol, "2026-10-30", sessions=1)
    assert "## Last complete session: Thu 29 Oct 2026 (from the daily bar)" in one
    assert _pivots(one)[0] == "## Classic floor pivots from the 29 Oct 2026 high, low and close"
    for text in (five, one):
        assert ", dividend-adjusted" not in text
        assert GAP_NOTE not in text and DAILY_BAR_NOTE not in text
        assert "ex-dividend" not in text


@pytest.mark.unit
@pytest.mark.parametrize("symbol", ["GC=F", "USDINR=X"])
def test_a_future_or_currency_daily_close_is_not_called_dividend_adjusted(yahoo, symbol):
    # No exchange calendar here: the session is read from the bars' own hours, in their time zone.
    yahoo.daily = _daily(end="2026-10-30")
    yahoo.daily.loc[yahoo.daily["Date"] == pd.Timestamp("2026-10-30"), "Open"] = 101.0
    yahoo.frame = pd.concat([_session(day, "09:30", "16:00", zone=NEW_YORK) for day in ("2026-10-27", "2026-10-28",
                                                                                         "2026-10-29")]
                            + [_session("2026-10-30", "09:30", "11:00", zone=NEW_YORK)])
    yahoo.now = _at("2026-10-30", "11:00", NEW_YORK)
    five = yahoo_intraday.get_intraday_bars(symbol, "2026-10-30")
    assert "| Gap at the opening price (daily bar, 101.00) vs the previous daily close (100.00) | +1.00% |" in five
    one = yahoo_intraday.get_intraday_bars(symbol, "2026-10-30", sessions=1)
    assert "## Last complete session: Thu 29 Oct 2026 (from the daily bar)" in one
    for text in (five, one):
        assert ", dividend-adjusted" not in text
        assert GAP_NOTE not in text and DAILY_BAR_NOTE not in text


@pytest.mark.unit
def test_only_an_instrument_that_can_pay_dividends_has_its_daily_rows_called_adjusted():
    for symbol in ("RELIANCE.NS", "BAJAJ-AUTO.NS", "500325.BO", "NIFTYBEES.NS", "AAPL", "BRK-B", "SPY", "0700.HK"):
        assert yahoo_intraday._dividend_adjusted(symbol), symbol
    for symbol in ("^NSEI", "^BSESN", "^CNXIT", "NIFTY_FIN_SERVICE.NS", "NIFTY_MIDCAP_100.NS", "^GSPC",
                   "USDINR=X", "GC=F", "BTC-USD", "BTC-INR", "ETH-USD"):
        assert not yahoo_intraday._dividend_adjusted(symbol), symbol


@pytest.mark.unit
@pytest.mark.parametrize("symbol", ["000001.SS", "399001.SZ", "FTSEMIB.MI", "^NSEI", "NIFTY_FIN_SERVICE.NS"])
def test_an_index_without_a_caret_is_not_called_dividend_adjusted(symbol):
    """The default benchmark_map names three indices Yahoo lists without a caret."""
    from tradingagents.dataflows.vendors.yahoo import intraday as yahoo_intraday

    assert yahoo_intraday._dividend_adjusted(symbol) is False
    assert yahoo_intraday._dividend_adjusted("TCS.NS") is True and yahoo_intraday._dividend_adjusted("SPY") is True
