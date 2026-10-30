"""The intraday snapshot tool as wired: run date, instrument and paper capital from the run.

A run given a portfolio sizes its paper examples against the portfolio's value,
when that value can stand for capital in the listing's currency; otherwise the
configured intraday_paper_capital, and the snapshot says which.
"""

from __future__ import annotations

from unittest import mock

import pandas as pd
import pytest
from langchain_core.messages import AIMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode

from tradingagents.agents import tools
from tradingagents.dataflows import router
from tradingagents.graph import trading_graph
from tradingagents.graph.propagation import Propagator
from tradingagents.portfolio import PortfolioContext
from tradingagents.portfolio_risk import book_value

TRADE_DATE = "2026-10-28"


class _State(MessagesState):
    trade_date: str
    company_of_interest: str
    portfolio_value: float | None


def _snapshot_call(args, **state):
    """get_intraday_snapshot run through a ToolNode; returns the router call's (args, kwargs)."""
    graph = StateGraph(_State)
    graph.add_node("tools", ToolNode([tools.get_intraday_snapshot]))
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    with mock.patch.object(tools, "route_to_vendor", return_value="snapshot") as routed:
        graph.compile().invoke({
            "messages": [AIMessage("", tool_calls=[{"name": "get_intraday_snapshot", "args": args, "id": "1"}])],
            "trade_date": TRADE_DATE, "company_of_interest": "RELIANCE.NS", **state,
        })
    return routed.call_args.args, routed.call_args.kwargs


# --- the tool -------------------------------------------------------------------------

@pytest.mark.unit
def test_a_run_with_a_portfolio_sizes_against_its_value():
    args, kwargs = _snapshot_call({}, portfolio_value=1_250_000.0)
    assert args == ("get_intraday_bars", "RELIANCE.NS", TRADE_DATE, "5m", 5)
    assert kwargs["capital"] == 1_250_000.0
    assert "portfolio's value" in kwargs["capital_source"]


@pytest.mark.unit
@pytest.mark.parametrize("state", [{}, {"portfolio_value": None}, {"portfolio_value": 0.0}],
                         ids=["no field (an older checkpoint)", "no portfolio", "nothing to value"])
def test_without_a_portfolio_value_the_configured_capital_is_used(state):
    _, kwargs = _snapshot_call({}, **state)
    assert kwargs == {"capital": None, "capital_source": None}


@pytest.mark.unit
@pytest.mark.parametrize("asked, served", [(50, 10), (0, 1), (-3, 1), (7, 7)])
def test_the_sessions_asked_for_are_kept_between_one_and_ten(asked, served):
    args, _ = _snapshot_call({"sessions": asked})
    assert args[-1] == served


@pytest.mark.unit
def test_a_direct_call_outside_a_run_is_dated_today(monkeypatch):
    monkeypatch.setattr(tools, "get_current_date", lambda: "2026-10-29")
    with mock.patch.object(tools, "route_to_vendor", return_value="snapshot") as routed:
        tools.get_intraday_snapshot.func("TCS.NS")
    assert routed.call_args.args == ("get_intraday_bars", "TCS.NS", "2026-10-29", "5m", 5)


@pytest.mark.unit
def test_an_intraday_failure_degrades_to_a_sentinel(monkeypatch):
    def broken(*a, **k):
        raise ValueError("intraday_square_off must be a time as HH:MM, got '3pm'")

    monkeypatch.setitem(router.VENDOR_METHODS, "get_intraday_bars", {"yfinance": broken})
    out = router.route_to_vendor("get_intraday_bars", "RELIANCE.NS", TRADE_DATE, "5m", 5, capital=None)
    assert out.startswith("DATA_UNAVAILABLE: optional intraday_data could not be retrieved")


# --- the portfolio's value ------------------------------------------------------------

def _prices(closes: dict[str, float]):
    def prices(symbol):
        if symbol not in closes:
            raise LookupError(f"no prices for {symbol}")
        return pd.DataFrame({"Date": pd.bdate_range(end=TRADE_DATE, periods=2), "Close": [1.0, closes[symbol]]})
    return prices


def _book(**data):
    return PortfolioContext.model_validate(data)


@pytest.mark.unit
def test_an_indian_book_is_valued_at_its_last_closes_plus_cash():
    book = _book(cash=50_000, positions=[{"ticker": "RELIANCE.NS", "quantity": 10},
                                         {"ticker": "TCS.BO", "quantity": 5}])
    assert book_value(book, "INFY.NS", _prices({"RELIANCE.NS": 1400.0, "TCS.BO": 3000.0})) == 79_000.0


@pytest.mark.unit
def test_a_short_holding_adds_nothing_as_in_the_risk_figures():
    book = _book(cash=10_000, positions=[{"ticker": "RELIANCE.NS", "quantity": -10}])
    assert book_value(book, "RELIANCE.NS", _prices({"RELIANCE.NS": 1400.0})) == 10_000.0


@pytest.mark.unit
@pytest.mark.parametrize("book, ticker", [
    (_book(cash=1000, currency="USD"), "RELIANCE.NS"),                              # cash in another currency
    (_book(cash=1000, currency="INR"), "AAPL"),
    (_book(positions=[{"ticker": "AAPL", "quantity": 1}]), "RELIANCE.NS"),         # a US holding for an NSE trade
    (_book(cash=1000), "VOD.L"),                                                    # the listing's currency unknown
    (_book(positions=[{"ticker": "VOD.L", "quantity": 1}], cash=10), "AAPL"),      # a holding's currency unknown
    (_book(), "RELIANCE.NS"),                                                       # nothing to value
    (_book(positions=[{"ticker": "WIPRO.NS", "quantity": 1}], cash=10), "RELIANCE.NS"),   # no prices
], ids=["usd cash, nse", "inr cash, us", "us holding, nse", "unknown listing", "unknown holding", "empty",
        "unpriced holding"])
def test_a_book_that_cannot_stand_for_capital_in_the_listings_currency_is_not_used(book, ticker):
    assert book_value(book, ticker, _prices({"AAPL": 200.0, "VOD.L": 70.0})) is None


@pytest.mark.unit
def test_a_holding_whose_prices_have_no_close_leaves_the_book_unvalued():
    book = _book(cash=50_000, positions=[{"ticker": "RELIANCE.NS", "quantity": 10}])
    no_close = pd.DataFrame({"Date": pd.bdate_range(end=TRADE_DATE, periods=2), "Close": [float("nan")] * 2})
    assert book_value(book, "RELIANCE.NS", lambda symbol: no_close) is None


@pytest.mark.unit
def test_a_us_book_is_valued_for_a_us_listing():
    book = _book(cash=5_000, currency="usd", positions=[{"ticker": "MSFT", "quantity": 10}])
    assert book_value(book, "AAPL", _prices({"MSFT": 500.0})) == 10_000.0
    assert book_value(_book(cash=5_000), "AAPL", _prices({})) == 5_000.0     # unlabelled cash: the listing's


def _bare_graph(analysts=("market", "day_trader")):
    graph = object.__new__(trading_graph.TradingAgentsGraph)
    graph.config = {"portfolio_risk": False}
    graph.propagator = Propagator()
    graph.selected_analysts = analysts
    graph.resolve_instrument_context = lambda t, a="stock", d=None: ""
    return graph


@pytest.mark.unit
def test_a_run_given_a_portfolio_starts_with_its_value(monkeypatch):
    monkeypatch.setattr(trading_graph, "load_ohlcv", lambda symbol, as_of: _prices({"RELIANCE.NS": 1400.0})(symbol))
    book = _book(cash=60_000, positions=[{"ticker": "RELIANCE.NS", "quantity": 100}])

    state = _bare_graph().create_run_state("RELIANCE.NS", TRADE_DATE, portfolio=book)

    assert state["portfolio_value"] == 200_000.0
    assert _bare_graph().create_run_state("RELIANCE.NS", TRADE_DATE)["portfolio_value"] is None


@pytest.mark.unit
def test_a_run_without_the_day_trader_does_not_value_the_book(monkeypatch):
    """Only the Day Trader's paper sizes use the value, so other runs fetch no holding's prices for it."""
    fetched = []
    monkeypatch.setattr(trading_graph, "load_ohlcv", lambda symbol, as_of: fetched.append(symbol))
    book = _book(cash=60_000, positions=[{"ticker": "RELIANCE.NS", "quantity": 100}])

    state = _bare_graph(("market", "news")).create_run_state("RELIANCE.NS", TRADE_DATE, portfolio=book)

    assert state["portfolio_value"] is None and fetched == []
    assert "RELIANCE.NS" in state["portfolio_context"]


@pytest.mark.unit
def test_a_portfolio_whose_value_cannot_be_found_leaves_the_configured_capital(monkeypatch):
    def unavailable(symbol, as_of):
        raise OSError("Yahoo unreachable")

    monkeypatch.setattr(trading_graph, "load_ohlcv", unavailable)
    book = _book(cash=60_000, positions=[{"ticker": "RELIANCE.NS", "quantity": 100}])

    state = _bare_graph().create_run_state("RELIANCE.NS", TRADE_DATE, portfolio=book)

    assert state["portfolio_value"] is None and "RELIANCE.NS" in state["portfolio_context"]
