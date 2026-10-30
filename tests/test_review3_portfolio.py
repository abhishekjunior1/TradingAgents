"""The portfolio block and the market context on runs a fourth review found mishandled: an intraday
run that measured the book, and a US listing's market context, against the swing benchmark it no
longer checks (India VIX among them); a current US run that counted older NSE lots to New York's
date and only its newest lot to India's; and prices of pairs quoted in a coin printed at two
decimals (0.00 BTC).

Prices are synthetic and injected, and every clock is pinned; no test reaches a vendor.
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from tradingagents import portfolio_risk
from tradingagents.dataflows import date_window
from tradingagents.dataflows.config import run_config
from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.vendors.yahoo import market_context
from tradingagents.dataflows.vendors.yahoo.common import price_text
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.markets import exchanges, risk
from tradingagents.markets.money import format_price
from tradingagents.portfolio import PortfolioContext
from tradingagents.portfolio_risk import portfolio_risk_block

IST = ZoneInfo("Asia/Kolkata")
TRADE_DATE = "2026-10-29"
SATURDAY = datetime(2026, 10, 31, 6, 0, tzinfo=UTC)      # every session closed
# 09:30 on Tuesday 1 Dec 2026 in India, NSE open since 09:15: 23:00 on Monday in New York.
MORNING = datetime(2026, 12, 1, 9, 30, tzinfo=IST)


def _frame(seed: int, last: float, end: str = TRADE_DATE, sessions: int = 300, vol: float = 0.01) -> pd.DataFrame:
    """Daily OHLC rows in load_ohlcv's shape, ending on ``end`` at a close of exactly ``last``."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(end=end, periods=sessions)
    closes = np.cumprod(1 + rng.normal(0, vol, sessions))
    closes = closes * (last / closes[-1])
    closes[-1] = last
    opens = np.r_[closes[0], closes[:-1]]
    return pd.DataFrame({"Date": dates, "Open": opens, "High": np.maximum(opens, closes) * 1.005,
                         "Low": np.minimum(opens, closes) * 0.995, "Close": closes, "Volume": 1_000})


def _table(end: str = TRADE_DATE) -> dict:
    return {
        "RELIANCE.NS": _frame(1, 1000.0, end), "AAPL": _frame(5, 200.0, end), "NVDA": _frame(6, 180.0, end),
        "7203.T": _frame(8, 2_800.0, end),
        "^NSEI": _frame(3, 25_000.0, end, vol=0.008), "^CNXIT": _frame(42, 36_000.0, end, vol=0.012),
        "^INDIAVIX": _frame(41, 12.0, end, vol=0.05), "^VIX": _frame(43, 17.0, end, vol=0.06),
        "SPY": _frame(7, 650.0, end, vol=0.008), "QQQ": _frame(9, 600.0, end, vol=0.009),
        "^N225": _frame(10, 48_000.0, end, vol=0.009),
        "XRP-BTC": _frame(31, 1.613e-05, end, vol=0.03), "DOGE-BTC": _frame(32, 9.8e-07, end, vol=0.04),
        "SOL-ETH": _frame(33, 0.04238, end, vol=0.03), "ETH-BTC": _frame(34, 0.028907, end, vol=0.02),
    }


def _prices(table: dict):
    """Each symbol's rows up to a date, as load_ohlcv serves a run dated then."""
    def prices(symbol, as_of=None):
        if symbol not in table:
            raise NoMarketDataError(symbol, detail="no rows")
        frame = table[symbol].copy()
        return frame if as_of is None else frame[frame["Date"] <= pd.Timestamp(as_of)]

    return prices


@pytest.fixture(autouse=True)
def _closed(monkeypatch):
    """The exchanges' session state at a fixed moment, when every session is closed."""
    monkeypatch.setattr(portfolio_risk, "session_status",
                        lambda exchange: exchanges.session_status(exchange, now=SATURDAY))


def _at(monkeypatch, machine_date: str, at: datetime):
    """Pin the machine's date, the instant every listing's own date is read at, and the exchanges' clock."""
    real = date_window.listing_date
    monkeypatch.setattr(date_window, "get_current_date", lambda: machine_date)
    monkeypatch.setattr(date_window, "listing_date", lambda symbol, now=None: real(symbol, now or at))
    monkeypatch.setattr(portfolio_risk, "session_status", lambda exchange: exchanges.session_status(exchange, now=at))


def _graph(monkeypatch, table: dict, **config):
    from tradingagents.graph import trading_graph

    graph = object.__new__(trading_graph.TradingAgentsGraph)
    graph.config = {**DEFAULT_CONFIG, **config}
    monkeypatch.setattr(trading_graph, "load_ohlcv", _prices(table))
    monkeypatch.setattr(trading_graph, "resolve_instrument_identity", lambda symbol: {"sector": None})
    return graph


def _block(book: dict, ticker: str, table: dict | None = None, trade_date: str = TRADE_DATE, **config) -> str:
    prices = _prices(table or _table())
    return portfolio_risk_block(PortfolioContext.model_validate(book), ticker, trade_date,
                                lambda symbol: prices(symbol, trade_date), lambda symbol: None,
                                {**DEFAULT_CONFIG, **config})


def _line(text: str, start: str) -> str:
    return next(line for line in text.splitlines() if line.startswith(start))


# --- R8, R11, R22: an intraday run measures nothing against benchmark_ticker -----------------

_NSE_BOOK = {"cash": 100_000, "currency": "INR", "positions": [
    {"ticker": "RELIANCE.NS", "quantity": 100, "average_price": 900}]}


@pytest.mark.unit
@pytest.mark.parametrize("benchmark", ["INDIA VIX", "^INDIAVIX", "NIFTY IT", "NIFTY50X", "^VIX"])
@pytest.mark.parametrize("horizon", ["intraday", " Intraday "])
def test_an_intraday_run_measures_the_book_against_the_listing_s_index(benchmark, horizon):
    """An intraday run no longer checks benchmark_ticker, and the agents are told 'benchmark
    NIFTY 50', yet the risk block read it: 'beta to India VIX -0.01; 1Y return vs India VIX
    +39.3 pts', 'beta to NIFTY IT' or, for a symbol with no prices, no beta at all."""
    text = _block(_NSE_BOOK, "RELIANCE.NS", decision_horizon=horizon, benchmark_ticker=benchmark)
    line = _line(text, "- RELIANCE.NS:")
    expected = _line(_block(_NSE_BOOK, "RELIANCE.NS", decision_horizon="intraday"), "- RELIANCE.NS:")
    assert "beta to NIFTY 50 " in line and "1Y return vs NIFTY 50 " in line and line == expected
    for absent in ("VIX", "NIFTY IT", "NIFTY50X"):
        assert absent not in text


@pytest.mark.unit
def test_a_swing_run_still_measures_the_book_against_its_own_benchmark():
    line = _line(_block(_NSE_BOOK, "RELIANCE.NS", benchmark_ticker="NIFTY IT"), "- RELIANCE.NS:")
    assert "beta to NIFTY IT " in line and "1Y return vs NIFTY IT " in line and "NIFTY 50" not in line


@pytest.mark.unit
def test_an_intraday_run_on_a_us_listing_measures_the_book_against_its_index():
    book = {"cash": 10_000, "currency": "USD", "positions": [{"ticker": "AAPL", "quantity": 10}]}
    intraday = _line(_block(book, "NVDA", decision_horizon="intraday", benchmark_ticker="^VIX"), "- AAPL:")
    assert "beta to SPY " in intraday and "VIX" not in intraday
    assert "beta to QQQ " in _line(_block(book, "NVDA", benchmark_ticker="QQQ"), "- AAPL:")


class _Client:
    def get_llm(self):
        return FakeListChatModel(responses=["never asked"])


@pytest.mark.unit
def test_an_intraday_graph_with_india_vix_gives_its_agents_no_figure_against_it(tmp_path, monkeypatch):
    """Built as the CLI builds it, an intraday graph checks no benchmark, and its run state gave the
    Day Trader, risk and Portfolio Manager agents a beta and a 1Y return against India VIX; its
    market context plan for AAPL was [('Benchmark', '^INDIAVIX')]."""
    from tradingagents.graph import trading_graph

    monkeypatch.setattr(trading_graph, "create_tier_client", lambda config, tier, **kw: _Client())
    monkeypatch.setattr(trading_graph, "load_ohlcv", _prices(_table()))
    monkeypatch.setattr(trading_graph, "resolve_instrument_identity", lambda symbol: {"sector": None})
    monkeypatch.setattr(trading_graph, "plan_session", lambda ticker, trade_date: {})
    graph = trading_graph.TradingAgentsGraph(["market"], config={
        **DEFAULT_CONFIG, "results_dir": str(tmp_path / "results"), "data_cache_dir": str(tmp_path / "cache"),
        "memory_log_path": str(tmp_path / "memory.md"), "decision_horizon": "intraday",
        "benchmark_ticker": "INDIA VIX"})
    graph.resolve_instrument_context = lambda *args, **kwargs: ""

    state = graph.create_run_state("RELIANCE.NS", TRADE_DATE, portfolio=PortfolioContext.model_validate(_NSE_BOOK))

    assert state["decision_horizon"] == "intraday"
    assert "beta to NIFTY 50 " in _line(state["portfolio_context"], "- RELIANCE.NS:")
    assert "India VIX" not in state["portfolio_context"]
    with run_config(graph._run_scope("AAPL")):
        assert market_context._series_plan("AAPL")[0] == [("Benchmark", "SPY")]


def _market(monkeypatch, table: dict) -> None:
    monkeypatch.setattr(market_context, "load_ohlcv", lambda symbol, as_of: _prices(table)(symbol, as_of))


@pytest.mark.unit
def test_an_intraday_run_reads_a_us_listing_against_its_own_index(monkeypatch):
    """The Day Trader is told to read get_market_context for the benchmark; on an intraday run
    NVDA's showed '| Benchmark | ^VIX |' and '- vs Benchmark (^VIX): ...'."""
    _market(monkeypatch, _table())

    with run_config({"decision_horizon": "intraday", "benchmark_ticker": "^VIX"}):
        text = market_context.get_market_context("NVDA", TRADE_DATE)
        assert market_context._series_plan("7203.T")[0] == [("Benchmark", "^N225")]

    assert "| Benchmark | SPY | 650.00 | 2026-10-29 |" in text and "- vs Benchmark (SPY): 1M " in text
    assert "VIX" not in text


@pytest.mark.unit
def test_a_swing_run_still_reads_a_us_listing_against_its_own_benchmark(monkeypatch):
    _market(monkeypatch, _table())

    with run_config({"decision_horizon": "swing", "benchmark_ticker": "QQQ"}):
        text = market_context.get_market_context("NVDA", TRADE_DATE)

    assert "| Benchmark | QQQ | 600.00 | 2026-10-29 |" in text and "- vs Benchmark (QQQ): 1M " in text


@pytest.mark.unit
def test_an_intraday_watchlist_row_is_measured_against_the_listing_s_index(monkeypatch):
    table = _table()
    _market(monkeypatch, table)
    gap = risk.relative_return(market_context._closes("NVDA", TRADE_DATE),
                               market_context._closes("SPY", TRADE_DATE), 21)

    with run_config({"decision_horizon": "intraday", "benchmark_ticker": "^VIX"}):
        row = _line(market_context.get_watchlist_snapshot(["NVDA"], TRADE_DATE), "| NVDA |")

    assert f"| {gap * 100:+.1f} pts |" in row


# --- R20: a current run counts every NSE lot to India's date ---------------------------------

_ANNIVERSARY = {"cash": 100_000, "currency": "INR", "positions": [
    {"ticker": "RELIANCE.NS", "quantity": 1_000, "average_price": 500, "acquired": "2025-11-30"},
    {"ticker": "RELIANCE.NS", "quantity": 10, "average_price": 990, "acquired": "2026-12-01"}]}


def _taxes(text: str) -> list[str]:
    return [line[line.index("unrealized"):] for line in text.splitlines() if line.startswith("- RELIANCE.NS:")]


@pytest.mark.unit
def test_a_current_us_run_counts_every_nse_lot_to_india_s_date(monkeypatch):
    """At 09:30 IST a US run is dated New York's 30 Nov. The lot bought on 30 Nov 2025 read 'held
    365 days (short-term); estimated tax if sold now ₹1,00,000.00', while a RELIANCE.NS run at the
    same instant, and a sale now on NSE, count to 1 Dec: long-term, ₹46,875."""
    _at(monkeypatch, "2026-12-01", MORNING)
    graph = _graph(monkeypatch, _table("2026-11-30"))
    book = PortfolioContext.model_validate(_ANNIVERSARY)
    taxes = {}
    for ticker, run_date in (("AAPL", "2026-11-30"), ("RELIANCE.NS", "2026-12-01")):
        assert date_window.today_for(ticker) == run_date and not date_window.is_historical(run_date, ticker)
        taxes[ticker] = _taxes(graph._portfolio_context(ticker, run_date, book))

    assert taxes["AAPL"] == taxes["RELIANCE.NS"]
    old, new = taxes["AAPL"]
    assert old.startswith("unrealized P/L ₹5,00,000.00 (+100.00%); held 366 days (long-term); estimated tax if "
                          "sold now ₹46,875.00 ")
    assert new.startswith("unrealized P/L ₹100.00 (+1.01%); held 0 days (short-term); estimated tax if sold now "
                          "₹20.00 ")


@pytest.mark.unit
def test_a_machine_whose_date_is_behind_india_s_counts_an_nse_lot_to_india_s_date(monkeypatch):
    """A UTC server at 00:30 IST still reads 30 Nov, so a current NSE run is dated the 30th; a sale
    now on NSE is on 1 Dec, a year and a day after the purchase."""
    _at(monkeypatch, "2026-11-30", datetime(2026, 12, 1, 0, 30, tzinfo=IST))
    graph = _graph(monkeypatch, _table("2026-11-30"))
    run_date = date_window.today_for("RELIANCE.NS")
    assert run_date == "2026-11-30" and not date_window.is_historical(run_date, "RELIANCE.NS")

    old, new = _taxes(graph._portfolio_context("RELIANCE.NS", run_date, PortfolioContext.model_validate(_ANNIVERSARY)))

    assert "; held 366 days (long-term); estimated tax if sold now ₹46,875.00 " in old
    assert "; held 0 days (short-term)" in new


@pytest.mark.unit
def test_a_past_dated_run_still_counts_its_lots_to_its_own_date(monkeypatch):
    """At the same instant a RELIANCE.NS run dated 30 Nov is past-dated (India's day is the 1st):
    a backtest asks what a sale on its own date would have cost."""
    _at(monkeypatch, "2026-12-01", MORNING)
    graph = _graph(monkeypatch, _table("2026-11-30"))
    assert date_window.is_historical("2026-11-30", "RELIANCE.NS")

    text = graph._portfolio_context("RELIANCE.NS", "2026-11-30", PortfolioContext.model_validate(_ANNIVERSARY))

    (old,) = _taxes(text)
    assert "; held 365 days (short-term); estimated tax if sold now ₹1,00,000.00 " in old
    assert "- Left out: 1 lot(s) acquired after 2026-11-30, not held yet on that date." in text


@pytest.mark.unit
def test_a_current_run_gives_no_holding_period_for_a_lot_dated_after_india_s_today(monkeypatch):
    _at(monkeypatch, "2026-12-01", MORNING)
    graph = _graph(monkeypatch, _table("2026-11-30"))
    book = PortfolioContext.model_validate({"cash": 100_000, "currency": "INR", "positions": [
        {"ticker": "RELIANCE.NS", "quantity": 10, "average_price": 900, "acquired": "2026-12-02"}]})

    (line,) = _taxes(graph._portfolio_context("AAPL", "2026-11-30", book))

    assert line == ("unrealized P/L ₹1,000.00 (+11.11%); purchase date 2026-12-02 is after 2026-12-01 (today in "
                    "IST), so no holding period or tax is estimated")


# --- R23: a price keeps its significant digits -----------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize(("value", "currency", "shown"), [
    (0.028907, "BTC", "0.028907 BTC"),
    (1.613304266356863e-05, "BTC", "0.000016133 BTC"),
    (9.799999816095806e-07, "BTC", "0.00000098 BTC"),
    (0.04238000139594078, "ETH", "0.04238 ETH"),
    (2_478.0, "INR", "₹2,478.00"),
    (123_456.789, "INR", "₹1,23,456.79"),
    (912.625, "INR", "₹912.625"),
    (7_000_000.0, "inr", "₹70,00,000.00"),
    (200.0, "USD", "200.00 USD"),
    (1_234.5, "USD", "1,234.50 USD"),
    (70.0, None, "70.00"),
    (0.0, "BTC", "0.00 BTC"),
    (-0.0289, "BTC", "-0.0289 BTC"),
    (-912.63, "INR", "-₹912.63"),
    (None, "BTC", "n/a"),
    (float("nan"), "INR", "n/a"),
    (float("inf"), "USD", "n/a"),
])
def test_a_price_keeps_its_significant_digits(value, currency, shown):
    assert format_price(value, currency) == shown


@pytest.mark.unit
@pytest.mark.parametrize("value", [1.613304266356863e-05, 9.8e-07, 0.028907, 0.5, 1.23456789, 70.123456, 912.6312,
                                   2478.123, 123_456.789, 0.0])
def test_a_price_has_the_digits_the_market_tools_show(value):
    assert format_price(value, None).replace(",", "") == price_text(value)


@pytest.mark.unit
def test_prices_of_pairs_quoted_in_a_coin_are_not_shown_as_zero():
    """XRP-BTC at 0.000016 BTC read 'average price 0.00 BTC', 'last close 0.00 BTC' and 'for a share
    at 0.00 BTC'."""
    book = PortfolioContext.model_validate({"cash": 0.5, "currency": "BTC", "positions": [
        {"ticker": "XRP-BTC", "quantity": 100_000, "average_price": 1.5e-05}]})
    prices = _prices(_table())

    rendered = book.render("XRP-BTC")
    text = portfolio_risk_block(book, "XRP-BTC", TRADE_DATE, prices, lambda symbol: None, dict(DEFAULT_CONFIG))

    assert "- Current position in XRP-BTC: 100,000 units, average price 0.000015 BTC" in rendered
    assert _line(text, "- XRP-BTC:").startswith("- XRP-BTC: 100,000 units at last close 0.00001613 BTC (2026-10-29) "
                                               "= 1.61 BTC;")
    assert "for a share at 0.00001613 BTC)." in _line(text, "- Sizing reference")
    assert "0.00 BTC" not in rendered + text


@pytest.mark.unit
def test_a_book_holding_two_pairs_quoted_in_btc_shows_both_prices():
    book = {"cash": 10_000, "currency": "USD", "positions": [
        {"ticker": "XRP-BTC", "quantity": 100_000, "average_price": 1.5e-05},
        {"ticker": "DOGE-BTC", "quantity": 1_000_000, "average_price": 1.2e-06}]}

    text = _block(book, "XRP-BTC")

    assert "1,000,000 units at last close 0.00000098 BTC (2026-10-29) = 0.98 BTC;" in _line(text, "- DOGE-BTC:")
    assert "average price 0.0000012 BTC" in PortfolioContext.model_validate(book).render("DOGE-BTC")


@pytest.mark.unit
def test_a_stop_below_a_price_quoted_in_eth_reads_apart_from_its_entry():
    """SOL-ETH's sizing read 'below the last close 0.04 ETH (stop 0.04 ETH)'."""
    table = _table()
    stop = 0.04238 - 2 * risk.average_true_range(table["SOL-ETH"])

    sizing = _line(_block({"cash": 2, "currency": "ETH"}, "SOL-ETH", table), "- Sizing reference for SOL-ETH")

    assert f"below the last close 0.04238 ETH (stop {format_price(stop, 'ETH')}) allows about" in sizing
    assert format_price(stop, "ETH") != "0.04238 ETH" and "0.04 ETH" not in sizing
