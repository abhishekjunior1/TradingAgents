"""The portfolio risk block on books a second review found mishandled: crypto pairs quoted in
INR and other currencies, a lot bought after the run date, an unlabelled book holding a listing
whose currency is not known here, and a holding period configured as a float.

Prices are synthetic and injected, and the exchange clock is fixed; no test reaches a vendor.
"""

import json
from datetime import UTC, date, datetime

import numpy as np
import pandas as pd
import pytest

from tradingagents import portfolio_risk
from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.symbols import (
    crypto_quote_currency,
    is_us_listing,
    listing_zone,
    normalize_symbol,
    resolve_symbol,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.markets import costs, exchanges
from tradingagents.portfolio import PortfolioContext, Position, listing_currency, load_portfolio
from tradingagents.portfolio_risk import book_value, portfolio_risk_block

TRADE_DATE = "2026-10-29"
SATURDAY = datetime(2026, 10, 31, 6, 0, tzinfo=UTC)      # NSE and BSE closed


@pytest.fixture(autouse=True)
def _closed(monkeypatch):
    """The exchanges' session state at a fixed moment, when every session is closed."""
    monkeypatch.setattr(portfolio_risk, "session_status",
                        lambda exchange: exchanges.session_status(exchange, now=SATURDAY))


def _frame(seed: int, last: float, sessions: int = 300, vol: float = 0.01) -> pd.DataFrame:
    """Daily OHLC rows in load_ohlcv's shape, ending on TRADE_DATE at a close of exactly ``last``."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(end=TRADE_DATE, periods=sessions)
    closes = np.cumprod(1 + rng.normal(0, vol, sessions))
    closes = closes * (last / closes[-1])
    closes[-1] = last
    opens = np.r_[closes[0], closes[:-1]]
    return pd.DataFrame({"Date": dates, "Open": opens, "High": np.maximum(opens, closes) * 1.005,
                         "Low": np.minimum(opens, closes) * 0.995, "Close": closes, "Volume": 1_000})


TABLE = {
    "RELIANCE.NS": _frame(1, 1000.0),
    "AAPL": _frame(5, 200.0),
    "VOD.L": _frame(8, 70.0),
    "0700.HK": _frame(9, 600.0),
    "BTC-INR": _frame(21, 7_000_000.0, vol=0.03),
    "ETH-INR": _frame(22, 340_000.0, vol=0.03),
    "BTC-USD": _frame(10, 110_000.0, vol=0.03),
    "ETH-USD": _frame(12, 4_000.0, vol=0.03),
    "^NSEI": _frame(3, 25000.0, vol=0.008),
    "SPY": _frame(7, 650.0, vol=0.008),
}


def _prices(until: str | None = None):
    """Each symbol's rows, up to ``until`` when given, as load_ohlcv serves a run dated then."""
    def prices(symbol):
        if symbol not in TABLE:
            raise NoMarketDataError(symbol, detail="no rows")
        frame = TABLE[symbol].copy()
        return frame if until is None else frame[frame["Date"] <= pd.Timestamp(until)]

    return prices


def _block(book: dict, ticker: str, trade_date: str = TRADE_DATE, **config) -> str:
    return portfolio_risk_block(PortfolioContext.model_validate(book), ticker, trade_date, _prices(trade_date),
                                lambda symbol: None, {**DEFAULT_CONFIG, **config})


def _line(text: str, start: str) -> str:
    return next(line for line in text.splitlines() if line.startswith(start))


def _graph(monkeypatch, **config):
    from tradingagents.graph import trading_graph

    graph = object.__new__(trading_graph.TradingAgentsGraph)
    graph.config = {**DEFAULT_CONFIG, **config}
    monkeypatch.setattr(trading_graph, "load_ohlcv", lambda symbol, as_of: _prices(as_of)(symbol))
    monkeypatch.setattr(trading_graph, "resolve_instrument_identity", lambda symbol: {"sector": None})
    return graph


# --- R25: a crypto pair is priced in the currency it names -----------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("ticker, currency", [
    ("BTC-INR", "INR"), ("eth-inr", "INR"), ("DOGE-INR", "INR"), ("BTC-EUR", "EUR"), ("ETH-GBP", "GBP"),
    ("SOL-JPY", "JPY"), ("BTC-USD", "USD"), ("BTCUSDT", "USD"),
])
def test_a_crypto_pair_is_priced_in_the_currency_it_names(ticker, currency):
    symbol = normalize_symbol(ticker)
    assert listing_currency(symbol) == currency and not is_us_listing(symbol)
    assert Position(ticker=ticker, quantity=1).price_currency("INR") == currency
    assert Position(ticker=ticker, quantity=1).price_currency(None) == currency


@pytest.mark.unit
@pytest.mark.parametrize("ticker", ["AAPL", "BRK-B", "BF-B"])
def test_a_us_share_class_is_still_a_us_listing(ticker):
    assert is_us_listing(ticker) and listing_currency(ticker) == "USD" and crypto_quote_currency(ticker) is None


_CRYPTO_BOOK = {"cash": 500_000, "currency": "INR", "positions": [
    {"ticker": "RELIANCE.NS", "quantity": 100},
    {"ticker": "BTC-INR", "quantity": 0.05, "average_price": 6_000_000}]}


@pytest.mark.unit
def test_an_inr_book_holding_btc_inr_is_in_one_currency_and_totalled():
    text = _block(_CRYPTO_BOOK, "RELIANCE.NS")
    assert "amounts in INR" in text and "several currencies" not in text and "USD" not in text
    assert "- Holdings ₹4,50,000.00; cash ₹5,00,000.00; total ₹9,50,000.00 (cash 52.63%)" in text
    btc = _line(text, "- BTC-INR:")
    assert btc.startswith("- BTC-INR: 0.05 units at last close ₹70,00,000.00 (2026-10-29) = ₹3,50,000.00;")
    assert "unrealized P/L ₹50,000.00 (+16.67%)" in btc
    assert "largest position BTC-INR at 77.78% of holdings" in text
    assert "Sizing reference for RELIANCE.NS" in text
    book = PortfolioContext.model_validate(_CRYPTO_BOOK)
    assert "average price ₹60,00,000.00" in book.render("BTC-INR")
    assert book_value(book, "RELIANCE.NS", _prices()) == pytest.approx(950_000)      # the two agree


@pytest.mark.unit
def test_an_inr_book_sizes_a_crypto_pair_quoted_in_inr_in_rupees():
    text = _block({"cash": 5_000_000, "currency": "INR"}, "ETH-INR")
    assert "last close ₹3,40,000.00" in _line(text, "- Sizing reference for ETH-INR")
    assert "trades in USD" not in text


@pytest.mark.unit
def test_an_inr_book_holding_a_usd_crypto_pair_holds_two_currencies():
    data = {"cash": 100_000, "currency": "INR", "positions": [
        {"ticker": "RELIANCE.NS", "quantity": 100}, {"ticker": "BTCUSDT", "quantity": 1, "average_price": 90_000}]}
    text = _block(data, "RELIANCE.NS")
    assert "several currencies" in text and "Sizing reference: n/a (the book holds several currencies" in text
    for absent in ("- Holdings", "Concentration", "allows about"):
        assert absent not in text
    btc = _line(text, "- BTC-USD:")
    assert "1 units at last close 110,000.00 USD (2026-10-29) = 110,000.00 USD" in btc
    assert "unrealized P/L 20,000.00 USD (+22.22%)" in btc
    assert book_value(PortfolioContext.model_validate(data), "RELIANCE.NS", _prices()) is None


@pytest.mark.unit
def test_a_usd_book_sizes_a_usd_crypto_pair_in_dollars():
    data = {"cash": 100_000, "currency": "USD", "positions": [{"ticker": "AAPL", "quantity": 10}]}
    sizing = _line(_block(data, "ETH-USD"), "- Sizing reference for ETH-USD")
    assert "risking 1% of the book (1,020.00 USD)" in sizing and "below the last close 4,000.00 USD" in sizing
    assert book_value(PortfolioContext.model_validate(data), "ETH-USD", _prices()) == pytest.approx(102_000)


@pytest.mark.unit
@pytest.mark.parametrize("raw, expected", [
    ("BTC-INR", "BTC-INR"), ("eth-eur", "ETH-EUR"), ("BTCUSDT", "BTC-USD"),
    ("RELIANCE", "RELIANCE.NS"), ("BAJAJ-AUTO", "BAJAJ-AUTO.NS"),
])
def test_a_crypto_pair_is_not_read_as_a_listing_of_the_default_exchange(raw, expected):
    assert resolve_symbol(raw, "NSE") == expected


@pytest.mark.unit
def test_a_portfolio_file_read_on_nse_keeps_its_crypto_pair(tmp_path):
    path = tmp_path / "book.json"
    path.write_text(json.dumps({"currency": "INR", "positions": [
        {"ticker": "BTC-INR", "quantity": 0.05}, {"ticker": "TCS", "quantity": 1}]}))
    assert [p.ticker for p in load_portfolio(path, "NSE").positions] == ["BTC-INR", "TCS.NS"]
    assert listing_zone("BTC-INR") is None          # crypto trades around the clock: the machine's date


# --- R26: a lot acquired after the run date is not held on it -------------------------------

_LATER_LOT = {"cash": 500_000, "currency": "INR", "positions": [
    {"ticker": "RELIANCE.NS", "quantity": 300, "average_price": 1007.46, "acquired": "2026-09-15"}]}


@pytest.mark.unit
def test_a_lot_acquired_after_the_run_date_adds_nothing_and_its_price_and_date_stay_out():
    text = _block(_LATER_LOT, "RELIANCE.NS", trade_date="2026-06-01")
    assert "- Holdings ₹0.00; cash ₹5,00,000.00; total ₹5,00,000.00 (cash 100.00%)" in text
    assert "- Left out: 1 lot(s) acquired after 2026-06-01, not held yet on that date." in text
    for absent in ("- RELIANCE.NS:", "unrealized P/L", "1,007.46", "2026-09-15", "already", "Concentration"):
        assert absent not in text
    # Nothing held yet, so nothing caps the add: the sizing is against the cash alone.
    assert "risking 1% of the book (₹5,000.00)" in _line(text, "- Sizing reference for RELIANCE.NS")


@pytest.mark.unit
def test_only_the_lots_held_on_the_run_date_count():
    text = _block({"cash": 100_000, "currency": "INR", "positions": [
        {"ticker": "RELIANCE.NS", "quantity": 100, "average_price": 900, "acquired": "2025-06-02"},
        {"ticker": "RELIANCE.NS", "quantity": 200, "average_price": 950, "acquired": "2026-12-01"}]}, "RELIANCE.NS")
    assert "- Holdings ₹1,00,000.00; cash ₹1,00,000.00; total ₹2,00,000.00 (cash 50.00%)" in text
    assert len([line for line in text.splitlines() if line.startswith("- RELIANCE.NS:")]) == 1
    assert "RELIANCE.NS is already 50.00% of the book" in text
    assert "950.00" not in text and "2026-12-01" not in text


@pytest.mark.unit
def test_a_lot_acquired_on_the_run_date_is_held():
    text = _block({"cash": 100_000, "currency": "INR", "positions": [
        {"ticker": "RELIANCE.NS", "quantity": 10, "average_price": 990, "acquired": TRADE_DATE}]}, "RELIANCE.NS")
    assert "held 0 days (short-term)" in _line(text, "- RELIANCE.NS:") and "Left out" not in text


@pytest.mark.unit
def test_the_book_on_a_date_leaves_out_only_the_lots_acquired_after_it():
    book = PortfolioContext.model_validate({"cash": 1_000, "currency": "INR", "positions": [
        {"ticker": "RELIANCE.NS", "quantity": 1}, {"ticker": "TCS.NS", "quantity": 2, "acquired": "2026-06-01"},
        {"ticker": "INFY.NS", "quantity": 3, "acquired": "2026-06-02"}]})
    on = book.held_on("2026-06-01")
    assert [p.ticker for p in on.positions] == ["RELIANCE.NS", "TCS.NS"] and on.cash == 1_000
    assert len(book.positions) == 3 and book.held_on("2026-06-02") is book       # the book itself is unchanged
    later = PortfolioContext.model_validate(_LATER_LOT).held_on("2026-06-01")
    assert "No current position in RELIANCE.NS" in later.render("RELIANCE.NS")


# --- R27: an unlabelled book holding a listing whose currency is not known here --------------

@pytest.mark.unit
@pytest.mark.parametrize("holding", ["VOD.L", "0700.HK"])
@pytest.mark.parametrize("ticker, sign", [("AAPL", "USD"), ("RELIANCE.NS", "₹")])
def test_an_unlabelled_book_with_a_holding_of_unknown_currency_gets_no_total_or_size(holding, ticker, sign):
    data = {"cash": 2_000, "positions": [{"ticker": holding, "quantity": 5_000, "average_price": 65}]}
    text = _block(data, ticker)
    assert "amounts in each holding's currency" in text and sign not in text
    assert (f"- The book names no currency, and none is known here for the prices of {holding}, so totals, "
            "weights and concentration are not computed.") in text
    assert (f"- Sizing reference: n/a (the book names no currency, and none is known here for the prices of "
            f"{holding}, so the book has no value to size {ticker} against).") in text
    for absent in ("- Holdings", "Concentration", "allows about"):
        assert absent not in text
    assert book_value(PortfolioContext.model_validate(data), ticker, _prices()) is None      # the two agree


@pytest.mark.unit
def test_a_holding_of_unknown_currency_is_not_folded_into_a_known_currencys_total():
    text = _block({"cash": 5_000, "positions": [
        {"ticker": "AAPL", "quantity": 10}, {"ticker": "VOD.L", "quantity": 5_000}]}, "AAPL")
    assert "none is known here for the prices of VOD.L" in text
    for absent in ("- Holdings", "Concentration", "allows about"):
        assert absent not in text
    assert "10 units at last close 200.00 USD (2026-10-29) = 2,000.00 USD" in _line(text, "- AAPL:")
    assert "5,000 units at last close 70.00 (2026-10-29) = 350,000.00;" in _line(text, "- VOD.L:")


@pytest.mark.unit
def test_the_agents_get_no_dollar_total_or_share_count_for_such_a_book(monkeypatch):
    graph = _graph(monkeypatch)
    book = PortfolioContext.model_validate({"cash": 2_000, "positions": [
        {"ticker": "VOD.L", "quantity": 5_000, "average_price": 65}]})
    text = graph._portfolio_context("AAPL", TRADE_DATE, book)
    assert "none is known here for the prices of VOD.L" in text
    assert "USD" not in text and "allows about" not in text
    assert graph._portfolio_value("AAPL", TRADE_DATE, book) is None


@pytest.mark.unit
def test_unlabelled_cash_taken_as_a_currency_says_so():
    assert "cash 50,000.00 USD (the book names no currency, so taken as USD)" in _line(
        _block({"cash": 50_000}, "AAPL"), "- Holdings")
    assert "cash ₹1,00,000.00 (the book names no currency, so taken as INR)" in _line(
        _block({"cash": 100_000, "positions": [{"ticker": "RELIANCE.NS", "quantity": 10}]}, "AAPL"), "- Holdings")
    assert "taken as" not in _block({"cash": 50_000, "currency": "USD"}, "AAPL")


@pytest.mark.unit
def test_a_labelled_book_still_totals_a_holding_in_its_label():
    text = _block({"cash": 2_000, "currency": "HKD", "positions": [{"ticker": "0700.HK", "quantity": 100}]},
                  "0700.HK")
    assert "- Holdings 60,000.00 HKD; cash 2,000.00 HKD; total 62,000.00 HKD (cash 3.23%)" in text
    assert "none is known here" not in text


# --- R28: a whole number of months given as a float ------------------------------------------

@pytest.mark.unit
def test_a_whole_number_of_months_given_as_a_float_is_that_many_months():
    rules = costs.get_capital_gains_rules("india_listed_equity", {"long_term_after_months": 12.0})
    assert rules.long_term_after_months == 12 and type(rules.long_term_after_months) is int
    assert not costs.is_long_term(rules, date(2025, 10, 29), date(2026, 10, 29))
    assert costs.is_long_term(rules, date(2025, 10, 29), date(2026, 10, 30))


@pytest.mark.unit
@pytest.mark.parametrize("months", [12.5, "12", float("nan"), True])
def test_a_holding_period_that_is_not_whole_months_is_refused_naming_the_field(months):
    with pytest.raises(ValueError, match="long_term_after_months must be a whole number of months"):
        costs.get_capital_gains_rules("india_listed_equity", {"long_term_after_months": months})


_NSE_LOT = {"cash": 100_000, "currency": "INR", "positions": [
    {"ticker": "RELIANCE.NS", "quantity": 10, "average_price": 900, "acquired": "2025-01-15"}]}


@pytest.mark.unit
def test_the_agents_keep_the_risk_figures_with_a_float_holding_period(monkeypatch):
    graph = _graph(monkeypatch, capital_gains_overrides={"long_term_after_months": 12.0})
    text = graph._portfolio_context("RELIANCE.NS", TRADE_DATE, PortfolioContext.model_validate(_NSE_LOT))
    assert "Portfolio risk as of 2026-10-29" in text
    assert "held 652 days (long-term)" in _line(text, "- RELIANCE.NS:")


@pytest.mark.unit
def test_a_holding_period_that_is_not_whole_months_is_named_and_spares_a_book_without_indian_listings(
        monkeypatch, caplog):
    graph = _graph(monkeypatch, capital_gains_overrides={"long_term_after_months": 12.5})
    book = PortfolioContext.model_validate(_NSE_LOT)
    assert graph._portfolio_context("RELIANCE.NS", TRADE_DATE, book) == book.render("RELIANCE.NS")
    assert "long_term_after_months must be a whole number of months, got 12.5" in caplog.text
    us_book = PortfolioContext.model_validate({"cash": 10_000, "currency": "USD", "positions": [
        {"ticker": "AAPL", "quantity": 10, "average_price": 150}]})
    assert "unrealized P/L 500.00 USD" in graph._portfolio_context("AAPL", TRADE_DATE, us_book)


@pytest.mark.unit
def test_the_agents_and_the_day_traders_capital_see_the_book_on_the_run_date(monkeypatch):
    """The risk block left a later lot out, but the holdings line above it still read
    'Current position in RELIANCE.NS: 300 units, average price ₹1,007.46', and the Day
    Trader's capital still counted the lot: a look-ahead in a run dated before the purchase."""
    graph = _graph(monkeypatch)
    book = PortfolioContext.model_validate(_LATER_LOT)
    text = graph._portfolio_context("RELIANCE.NS", "2026-06-01", book)
    assert "- No current position in RELIANCE.NS" in text and "1,007.46" not in text
    assert "- Left out: 1 lot(s) acquired after 2026-06-01, not held yet on that date." in text
    assert graph._portfolio_value("RELIANCE.NS", "2026-06-01", book) == 500_000
    assert "- Current position in RELIANCE.NS: 300 units" in graph._portfolio_context("RELIANCE.NS", TRADE_DATE, book)
    assert graph._portfolio_value("RELIANCE.NS", TRADE_DATE, book) == pytest.approx(500_000 + 300 * 1000.0)


@pytest.mark.unit
@pytest.mark.parametrize(("quantity", "shown"), [
    (12_345, "12,345"), (300, "300"), (1_234_567.5, "1,234,567.5"), (0.0125, "0.0125"), (0.00000001, "0.00000001"),
    (2.5e-10, "2.5e-10"), (-40, "-40"),
])
def test_a_holding_is_shown_in_full(quantity, shown):
    """',.4g' showed 12,345 shares as '1.234e+04 units' to the agents and in the risk block."""
    from tradingagents.markets.money import format_quantity

    assert format_quantity(quantity) == shown


@pytest.mark.unit
def test_a_large_holding_reaches_the_agents_and_the_risk_block_in_full():
    book = {"cash": 100_000, "currency": "INR", "positions": [
        {"ticker": "RELIANCE.NS", "quantity": 12_345, "average_price": 900}, {"ticker": "TCS.NS", "quantity": 10_500}]}
    rendered = PortfolioContext.model_validate(book).render("RELIANCE.NS")
    assert "- Current position in RELIANCE.NS: 12,345 units" in rendered and "TCS.NS 10,500" in rendered
    assert "12,345 units at last close" in _line(_block(book, "RELIANCE.NS"), "- RELIANCE.NS:")
    assert "e+04" not in rendered


@pytest.mark.unit
def test_a_benchmark_set_by_name_is_measured_and_named_as_its_index(monkeypatch):
    """TRADINGAGENTS_BENCHMARK_TICKER="NIFTY 50" reached the risk block as written: its label read
    'NIFTY 50' only by chance, and 'nifty 50' or 'BANKNIFTY' read as typed."""
    TABLE["^NSEBANK"] = _frame(31, 52_000.0, vol=0.008)
    try:
        text = _block(_NSE_LOT, "RELIANCE.NS", benchmark_ticker="banknifty")
    finally:
        del TABLE["^NSEBANK"]
    line = _line(text, "- RELIANCE.NS:")
    assert "1Y return vs NIFTY Bank" in line and "banknifty" not in text.lower().replace("nifty bank", "")


@pytest.mark.unit
def test_a_book_in_several_currencies_says_why_it_has_no_sizing_reference():
    text = _block({"cash": 1_000, "currency": "INR", "positions": [{"ticker": "AAPL", "quantity": 10}]}, "RELIANCE.NS")
    assert "- The book holds several currencies" in text
    assert ("- Sizing reference: n/a (the book holds several currencies and no exchange rate is applied here, so it "
            "has no single value to size RELIANCE.NS against).") in text
