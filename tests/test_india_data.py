"""Indian market data: market context, corporate actions, India news searches, and the agents' market briefing.

Every vendor is stubbed; no test reaches Yahoo.
"""

from datetime import date, datetime
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from langchain_core.messages import AIMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode

from tradingagents.agents import tools
from tradingagents.agents.context import build_instrument_context
from tradingagents.dataflows import router
from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.vendors.yahoo import corporate_actions, market_context
from tradingagents.default_config import DEFAULT_CONFIG

AS_OF = "2026-10-28"


def _frame(end: str, sessions: int = 300, start_price: float = 100.0, step: float = 0.001) -> pd.DataFrame:
    dates = pd.bdate_range(end=end, periods=sessions)
    closes = [start_price * (1 + step) ** i for i in range(sessions)]
    return pd.DataFrame({"Date": dates, "Open": closes, "High": closes, "Low": closes, "Close": closes, "Volume": 1})


@pytest.fixture
def yahoo(monkeypatch):
    """Stub load_ohlcv per symbol; a symbol set to an exception raises it."""
    frames = {}

    def load(symbol, as_of_date, fill_gaps=True):
        found = frames.get(symbol, _frame(as_of_date))
        if isinstance(found, Exception):
            raise found
        return found

    monkeypatch.setattr(market_context, "load_ohlcv", load)
    monkeypatch.setattr(market_context, "_classification", lambda s: ("Energy", "Oil & Gas Refining & Marketing"))
    monkeypatch.setattr(market_context, "expected_last_session", lambda exchange, as_of: date.fromisoformat(AS_OF))
    return frames


# --- market context --------------------------------------------------------------

@pytest.mark.unit
def test_an_indian_stock_is_read_against_its_indices_the_rupee_and_crude(yahoo):
    yahoo["^NSEI"] = _frame("2026-10-27")                         # Yahoo a session late
    yahoo["^INDIAVIX"] = NoMarketDataError("^INDIAVIX", detail="no closing prices")
    yahoo["USDINR=X"] = _frame("2026-10-27", start_price=90)      # another calendar: not held to NSE's
    text = market_context.get_market_context("RELIANCE.NS", AS_OF)

    assert text.startswith("# Market context for RELIANCE.NS as of 2026-10-28")
    assert "BSE quotes as 15-minute delayed" in text and "not intended for trading" in text
    for row in ("| NIFTY 50 | ^NSEI |", "| BSE SENSEX | ^BSESN |", "| NIFTY Bank | ^NSEBANK |",
                "| NIFTY Energy (closest sector index) | ^CNXENERGY |", "| USD/INR | USDINR=X |",
                "| Brent crude (USD) | BZ=F |"):
        assert row in text
    nifty = next(line for line in text.splitlines() if line.startswith("| NIFTY 50 |"))
    assert "2026-10-27 (Yahoo has not published the 28 Oct close)" in nifty
    rupee = next(line for line in text.splitlines() if line.startswith("| USD/INR |"))
    assert "not published" not in rupee
    assert "India VIX (^INDIAVIX): unavailable" in text
    assert "- vs NIFTY 50 (^NSEI): 1M" in text and "- vs NIFTY Energy (^CNXENERGY): 1M" in text
    assert "not NSE's index membership" in text


@pytest.mark.unit
def test_changes_are_returns_over_the_last_sessions(yahoo):
    yahoo["^NSEI"] = _frame(AS_OF, step=0.01)
    nifty = next(line for line in market_context.get_market_context("TCS.NS", AS_OF).splitlines()
                 if line.startswith("| NIFTY 50 |"))
    one_day, one_week = nifty.split("|")[5].strip(), nifty.split("|")[6].strip()
    assert (one_day, one_week) == ("+1.00%", f"{(1.01 ** 5 - 1) * 100:+.2f}%")


@pytest.mark.unit
def test_a_short_history_reports_no_change_rather_than_a_number(yahoo):
    yahoo["^NSEI"] = _frame(AS_OF, sessions=10)
    nifty = next(line for line in market_context.get_market_context("TCS.NS", AS_OF).splitlines()
                 if line.startswith("| NIFTY 50 |"))
    assert nifty.rstrip(" |").endswith("n/a | n/a")      # 1M and 3M need 22 and 64 closes


@pytest.mark.unit
def test_an_index_is_not_compared_with_itself(yahoo):
    text = market_context.get_market_context("^NSEI", AS_OF)
    assert "Relative performance" not in text and "closest sector index" not in text


@pytest.mark.unit
def test_a_watchlist_row_flags_a_close_yahoo_has_not_published(yahoo):
    yahoo["TCS.NS"] = _frame("2026-10-27")
    yahoo["GHOST.NS"] = NoMarketDataError("GHOST.NS", detail="no rows")
    text = market_context.get_watchlist_snapshot(["RELIANCE", "TCS.NS", "GHOST.NS"], AS_OF)
    rows = {line.split("|")[1].strip(): line for line in text.splitlines() if line.startswith("| ")}
    assert "2026-10-27 (28 Oct not yet published)" in rows["TCS.NS"]
    assert "not yet published" not in rows["RELIANCE"]           # a bare symbol: no Indian calendar
    assert "unavailable" in rows["GHOST.NS"]
    assert "pts" in rows["TCS.NS"]                                # relative to NIFTY 50


@pytest.mark.unit
def test_a_us_stock_gets_its_configured_benchmark(yahoo):
    text = market_context.get_market_context("AAPL", AS_OF)
    assert "| Benchmark | SPY |" in text and "USD/INR" not in text
    assert "- vs Benchmark (SPY): 1M" in text


# --- corporate actions -----------------------------------------------------------

def _ticker(dividends, splits, calendar):
    return lambda symbol: SimpleNamespace(dividends=dividends, splits=splits, calendar=calendar)


def _series(points):
    index = pd.DatetimeIndex([pd.Timestamp(d, tz="Asia/Kolkata") for d, _ in points])
    return pd.Series([v for _, v in points], index=index, dtype=float)


@pytest.mark.unit
def test_actions_are_served_up_to_the_run_date_with_their_currency(monkeypatch):
    dividends = _series([("2019-08-01", 6.5), ("2025-08-14", 5.5), ("2026-06-05", 6.0), ("2026-11-20", 7.0)])
    splits = _series([("2024-10-28", 2.0)])
    monkeypatch.setattr(corporate_actions.yf, "Ticker", _ticker(dividends, splits, {
        "Earnings Date": [date(2027, 1, 16)], "Ex-Dividend Date": date(2026, 6, 5)}))
    monkeypatch.setattr(corporate_actions, "is_historical", lambda d, symbol=None: False)
    text = corporate_actions.get_corporate_actions("RELIANCE.NS", AS_OF)

    assert "dividends per share in INR" in text
    assert "- 2025-08-14: 5.50" in text and "- 2026-06-05: 6.00" in text
    assert "2026-11-20" not in text and "2019-08-01" not in text      # after the run; outside 5 years
    assert "- 2024-10-28: 2-for-1" in text
    assert "Total per calendar year: 2025 5.50, 2026 6.00" in text
    assert "Next results date (Yahoo estimate or announcement): 2027-01-16" in text
    assert "Latest ex-dividend date: 2026-06-05" in text


@pytest.mark.unit
def test_a_past_run_has_the_event_calendar_withheld(monkeypatch):
    class Ticker:
        dividends = _series([])
        splits = _series([])

        def __init__(self, symbol):
            pass

        @property
        def calendar(self):
            raise AssertionError("a past run must not read the present-day calendar")

    monkeypatch.setattr(corporate_actions.yf, "Ticker", Ticker)
    text = corporate_actions.get_corporate_actions("TCS.NS", "2024-01-05")
    assert "Upcoming events: withheld" in text and "after 2024-01-05" in text


@pytest.mark.unit
def test_a_bse_listing_without_actions_points_to_the_nse_listing(monkeypatch):
    monkeypatch.setattr(corporate_actions.yf, "Ticker", _ticker(_series([]), _series([]), {}))
    monkeypatch.setattr(corporate_actions, "is_historical", lambda d, symbol=None: False)
    text = corporate_actions.get_corporate_actions("RELIANCE.BO", AS_OF)
    assert "none recorded by Yahoo" in text and "RELIANCE.NS may have them" in text


# --- routing and tools -----------------------------------------------------------

@pytest.mark.unit
def test_the_new_methods_are_routed_as_optional_categories():
    for method, category in (("get_market_context", "market_context"), ("get_corporate_actions", "corporate_actions")):
        assert router.get_category_for_method(method) == category
        assert category in router.OPTIONAL_CATEGORIES
        assert DEFAULT_CONFIG["data_vendors"][category] == "yfinance"


@pytest.mark.unit
def test_a_failing_context_vendor_degrades_to_a_notice_not_a_crash(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("socket closed")

    monkeypatch.setitem(router.VENDOR_METHODS["get_market_context"], "yfinance", broken)
    assert router.route_to_vendor("get_market_context", "RELIANCE.NS", AS_OF).startswith("DATA_UNAVAILABLE")


class _State(MessagesState):
    trade_date: str
    company_of_interest: str


def _call(tool, args, company):
    graph = StateGraph(_State)
    graph.add_node("tools", ToolNode([tool]))
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    seen = {}

    def routed(method, *a, **k):
        config = get_config()
        seen.update(method=method, args=a, queries=config["global_news_queries"],
                    groups=config.get("global_news_groups"))
        return "ok"

    with mock.patch.object(tools, "route_to_vendor", side_effect=routed):
        graph.compile().invoke({"messages": [AIMessage("", tool_calls=[{"name": tool.name, "args": args, "id": "1"}])],
                                "trade_date": AS_OF, "company_of_interest": company})
    return seen


@pytest.mark.unit
def test_an_indian_instrument_searches_india_and_global_news_in_two_groups():
    seen = _call(tools.get_global_news, {"curr_date": AS_OF}, "RELIANCE.NS")
    india, world = seen["groups"]
    assert india["label"] == "India" and india["queries"] == ["rupee", "RBI", "India"]
    assert india["fuzzy"] is False and "India" in india["terms"] and "RBI" in india["weak_terms"]
    assert ".NS" in india["tickers"]
    assert world["label"] == "Global" and world["fuzzy"] is True
    assert world["queries"] == DEFAULT_CONFIG["global_news_queries"]
    us = _call(tools.get_global_news, {"curr_date": AS_OF}, "AAPL")
    assert us["queries"] == DEFAULT_CONFIG["global_news_queries"] and us["groups"] is None


@pytest.mark.unit
def test_the_context_tools_serve_the_run_instrument_on_the_run_date():
    seen = _call(tools.get_market_context, {"curr_date": "2026-12-31"}, "RELIANCE.NS")
    assert seen["method"] == "get_market_context" and seen["args"] == ("RELIANCE.NS", AS_OF)
    seen = _call(tools.get_corporate_actions, {}, "TCS.NS")
    assert seen["args"] == ("TCS.NS", AS_OF)


# --- the agents' market briefing -------------------------------------------------

IST = ZoneInfo("Asia/Kolkata")


@pytest.mark.unit
def test_a_current_indian_run_is_told_its_exchange_session_and_rules():
    now = datetime(2031, 10, 30, 11, 5, tzinfo=IST)
    text = build_instrument_context("RELIANCE.NS", "stock", {"company_name": "Reliance Industries Limited"},
                                    trade_date=None, now=now)
    assert "Market: National Stock Exchange of India (NSE), benchmark NIFTY 50 (`^NSEI`)." in text
    assert "NSE is open (regular session)" in text and "still forming" in text
    assert "prices, values and returns are in INR" in text and "Evidence rules:" in text


@pytest.mark.unit
def test_a_historical_indian_run_is_told_its_own_day_not_today():
    text = build_instrument_context("TCS.BO", "stock", {}, trade_date="2024-01-06")
    assert "BSE (Bombay Stock Exchange) (BSE)" in text and "was a weekend" in text
    assert "is open" not in text and "is closed" not in text


@pytest.mark.unit
def test_a_us_run_gets_the_evidence_rules_but_no_indian_briefing():
    text = build_instrument_context("AAPL", "stock", {}, trade_date=None)
    assert "Evidence rules:" in text and "Market:" not in text and "INR" not in text


@pytest.mark.unit
def test_a_bse_listing_is_read_against_sensex_like_the_rest_of_its_run(yahoo):
    """Its market line, report header, settlement and watchlist columns use SENSEX; the
    relative lines read it against NIFTY 50, the plan's first series."""
    yahoo["^BSESN"] = _frame(AS_OF, step=0.002)
    bse = market_context.get_market_context("RELIANCE.BO", AS_OF)
    assert "- vs BSE SENSEX (^BSESN): 1M" in bse and "- vs NIFTY 50 (^NSEI)" not in bse
    nse = market_context.get_market_context("RELIANCE.NS", AS_OF)
    assert "- vs NIFTY 50 (^NSEI): 1M" in nse and "- vs BSE SENSEX (^BSESN)" not in nse
