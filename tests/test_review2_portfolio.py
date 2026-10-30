"""The portfolio block on books a third review found mishandled: a current run that hid lots the
person holds now because their dates were a day ahead of the run's (an Indian investor's dates on a
US run dated by New York's day), and crypto pairs quoted in another coin (ETH-BTC) read as US
listings priced in USD.

Prices are synthetic and injected, and every clock is pinned; no test reaches a vendor.
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from tradingagents import portfolio_risk
from tradingagents.dataflows import date_window
from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.symbols import (
    is_us_listing,
    listing_zone,
    normalize_symbol,
    resolve_symbol,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.markets import exchanges
from tradingagents.portfolio import PortfolioContext, Position, listing_currency
from tradingagents.portfolio_risk import book_value, portfolio_risk_block

IST = ZoneInfo("Asia/Kolkata")
# 03:00 on Friday 30 Oct 2026 in India: 17:30 on Thursday in New York, after its close.
NIGHT = datetime(2026, 10, 30, 3, 0, tzinfo=IST)
# 09:30 on Tuesday 1 Dec 2026 in India, NSE open since 09:15: 23:00 on Monday in New York.
MORNING = datetime(2026, 12, 1, 9, 30, tzinfo=IST)
SATURDAY = datetime(2026, 10, 31, 6, 0, tzinfo=UTC)      # every session closed


def _frame(seed: int, last: float, end: str, sessions: int = 300, vol: float = 0.01) -> pd.DataFrame:
    """Daily OHLC rows in load_ohlcv's shape, ending on ``end`` at a close of exactly ``last``."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(end=end, periods=sessions)
    closes = np.cumprod(1 + rng.normal(0, vol, sessions))
    closes = closes * (last / closes[-1])
    closes[-1] = last
    opens = np.r_[closes[0], closes[:-1]]
    return pd.DataFrame({"Date": dates, "Open": opens, "High": np.maximum(opens, closes) * 1.005,
                         "Low": np.minimum(opens, closes) * 0.995, "Close": closes, "Volume": 1_000})


def _table(end: str) -> dict:
    return {"AAPL": _frame(5, 200.0, end), "RELIANCE.NS": _frame(1, 1000.0, end), "TCS.NS": _frame(2, 2500.0, end),
            "SPY": _frame(7, 650.0, end, vol=0.008), "^NSEI": _frame(3, 25_000.0, end, vol=0.008),
            "ETH-BTC": _frame(31, 0.0289, end, vol=0.02)}


def _prices(table: dict):
    """Each symbol's rows up to a date, as load_ohlcv serves a run dated then."""
    def prices(symbol, as_of=None):
        if symbol not in table:
            raise NoMarketDataError(symbol, detail="no rows")
        frame = table[symbol].copy()
        return frame if as_of is None else frame[frame["Date"] <= pd.Timestamp(as_of)]

    return prices


def _at(monkeypatch, machine_date: str, at: datetime):
    """Pin the machine's date, the instant every listing's own date is read at, and the exchanges' clock."""
    real = date_window.listing_date
    monkeypatch.setattr(date_window, "get_current_date", lambda: machine_date)
    monkeypatch.setattr(date_window, "listing_date", lambda symbol, now=None: real(symbol, now or at))
    monkeypatch.setattr(portfolio_risk, "session_status", lambda exchange: exchanges.session_status(exchange, now=at))


def _graph(monkeypatch, table: dict):
    from tradingagents.graph import trading_graph

    graph = object.__new__(trading_graph.TradingAgentsGraph)
    graph.config = dict(DEFAULT_CONFIG)
    monkeypatch.setattr(trading_graph, "load_ohlcv", _prices(table))
    monkeypatch.setattr(trading_graph, "resolve_instrument_identity", lambda symbol: {"sector": None})
    return graph


def _line(text: str, start: str) -> str:
    return next(line for line in text.splitlines() if line.startswith(start))


# --- R16: a current run counts every lot in the book ----------------------------------------

# Bought at 00:30 IST on 30 Oct, 15:00 on the 29th in New York, and written down with the IST date.
_US_LOT = {"cash": 5_000, "currency": "USD", "positions": [
    {"ticker": "AAPL", "quantity": 10, "average_price": 195, "acquired": "2026-10-30"}]}


@pytest.mark.unit
def test_a_current_us_run_after_midnight_in_india_counts_a_lot_bought_in_that_session(monkeypatch):
    """The run is dated New York's day, the CLI's default; the lot carries the next IST date. The
    agents were told there was no position and given a share count past the 20% maximum."""
    _at(monkeypatch, "2026-10-30", NIGHT)
    run_date = date_window.today_for("AAPL")
    assert run_date == "2026-10-29" and not date_window.is_historical(run_date, "AAPL")
    graph = _graph(monkeypatch, _table("2026-10-29"))
    book = PortfolioContext.model_validate(_US_LOT)

    text = graph._portfolio_context("AAPL", run_date, book)

    assert "- Current position in AAPL: 10 units, average price 195.00 USD" in text
    assert "- Holdings 2,000.00 USD; cash 5,000.00 USD; total 7,000.00 USD (cash 71.43%)" in text
    assert "unrealized P/L 50.00 USD (+2.56%)" in _line(text, "- AAPL:")
    assert ("- Sizing reference: n/a (AAPL is already 28.57% of the book, which leaves no room under the 20% "
            "maximum position for a share at 200.00 USD).") in text
    assert "Left out" not in text and "not held yet" not in text and "No current position" not in text
    assert graph._portfolio_value("AAPL", run_date, book) == pytest.approx(7_000.0)


@pytest.mark.unit
def test_a_current_us_run_counts_an_nse_lot_bought_this_morning_and_taxes_it_as_held_today(monkeypatch):
    """At 09:30 IST New York's day is still the 30th; the NSE lot bought at 09:20 is dated the 1st."""
    _at(monkeypatch, "2026-12-01", MORNING)
    run_date = date_window.today_for("AAPL")
    assert run_date == "2026-11-30"
    graph = _graph(monkeypatch, _table(run_date))
    book = PortfolioContext.model_validate({"cash": 500_000, "currency": "INR", "positions": [
        {"ticker": "RELIANCE.NS", "quantity": 100, "average_price": 900, "acquired": "2026-12-01"},
        {"ticker": "TCS.NS", "quantity": 10, "average_price": 2400, "acquired": "2025-03-03"}]})

    text = graph._portfolio_context("AAPL", run_date, book)

    assert "- Other positions: RELIANCE.NS 100, TCS.NS 10" in text
    assert "- Holdings ₹1,25,000.00; cash ₹5,00,000.00; total ₹6,25,000.00 (cash 80.00%)" in text
    assert "largest position RELIANCE.NS at 80.00% of holdings" in text
    # Sold now it has been held under a day: short-term, 20% of the ₹10,000 gain.
    assert ("unrealized P/L ₹10,000.00 (+11.11%); held 0 days (short-term); estimated tax if sold now ₹2,000.00 "
            in _line(text, "- RELIANCE.NS:"))
    for absent in ("Left out", "not held yet", "no holding period", "acquired after"):
        assert absent not in text


@pytest.mark.unit
def test_a_past_dated_run_on_the_same_night_still_leaves_the_later_lot_out(monkeypatch):
    """R26 stands: a run dated before New York's day is past-dated (a backtest), and must not see the lot."""
    _at(monkeypatch, "2026-10-30", NIGHT)
    assert date_window.is_historical("2026-10-28", "AAPL")
    graph = _graph(monkeypatch, _table("2026-10-29"))
    book = PortfolioContext.model_validate(_US_LOT)

    text = graph._portfolio_context("AAPL", "2026-10-28", book)

    assert "- No current position in AAPL" in text
    assert "- Holdings 0.00 USD; cash 5,000.00 USD; total 5,000.00 USD (cash 100.00%)" in text
    assert "- Left out: 1 lot(s) acquired after 2026-10-28, not held yet on that date." in text
    for absent in ("195.00", "2026-10-30", "unrealized P/L"):
        assert absent not in text
    assert graph._portfolio_value("AAPL", "2026-10-28", book) == pytest.approx(5_000.0)


@pytest.mark.unit
def test_whose_day_decides_a_run_is_past_dated_is_the_analysed_listing_s(monkeypatch):
    """At 03:00 IST an NSE run dated the 29th is past-dated (India's day is the 30th), so the US lot
    bought at 00:30 IST, after India's 29th ended, is left out of it."""
    _at(monkeypatch, "2026-10-30", NIGHT)
    assert date_window.is_historical("2026-10-29", "RELIANCE.NS")
    graph = _graph(monkeypatch, _table("2026-10-29"))
    book = PortfolioContext.model_validate(_US_LOT)

    text = graph._portfolio_context("RELIANCE.NS", "2026-10-29", book)

    assert "- Left out: 1 lot(s) acquired after 2026-10-29, not held yet on that date." in text
    assert "Other positions" not in text and "- AAPL:" not in text


@pytest.mark.unit
def test_the_risk_block_counts_every_lot_only_when_told_the_run_is_current(monkeypatch):
    _at(monkeypatch, "2026-10-30", NIGHT)        # a current run's sale is today's: 30 Oct in India
    book = PortfolioContext.model_validate({"cash": 100_000, "currency": "INR", "positions": [
        {"ticker": "RELIANCE.NS", "quantity": 100, "average_price": 900, "acquired": "2026-10-30"}]})
    prices = _prices(_table("2026-10-29"))

    def block(**kwargs):
        return portfolio_risk_block(book, "RELIANCE.NS", "2026-10-29", prices, lambda symbol: None,
                                    dict(DEFAULT_CONFIG), **kwargs)

    current = block(historical=False)
    assert "- Holdings ₹1,00,000.00; cash ₹1,00,000.00; total ₹2,00,000.00 (cash 50.00%)" in current
    assert "held 0 days (short-term)" in _line(current, "- RELIANCE.NS:") and "Left out" not in current
    for past in (block(), block(historical=True)):         # by default a lot after the date is left out
        assert "- Left out: 1 lot(s) acquired after 2026-10-29, not held yet on that date." in past
        assert "- RELIANCE.NS:" not in past


@pytest.mark.unit
def test_a_current_run_gives_no_holding_period_for_a_lot_dated_past_any_calendar(monkeypatch):
    """A current run counts a lot to today's date on its exchange. A later date (mistyped, say)
    tells nothing about how long the lot has been held, so no period or tax is made up for it."""
    _at(monkeypatch, "2026-10-30", NIGHT)        # 30 Oct in India
    book = PortfolioContext.model_validate({"cash": 100_000, "currency": "INR", "positions": [
        {"ticker": "RELIANCE.NS", "quantity": 100, "average_price": 900, "acquired": "2026-10-31"}]})

    text = portfolio_risk_block(book, "RELIANCE.NS", "2026-10-29", _prices(_table("2026-10-29")),
                                lambda symbol: None, dict(DEFAULT_CONFIG), historical=False)

    line = _line(text, "- RELIANCE.NS:")
    assert line.endswith("unrealized P/L ₹10,000.00 (+11.11%); purchase date 2026-10-31 is after 2026-10-30 "
                         "(today in IST), so no holding period or tax is estimated")
    assert "- Holdings ₹1,00,000.00; cash ₹1,00,000.00; total ₹2,00,000.00 (cash 50.00%)" in text


# --- R17: a crypto pair quoted in another coin is priced in that coin ------------------------

@pytest.mark.unit
@pytest.mark.parametrize("raw, symbol, quote", [
    ("ETH-BTC", "ETH-BTC", "BTC"), ("eth-btc", "ETH-BTC", "BTC"), ("SOL-ETH", "SOL-ETH", "ETH"),
    ("XRP-BTC", "XRP-BTC", "BTC"), ("DOGE-BTC", "DOGE-BTC", "BTC"), ("BNB-BTC", "BNB-BTC", "BTC"),
    ("LINK-ETH", "LINK-ETH", "ETH"), ("PEPE-USDT", "PEPE-USDT", "USDT"), ("UNI-USDC", "UNI-USDC", "USDC"),
])
def test_a_pair_quoted_in_a_coin_is_priced_in_that_coin_and_is_no_us_listing(raw, symbol, quote):
    """Yahoo quotes ETH-BTC in BTC (exchange CCC); it was read as a US share priced in USD."""
    assert normalize_symbol(raw) == symbol
    assert listing_currency(symbol) == quote and not is_us_listing(symbol)
    assert listing_zone(symbol) is None                  # crypto trades around the clock: the machine's day
    assert resolve_symbol(raw, "NSE") == symbol          # not a listing of the default exchange
    assert Position(ticker=raw, quantity=1).price_currency("USD") == quote


@pytest.mark.unit
@pytest.mark.parametrize("ticker", ["BRK-B", "BF-B", "BAC-PL", "PBR-A"])
def test_a_us_share_class_or_series_is_still_a_us_listing(ticker):
    assert is_us_listing(ticker) and listing_currency(ticker) == "USD" and listing_zone(ticker).key == "America/New_York"


@pytest.mark.unit
def test_a_usd_book_gets_no_dollar_size_for_a_pair_priced_in_btc(monkeypatch):
    monkeypatch.setattr(portfolio_risk, "session_status", lambda exchange: exchanges.session_status(exchange,
                                                                                                    now=SATURDAY))
    prices = _prices(_table("2026-10-29"))
    book = PortfolioContext.model_validate({"cash": 100_000, "currency": "USD"})

    text = portfolio_risk_block(book, "ETH-BTC", "2026-10-29", prices, lambda symbol: None, dict(DEFAULT_CONFIG))

    assert ("- Sizing reference: n/a (the book is in USD and ETH-BTC trades in BTC; no exchange rate is "
            "applied).") in text
    assert "allows about" not in text
    assert book_value(book, "ETH-BTC", prices) is None


@pytest.mark.unit
def test_a_usd_book_holding_a_pair_priced_in_btc_holds_two_currencies(monkeypatch):
    monkeypatch.setattr(portfolio_risk, "session_status", lambda exchange: exchanges.session_status(exchange,
                                                                                                    now=SATURDAY))
    prices = _prices(_table("2026-10-29"))
    book = PortfolioContext.model_validate({"cash": 100_000, "currency": "USD", "positions": [
        {"ticker": "ETH-BTC", "quantity": 10}]})

    text = portfolio_risk_block(book, "AAPL", "2026-10-29", prices, lambda symbol: None, dict(DEFAULT_CONFIG))

    assert "- The book holds several currencies, so totals, weights and concentration are not computed." in text
    eth = _line(text, "- ETH-BTC:")
    assert eth.startswith("- ETH-BTC: 10 units at last close 0.0289 BTC (2026-10-29) = 0.29 BTC;") and "USD" not in eth
    for absent in ("- Holdings", "Concentration", "allows about"):
        assert absent not in text
    assert "Sizing reference: n/a (the book holds several currencies" in text
    assert book_value(book, "AAPL", prices) is None
