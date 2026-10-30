"""The portfolio risk block given to the trader, risk and portfolio agents, on books it got wrong:
currencies mixed in the sizing and the totals, lots of one instrument, an add to a position already
held, a session still trading, short histories, shorts, the 12-month tax boundary, negative cash,
a purchase after the run date, and an index as the analysed instrument.

Prices are synthetic and injected, and the exchange clock is fixed; no test reaches a vendor.
"""

from datetime import UTC, date, datetime

import numpy as np
import pandas as pd
import pytest

from tradingagents import portfolio_risk
from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.markets import costs, exchanges, risk
from tradingagents.markets.money import format_money, format_percent
from tradingagents.portfolio import PortfolioContext, Position
from tradingagents.portfolio_risk import book_value, portfolio_risk_block

TRADE_DATE = "2026-10-29"
SATURDAY = datetime(2026, 10, 31, 6, 0, tzinfo=UTC)      # NSE and BSE closed
TRADING = datetime(2026, 10, 30, 8, 0, tzinfo=UTC)       # Fri 30 Oct 2026, 13:30 IST: NSE is open
CLOSED = datetime(2026, 10, 30, 11, 0, tzinfo=UTC)       # 16:30 IST: the day's close is final


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    """The exchanges' session state at a fixed moment (closed unless a test moves it)."""
    def at(now):
        monkeypatch.setattr(portfolio_risk, "session_status",
                            lambda exchange: exchanges.session_status(exchange, now=now), raising=False)
    at(SATURDAY)
    return at


def _frame(seed: int, last: float, sessions: int = 300, end: str = TRADE_DATE, vol: float = 0.01) -> pd.DataFrame:
    """Daily OHLC rows in load_ohlcv's shape, ending on ``end`` at a close of exactly ``last``."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(end=end, periods=sessions)
    closes = np.cumprod(1 + rng.normal(0, vol, sessions))
    closes = closes * (last / closes[-1])
    closes[-1] = last
    opens = np.r_[closes[0], closes[:-1]]
    return pd.DataFrame({"Date": dates, "Open": opens, "High": np.maximum(opens, closes) * 1.005,
                         "Low": np.minimum(opens, closes) * 0.995, "Close": closes, "Volume": 1_000})


TABLE = {
    "RELIANCE.NS": _frame(1, 1000.0),
    "TCS.NS": _frame(2, 2500.0),
    "AAPL": _frame(5, 200.0),
    "MSFT": _frame(6, 500.0),
    "VOD.L": _frame(8, 70.0),
    "^NSEI": _frame(3, 25000.0, vol=0.008),
    "SPY": _frame(7, 650.0, vol=0.008),
    "NIFTY_FIN_SERVICE.NS": _frame(9, 27000.0, vol=0.008),
    "NEWIPO.NS": _frame(11, 500.0, sessions=70, vol=0.03),
}


def _prices(table=None):
    table = TABLE if table is None else table

    def prices(symbol):
        if symbol not in table:
            raise NoMarketDataError(symbol, detail="no rows")
        return table[symbol].copy()

    return prices


def _block(book: dict, ticker: str, trade_date: str = TRADE_DATE, table=None, **config) -> str:
    sectors = {"RELIANCE.NS": "Energy", "TCS.NS": "Technology", "AAPL": "Technology"}
    return portfolio_risk_block(PortfolioContext.model_validate(book), ticker, trade_date, _prices(table),
                                sectors.get, {**DEFAULT_CONFIG, **config})


def _line(text: str, start: str) -> str:
    return next(line for line in text.splitlines() if line.startswith(start))


def _lines(text: str, start: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith(start)]


# --- #46: the sizing reference is in one currency or not given -------------------------

@pytest.mark.unit
def test_an_inr_book_gives_no_share_count_for_a_us_listing():
    text = _block({"cash": 1_000_000, "currency": "INR", "positions": [{"ticker": "RELIANCE.NS", "quantity": 100}]},
                  "AAPL")
    assert "- Sizing reference: n/a (the book is in INR and AAPL trades in USD; no exchange rate is applied)." in text
    assert "Sizing reference for AAPL" not in text and "₹200.00" not in text


@pytest.mark.unit
def test_a_usd_book_gives_no_share_count_or_rupee_charges_for_an_nse_listing():
    text = _block({"cash": 10_000, "currency": "USD", "positions": [
        {"ticker": "AAPL", "quantity": 20}, {"ticker": "MSFT", "quantity": 10}]}, "RELIANCE.NS")
    assert ("- Sizing reference: n/a (the book is in USD and RELIANCE.NS trades in INR; "
            "no exchange rate is applied).") in text
    assert "Estimated charges" not in text
    assert "total 19,000.00 USD" in text          # the book's own figures are still given


@pytest.mark.unit
def test_an_unlabelled_book_of_nse_holdings_is_in_rupees_for_a_us_listing():
    text = _block({"cash": 1_000_000, "positions": [{"ticker": "RELIANCE.NS", "quantity": 100}]}, "AAPL")
    assert "n/a (the book is in INR and AAPL trades in USD" in text


@pytest.mark.unit
def test_unlabelled_cash_alone_counts_as_the_listings_currency():
    text = _block({"cash": 50_000}, "AAPL")
    assert "amounts in USD" in text
    sizing = _line(text, "- Sizing reference for AAPL")
    assert "risking 1% of the book (500.00 USD)" in sizing and "last close 200.00 USD" in sizing


@pytest.mark.unit
def test_a_listing_whose_currency_is_not_known_gets_no_share_count():
    text = _block({"cash": 50_000, "currency": "GBP"}, "VOD.L")
    assert "- Sizing reference: n/a (the currency VOD.L trades in is not known here" in text
    assert book_value(PortfolioContext.model_validate({"cash": 50_000, "currency": "GBP"}), "VOD.L", _prices()) is None


@pytest.mark.unit
def test_the_agents_get_no_rupee_share_count_for_a_us_listing(monkeypatch):
    from tradingagents.graph import trading_graph

    graph = object.__new__(trading_graph.TradingAgentsGraph)
    graph.config = {**DEFAULT_CONFIG}
    monkeypatch.setattr(trading_graph, "load_ohlcv", lambda symbol, as_of: TABLE[symbol].copy())
    monkeypatch.setattr(trading_graph, "resolve_instrument_identity", lambda s: {"sector": None})
    book = PortfolioContext.model_validate({"cash": 1_000_000, "currency": "INR", "positions": [
        {"ticker": "RELIANCE.NS", "quantity": 100}, {"ticker": "TCS.NS", "quantity": 20}]})
    text = graph._portfolio_context("AAPL", TRADE_DATE, book)
    assert "n/a (the book is in INR and AAPL trades in USD" in text and "allows about" not in text


# --- #47: a US holding is priced in USD whatever the book's label -------------------------

@pytest.mark.unit
@pytest.mark.parametrize("ticker, label, expected", [
    ("AAPL", "INR", "USD"), ("aapl", None, "USD"), ("RELIANCE.NS", "USD", "INR"), ("NSE:RELIANCE", None, "INR"),
    ("VOD.L", "GBP", "GBP"), ("VOD.L", None, None),
])
def test_a_holdings_prices_are_in_its_listings_currency_where_known(ticker, label, expected):
    assert Position(ticker=ticker, quantity=1).price_currency(label) == expected


@pytest.mark.unit
def test_an_inr_book_holding_a_us_stock_holds_two_currencies():
    data = {"cash": 200_000, "currency": "INR", "positions": [
        {"ticker": "RELIANCE.NS", "quantity": 100, "average_price": 1250},
        {"ticker": "AAPL", "quantity": 50, "average_price": 180}]}
    text = _block(data, "RELIANCE.NS")
    assert "several currencies" in text and "amounts in each holding's currency" in text
    for absent in ("- Holdings", "Concentration", "Sector exposure", "allows about"):
        assert absent not in text
    assert "Sizing reference: n/a (the book holds several currencies" in text
    aapl = _line(text, "- AAPL:")
    assert "50 units at last close 200.00 USD (2026-10-29) = 10,000.00 USD" in aapl
    assert "unrealized P/L 1,000.00 USD (+11.11%)" in aapl
    assert "= ₹1,00,000.00" in _line(text, "- RELIANCE.NS:")
    book = PortfolioContext.model_validate(data)
    assert "average price 180.00 USD" in book.render("AAPL")
    assert book_value(book, "RELIANCE.NS", _prices()) is None       # the two agree


@pytest.mark.unit
def test_an_unlabelled_book_holding_us_and_nse_stocks_holds_two_currencies():
    text = _block({"positions": [{"ticker": "RELIANCE.NS", "quantity": 100}, {"ticker": "AAPL", "quantity": 20}]},
                  "RELIANCE.NS")
    assert "several currencies" in text and "- Holdings" not in text and "allows about" not in text
    assert "Sizing reference: n/a (the book holds several currencies" in text


# --- #48: lots of one instrument add up ----------------------------------------------------

@pytest.mark.unit
def test_every_lot_of_an_instrument_counts_in_the_totals_and_concentration():
    data = {"cash": 100_000, "currency": "INR", "positions": [
        {"ticker": "RELIANCE.NS", "quantity": 100, "average_price": 900, "acquired": "2024-06-03"},
        {"ticker": "RELIANCE.NS", "quantity": 50, "average_price": 1300, "acquired": "2026-05-04"},
        {"ticker": "TCS.NS", "quantity": 10}]}
    text = _block(data, "RELIANCE.NS")
    assert "- Holdings ₹1,75,000.00; cash ₹1,00,000.00; total ₹2,75,000.00 (cash 36.36%)" in text
    assert "largest position RELIANCE.NS at 85.71% of holdings" in text
    assert "Energy 85.7%, Technology 14.3%" in text
    assert "RELIANCE.NS is already 54.55% of the book" in text           # both lots, of the whole book
    assert "risking 1% of the book (₹2,750.00)" in _block(data, "TCS.NS")
    assert len(_lines(text, "- RELIANCE.NS:")) == 2                       # each lot keeps its own line
    assert book_value(PortfolioContext.model_validate(data), "RELIANCE.NS", _prices()) == pytest.approx(275_000)


@pytest.mark.unit
def test_one_stock_under_two_spellings_counts_once_per_lot():
    text = _block({"cash": 100_000, "currency": "INR", "positions": [
        {"ticker": "RELIANCE.NS", "quantity": 100}, {"ticker": "NSE:RELIANCE", "quantity": 100},
        {"ticker": "TCS.NS", "quantity": 10}]}, "RELIANCE.NS")
    assert "- Holdings ₹2,25,000.00; cash ₹1,00,000.00; total ₹3,25,000.00" in text


@pytest.mark.unit
def test_an_unpriced_instrument_held_in_two_lots_is_named_once():
    text = _block({"cash": 1_000, "currency": "INR", "positions": [
        {"ticker": "GHOST.NS", "quantity": 1}, {"ticker": "GHOST.NS", "quantity": 2}]}, "RELIANCE.NS")
    assert "No prices up to 2026-10-29 for: GHOST.NS (left out of the figures)." in text


# --- #49: the maximum position counts what is already held -----------------------------

def _held(shares: int, **config) -> str:
    book = {"cash": 1_000_000 - shares * 1000, "currency": "INR",
            "positions": [{"ticker": "RELIANCE.NS", "quantity": shares}] if shares else []}
    return _block(book, "RELIANCE.NS", **config)


@pytest.mark.unit
def test_with_nothing_held_the_sizing_reference_reads_as_before():
    assert ("allows about 200 shares, ₹2,00,000.00, held to the 20% maximum position."
            in _line(_held(0), "- Sizing reference for RELIANCE.NS"))


@pytest.mark.unit
def test_an_add_is_capped_so_the_position_stays_within_the_maximum():
    sizing = _line(_held(100), "- Sizing reference for RELIANCE.NS")
    assert ("allows about 100 shares, ₹1,00,000.00; with the ₹1,00,000.00 already held, the position would be "
            "20.00% of the book, held to the 20% maximum position.") in sizing


@pytest.mark.unit
def test_an_add_within_the_maximum_says_where_the_position_would_stand():
    sizing = _line(_held(100, max_position_pct=50), "- Sizing reference for RELIANCE.NS")
    shares = int(sizing.split("allows about ")[1].split(" shares")[0].replace(",", ""))
    assert (100 + shares) * 1000 <= 500_000 and "held to the" not in sizing
    weight = format_percent((100 + shares) * 1000 / 1_000_000, signed=False)
    assert f"with the ₹1,00,000.00 already held, the position would be {weight} of the book." in sizing


@pytest.mark.unit
@pytest.mark.parametrize("shares, weight", [(200, "20.00%"), (400, "40.00%")])
def test_a_position_at_or_over_the_maximum_gets_no_add(shares, weight):
    text = _held(shares)
    assert (f"- Sizing reference: n/a (RELIANCE.NS is already {weight} of the book, which leaves no room under "
            f"the 20% maximum position for a share at ₹1,000.00).") in text
    assert "allows about" not in text


@pytest.mark.unit
def test_a_share_dearer_than_the_maximum_position_says_so():
    text = _block({"cash": 4_000, "currency": "INR"}, "RELIANCE.NS")
    assert ("- Sizing reference: n/a (the 20% maximum position (₹800.00) buys no whole share of RELIANCE.NS "
            "at ₹1,000.00).") in text


# --- #50: a session still trading is not a close -------------------------------------------

def _forming_table():
    """RELIANCE.NS and NIFTY 50 with a row for 30 Oct 2026, a day that jumps 10% on a wide range."""
    table = {symbol: _frame(seed, last, end="2026-10-29") for symbol, seed, last in
             (("RELIANCE.NS", 1, 1000.0), ("^NSEI", 3, 25000.0))}
    for symbol, frame in table.items():
        prior = float(frame["Close"].iloc[-1])
        today = pd.DataFrame({"Date": [pd.Timestamp("2026-10-30")], "Open": [prior], "High": [prior * 1.12],
                              "Low": [prior * 0.99], "Close": [prior * 1.10], "Volume": [400]})
        table[symbol] = pd.concat([frame, today], ignore_index=True)
    return table


_FORMING_BOOK = {"cash": 500_000, "currency": "INR", "positions": [
    {"ticker": "RELIANCE.NS", "quantity": 100, "average_price": 900, "acquired": "2025-01-15"}]}


@pytest.mark.unit
def test_a_session_still_trading_reads_as_the_latest_price_and_stays_out_of_the_statistics(clock):
    clock(TRADING)
    table = _forming_table()
    text = _block(_FORMING_BOOK, "RELIANCE.NS", trade_date="2026-10-30", table=table)
    reliance = _line(text, "- RELIANCE.NS:")
    assert reliance.startswith("- RELIANCE.NS: 100 units at latest price ₹1,100.00 (2026-10-30 13:30 IST, "
                               "session still trading; not a close, which is final after 16:00 IST) = ₹1,10,000.00")
    complete = portfolio_risk._closes(table["RELIANCE.NS"]).iloc[:-1]
    bench = portfolio_risk._closes(table["^NSEI"]).iloc[:-1]
    vol = risk.annualized_volatility(complete.iloc[-251:])
    assert vol != pytest.approx(risk.annualized_volatility(portfolio_risk._closes(table["RELIANCE.NS"]).iloc[-251:]))
    assert f"1Y volatility {format_percent(vol, signed=False)}" in reliance
    gap = risk.relative_return(complete, bench, 250)
    assert f"1Y return vs NIFTY 50 {gap * 100:+.1f} pts" in reliance
    stop = 1100.0 - 2 * risk.average_true_range(table["RELIANCE.NS"].iloc[:-1])
    sizing = _line(text, "- Sizing reference for RELIANCE.NS")
    assert (f"below the latest price ₹1,100.00 (13:30 IST, session still trading; stop {format_money(stop, 'INR')})"
            in sizing)
    assert "use closes through the last complete session, without the session still trading" in text


@pytest.mark.unit
def test_the_closing_session_is_still_trading(clock):
    clock(datetime(2026, 10, 30, 10, 15, tzinfo=UTC))     # 15:45 IST: closing auction and post-close
    text = _block(_FORMING_BOOK, "RELIANCE.NS", trade_date="2026-10-30", table=_forming_table())
    assert "100 units at latest price ₹1,100.00 (2026-10-30 15:45 IST, session still trading;" in text


@pytest.mark.unit
def test_a_run_dated_before_today_reads_its_last_row_as_a_close_while_the_market_trades(clock):
    clock(TRADING)
    text = _block(_FORMING_BOOK, "RELIANCE.NS")             # rows end 29 Oct; NSE trades on 30 Oct
    assert "100 units at last close ₹1,000.00 (2026-10-29)" in text and "session still trading" not in text


@pytest.mark.unit
def test_after_the_close_the_same_row_is_the_last_close(clock):
    clock(CLOSED)
    table = _forming_table()
    text = _block(_FORMING_BOOK, "RELIANCE.NS", trade_date="2026-10-30", table=table)
    reliance = _line(text, "- RELIANCE.NS:")
    assert "100 units at last close ₹1,100.00 (2026-10-30) = ₹1,10,000.00" in reliance
    vol = risk.annualized_volatility(portfolio_risk._closes(table["RELIANCE.NS"]).iloc[-251:])
    assert f"1Y volatility {format_percent(vol, signed=False)}" in reliance
    assert "below the last close ₹1,100.00 (stop" in text
    assert "session still trading" not in text


# --- #51: under a year of history the figures say their span ------------------------------

@pytest.mark.unit
def test_a_recent_listing_is_not_labelled_with_a_one_year_figure():
    text = _block({"cash": 500_000, "currency": "INR", "positions": [{"ticker": "NEWIPO.NS", "quantity": 100}]},
                  "NEWIPO.NS")
    line = _line(text, "- NEWIPO.NS:")
    closes = portfolio_risk._closes(TABLE["NEWIPO.NS"])
    vol = format_percent(risk.annualized_volatility(closes), signed=False)
    depth = format_percent(risk.max_drawdown(closes).depth)
    assert f"; volatility {vol} over the 70 sessions since 2026-07-24;" in line
    assert f"; max drawdown {depth} over the 70 sessions since 2026-07-24;" in line
    assert "1Y volatility" not in line and "1Y max drawdown" not in line and "1Y return vs NIFTY 50 n/a" in line
    assert "1Y volatility" in _block({"currency": "INR", "positions": [{"ticker": "TCS.NS", "quantity": 1}]},
                                     "TCS.NS")


# --- #52: a short in profit reads as a gain -----------------------------------------------

@pytest.mark.unit
def test_a_short_positions_unrealized_pl_percentage_has_the_pls_sign():
    text = _block({"cash": 100_000, "currency": "INR", "positions": [
        {"ticker": "TCS.NS", "quantity": -10, "average_price": 3200}]}, "RELIANCE.NS")
    assert "unrealized P/L ₹7,000.00 (+21.88%)" in _line(text, "- TCS.NS:")


@pytest.mark.unit
def test_a_long_positions_percentage_is_unchanged_and_a_zero_quantity_has_none():
    text = _block({"cash": 100_000, "currency": "INR", "positions": [
        {"ticker": "TCS.NS", "quantity": 10, "average_price": 2000},
        {"ticker": "RELIANCE.NS", "quantity": 0, "average_price": 1000}]}, "RELIANCE.NS")
    assert "unrealized P/L ₹5,000.00 (+25.00%)" in _line(text, "- TCS.NS:")
    assert "unrealized P/L ₹0.00 (n/a)" in _line(text, "- RELIANCE.NS:")


# --- #53: the 12-month boundary is counted in calendar months -----------------------------

@pytest.mark.unit
@pytest.mark.parametrize("acquired, sold, long_term", [
    ("2025-10-29", "2026-10-29", False),     # exactly 12 months
    ("2025-10-29", "2026-10-30", True),
    ("2023-06-15", "2024-06-15", False),     # exactly 12 months, 366 days across 29 Feb 2024
    ("2023-06-15", "2024-06-16", True),
    ("2027-03-01", "2028-03-01", False),     # 29 Feb 2028 inside
    ("2027-03-01", "2028-03-02", True),
    ("2024-02-29", "2025-02-28", False),     # 12 months from 29 Feb end on 28 Feb
    ("2024-02-29", "2025-03-01", True),
])
def test_long_term_means_held_longer_than_twelve_calendar_months(acquired, sold, long_term):
    rules = costs.get_capital_gains_rules("india_listed_equity")
    assert costs.is_long_term(rules, date.fromisoformat(acquired), date.fromisoformat(sold)) is long_term


@pytest.mark.unit
def test_the_term_given_decides_the_tax():
    rules = costs.get_capital_gains_rules("india_listed_equity")
    assert costs.capital_gains_tax(rules, 200_000, 366, long_term=False) == pytest.approx(40_000)
    assert costs.capital_gains_tax(rules, 200_000, 366, long_term=True) == pytest.approx(9_375)
    six_months = costs.get_capital_gains_rules("india_listed_equity", {"long_term_after_months": 6})
    assert not costs.is_long_term(six_months, date(2026, 2, 28), date(2026, 8, 28))
    assert costs.is_long_term(six_months, date(2026, 2, 28), date(2026, 8, 29))


@pytest.mark.unit
def test_an_exact_twelve_months_across_29_february_is_taxed_short_term():
    table = {"RELIANCE.NS": _frame(1, 2500.0, end="2024-06-14"), "^NSEI": _frame(3, 22000.0, end="2024-06-14")}
    text = _block({"currency": "INR", "positions": [
        {"ticker": "RELIANCE.NS", "quantity": 100, "average_price": 500, "acquired": "2023-06-15"}]},
        "RELIANCE.NS", trade_date="2024-06-15", table=table)
    assert "held 366 days (short-term); estimated tax if sold now ₹40,000.00" in text


# --- #54: negative cash is money owed ------------------------------------------------------

@pytest.mark.unit
def test_cash_owed_beyond_the_holdings_gives_no_cash_share():
    text = _block({"cash": -200_000, "currency": "INR", "positions": [{"ticker": "RELIANCE.NS", "quantity": 100}]},
                  "RELIANCE.NS")
    assert ("- Holdings ₹1,00,000.00; cash -₹2,00,000.00 (negative: money owed); total -₹1,00,000.00 "
            "(the book owes more than its holdings are worth)") in text
    assert "(cash " not in text


@pytest.mark.unit
def test_cash_owed_within_the_holdings_gives_no_negative_cash_share():
    text = _block({"cash": -50_000, "currency": "INR", "positions": [{"ticker": "RELIANCE.NS", "quantity": 100}]},
                  "RELIANCE.NS")
    holdings = _line(text, "- Holdings")
    assert holdings == "- Holdings ₹1,00,000.00; cash -₹50,000.00 (negative: money owed); total ₹50,000.00"


# --- #55: a purchase after the run date has no holding period ------------------------------

@pytest.mark.unit
def test_a_lot_bought_after_the_run_date_gets_no_holding_period_or_tax():
    # The lot was not held yet, so it is left out whole (tests/test_review_portfolio.py, R26).
    text = _block({"cash": 100_000, "currency": "INR", "positions": [
        {"ticker": "RELIANCE.NS", "quantity": 10, "average_price": 900, "acquired": "2026-12-01"}]}, "RELIANCE.NS")
    assert "- Left out: 1 lot(s) acquired after 2026-10-29, not held yet on that date." in text
    for absent in ("- RELIANCE.NS:", "held -", "estimated tax", "2026-12-01"):
        assert absent not in text


# --- #56: an index is not sized in shares --------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("ticker, symbol", [("NIFTY 50", "^NSEI"), ("FINNIFTY", "NIFTY_FIN_SERVICE.NS")])
def test_an_index_gets_no_share_count_or_equity_charges(ticker, symbol):
    text = _block({"cash": 1_000_000, "currency": "INR"}, ticker)
    assert (f"- Sizing reference: n/a ({symbol} is an index, which is not traded as shares; "
            "size an ETF or a futures lot on it instead).") in text
    assert "Estimated charges" not in text and "allows about" not in text
