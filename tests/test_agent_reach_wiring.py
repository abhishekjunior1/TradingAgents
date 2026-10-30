"""Agent-Reach as wired into the router, the config and the News Analyst.

Off (the default) nothing changes: the news chain, the News Analyst's tools and
its prompt are as before, and a "default" chain never starts Agent-Reach. On,
news is read through Agent-Reach first with Yahoo after it, unless the user chose
the news vendors, and the News Analyst can also read web pages and videos.
"""

from __future__ import annotations

import copy

import pandas as pd
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

import tradingagents.default_config as default_config
from cli.prompts import parse_vendors
from tradingagents.agents import context
from tradingagents.agents.analysts import news_analyst, sentiment_analyst
from tradingagents.dataflows import router
from tradingagents.dataflows.config import run_config, set_config
from tradingagents.dataflows.errors import (
    NoMarketDataError,
    VendorNotConfiguredError,
    VendorUnavailableError,
)
from tradingagents.dataflows.vendors.yahoo import market as yahoo_market, snapshot
from tradingagents.graph import setup as graph_setup, trading_graph
from tradingagents.graph.analyst_execution import build_analyst_execution_plan
from tradingagents.graph.conditional_logic import ConditionalLogic

TRADE_DATE = "2026-10-28"


def _raises(exc):
    def impl(*a, **k):
        raise exc
    return impl


def _vendors(monkeypatch, method, **impls):
    monkeypatch.setitem(router.VENDOR_METHODS, method, impls)


# --- the router ---------------------------------------------------------------------

@pytest.mark.unit
def test_agent_reach_serves_both_news_methods():
    assert router.VENDOR_METHODS["get_news"]["agent_reach"] is router.get_news_agent_reach
    assert router.VENDOR_METHODS["get_global_news"]["agent_reach"] is router.get_global_news_agent_reach
    for method in ("read_web_page", "get_video_coverage", "get_video_transcript"):
        assert router.get_category_for_method(method) == "web_research"
        assert list(router.VENDOR_METHODS[method]) == ["agent_reach"]
    assert {"web_research", "intraday_data"} <= router.OPTIONAL_CATEGORIES


@pytest.mark.unit
@pytest.mark.parametrize("failure", [
    VendorNotConfiguredError("Agent-Reach is not installed"),
    VendorUnavailableError("Agent-Reach web search failed: timed out (TIMEOUT)"),
], ids=["not installed", "failed"])
def test_yahoo_serves_the_news_when_agent_reach_cannot(monkeypatch, failure):
    set_config({"data_vendors": {"news_data": "agent_reach,yfinance"}})
    _vendors(monkeypatch, "get_news", agent_reach=_raises(failure), yfinance=lambda *a: "yahoo news")
    assert router.route_to_vendor("get_news", "RELIANCE.NS", "2026-10-21", TRADE_DATE) == "yahoo news"


@pytest.mark.unit
@pytest.mark.parametrize("method, args", [
    ("get_news", ("AAPL", "2025-05-01", "2025-05-09")),
    ("get_global_news", ("2025-05-09", None, None)),
])
def test_yahoo_serves_the_news_when_agent_reach_has_none_in_the_window(monkeypatch, method, args):
    """A past-dated run whose Exa results are undated or outside its window gets Yahoo's
    answer, whatever it is, rather than a line saying the web search found nothing."""
    set_config({"data_vendors": {"news_data": "agent_reach,yfinance"}})
    nothing = NoMarketDataError("AAPL", detail="no Agent-Reach web search (Exa) result is dated in the window")
    _vendors(monkeypatch, method, agent_reach=_raises(nothing), yfinance=lambda *a: "yahoo news")
    assert router.route_to_vendor(method, *args) == "yahoo news"


@pytest.mark.unit
@pytest.mark.parametrize("method, args, subject", [
    ("get_news", ("NSE:RELIANCE", "2025-05-01", "2025-05-09"),
     "news for 'NSE:RELIANCE' (resolved to 'RELIANCE.NS')"),
    ("get_global_news", ("2025-05-09", None, None), "global news"),
])
def test_a_news_chain_with_nothing_in_the_window_never_blames_the_symbol(monkeypatch, method, args, subject):
    set_config({"data_vendors": {"news_data": "agent_reach,alpha_vantage"}})
    detail = "no Agent-Reach web search (Exa) result is dated in 2025-05-01..2025-05-09"
    _vendors(monkeypatch, method, agent_reach=_raises(NoMarketDataError(args[0], "RELIANCE.NS", detail)),
             alpha_vantage=_raises(VendorNotConfiguredError("no key")))
    out = router.route_to_vendor(method, *args)
    assert out.startswith(f"NO_DATA_AVAILABLE: No configured vendor found {subject} in the requested window "
                          f"({detail}).")
    assert "has not shown an absence of news" in out and "unavailable" in out
    assert "invalid" not in out and "delisted" not in out
    # Other methods keep the market-data wording.
    set_config({"data_vendors": {"news_data": "yfinance"}})
    _vendors(monkeypatch, "get_insider_transactions", yfinance=_raises(NoMarketDataError("AAPL", detail="none")))
    assert "may be invalid" in router.route_to_vendor("get_insider_transactions", "AAPL", "2025-05-09")


@pytest.mark.unit
def test_agent_reach_news_comes_first_when_it_answers(monkeypatch):
    set_config({"data_vendors": {"news_data": "agent_reach,yfinance"}})
    _vendors(monkeypatch, "get_global_news", agent_reach=lambda *a: "web news", yfinance=_raises(AssertionError()))
    assert router.route_to_vendor("get_global_news", TRADE_DATE, None, None) == "web news"


@pytest.mark.unit
@pytest.mark.parametrize("enabled", [False, True])
def test_a_default_news_chain_starts_agent_reach_only_when_it_is_on(monkeypatch, enabled):
    started = []
    set_config({"agent_reach_enabled": enabled, "data_vendors": {"news_data": "default"}})
    _vendors(monkeypatch, "get_news", alpha_vantage=_raises(VendorNotConfiguredError("no key")),
             yfinance=_raises(VendorUnavailableError("down")),
             agent_reach=lambda *a: started.append(1) or "web news")
    out = router.route_to_vendor("get_news", "AAPL", "2026-10-21", TRADE_DATE)
    assert (out == "web news") is enabled and bool(started) is enabled


@pytest.mark.unit
def test_web_research_left_unset_needs_agent_reach_switched_on(monkeypatch):
    started = []
    _vendors(monkeypatch, "read_web_page", agent_reach=lambda *a: started.append(1) or "page")
    out = router.route_to_vendor("read_web_page", "https://example.com/a", TRADE_DATE)
    assert out.startswith("DATA_UNAVAILABLE: optional web_research") and "agent_reach_enabled" in out
    assert not started
    set_config({"agent_reach_enabled": True})
    assert router.route_to_vendor("read_web_page", "https://example.com/a", TRADE_DATE) == "page"


@pytest.mark.unit
def test_a_named_web_research_vendor_is_used_without_the_switch(monkeypatch):
    set_config({"data_vendors": {"web_research": "agent_reach"}})
    _vendors(monkeypatch, "get_video_transcript", agent_reach=lambda *a: "captions")
    assert router.route_to_vendor("get_video_transcript", "dQw4w9WgXcQ", TRADE_DATE) == "captions"


@pytest.mark.unit
def test_agent_reach_missing_degrades_web_research_to_its_own_message(monkeypatch):
    set_config({"agent_reach_enabled": True})
    message = "Agent-Reach is not installed, or agent_reach_command does not point to it; see docs/agent-reach.md"
    _vendors(monkeypatch, "get_video_coverage", agent_reach=_raises(VendorNotConfiguredError(message)))
    out = router.route_to_vendor("get_video_coverage", "Reliance results", TRADE_DATE, None)
    assert out.startswith("DATA_UNAVAILABLE: optional web_research could not be retrieved") and message in out


@pytest.mark.unit
def test_the_vendor_flag_takes_the_new_categories():
    assert parse_vendors(["news_data=agent_reach,yfinance", "web_research=agent_reach",
                          "intraday_data=yfinance"]) == {
        "news_data": "agent_reach,yfinance", "web_research": "agent_reach", "intraday_data": "yfinance"}
    with pytest.raises(ValueError, match="web_research has no vendor yfinance"):
        parse_vendors(["web_research=yfinance"])


# --- the news chain while Agent-Reach is on -----------------------------------------

def _config(**overrides):
    cfg = copy.deepcopy(default_config.DEFAULT_CONFIG)
    for key, value in overrides.items():
        if key == "news_data":
            cfg["data_vendors"]["news_data"] = value
        else:
            cfg[key] = value
    return cfg


@pytest.mark.unit
def test_an_untouched_news_chain_reads_agent_reach_first_only_when_it_is_on():
    off = _config()
    assert default_config.with_agent_reach_news(off) is off
    on = default_config.with_agent_reach_news(_config(agent_reach_enabled=True))
    assert on["data_vendors"]["news_data"] == "agent_reach,yfinance"
    assert on["data_vendors"]["core_stock_apis"] == "yfinance"           # the rest kept


@pytest.mark.unit
@pytest.mark.parametrize("chosen", ["alpha_vantage", "yfinance,alpha_vantage", "agent_reach"])
def test_a_chosen_news_chain_is_kept(chosen):
    cfg = _config(agent_reach_enabled=True, news_data=chosen)
    assert default_config.with_agent_reach_news(cfg)["data_vendors"]["news_data"] == chosen


def _graph(tmp_path, monkeypatch, model, selected=("news",), **overrides):
    cfg = _config(results_dir=str(tmp_path / "results"), data_cache_dir=str(tmp_path / "cache"),
                  memory_log_path=str(tmp_path / "log.md"), **overrides)
    monkeypatch.setattr(trading_graph, "create_tier_client", lambda config, tier, **k: _Client(model))
    return trading_graph.TradingAgentsGraph(list(selected), config=cfg)


@pytest.mark.unit
def test_switched_on_in_a_config_a_graph_reads_agent_reach_first(tmp_path, monkeypatch):
    graph = _graph(tmp_path, monkeypatch, ScriptedModel(), agent_reach_enabled=True)
    assert graph.config["data_vendors"]["news_data"] == "agent_reach,yfinance"
    assert graph.run_settings()["data_vendors"]["news_data"] == "agent_reach,yfinance"
    chosen = _graph(tmp_path, monkeypatch, ScriptedModel(), agent_reach_enabled=True, news_data="alpha_vantage")
    assert chosen.config["data_vendors"]["news_data"] == "alpha_vantage"


@pytest.mark.unit
@pytest.mark.parametrize("enabled", [False, True])
def test_the_run_settings_say_whether_agent_reach_was_on(tmp_path, monkeypatch, enabled):
    """The news chain alone does not say it: a chosen chain is kept while the News
    Analyst still gets the web tools."""
    graph = _graph(tmp_path, monkeypatch, ScriptedModel(), agent_reach_enabled=enabled, news_data="alpha_vantage")
    settings = graph.run_settings()
    assert settings["agent_reach_enabled"] is enabled and settings["data_vendors"]["news_data"] == "alpha_vantage"


@pytest.mark.unit
def test_switched_on_by_the_environment_a_chosen_yahoo_chain_is_kept(tmp_path, monkeypatch):
    """With TRADINGAGENTS_AGENT_REACH set the defaults already read Agent-Reach first,
    so --vendor news_data=yfinance is a choice, and the graph keeps it."""
    from cli.run import _build_run_config

    monkeypatch.setenv("TRADINGAGENTS_AGENT_REACH", "1")
    defaults = default_config.build_default_config()
    monkeypatch.setattr(trading_graph, "DEFAULT_CONFIG", defaults)
    monkeypatch.setattr("cli.run.DEFAULT_CONFIG", defaults)
    selections = {"research_depth": 1, "quick_think_llm": "q", "deep_think_llm": "d", "backend_url": None,
                  "llm_provider": "openai"}

    assert _build_run_config(selections, None)["data_vendors"]["news_data"] == "agent_reach,yfinance"
    run_config = _build_run_config({**selections, "data_vendors": {"news_data": "yfinance"}}, None)
    assert run_config["data_vendors"]["news_data"] == "yfinance"
    monkeypatch.setattr(trading_graph, "create_tier_client", lambda config, tier, **k: _Client(ScriptedModel()))
    run_config.update(results_dir=str(tmp_path / "r"), data_cache_dir=str(tmp_path / "c"),
                      memory_log_path=str(tmp_path / "m.md"))
    graph = trading_graph.TradingAgentsGraph(["news"], config=run_config)
    assert graph.config["data_vendors"]["news_data"] == "yfinance"


@pytest.mark.unit
def test_switched_on_by_the_environment_and_off_in_a_config_agent_reach_is_never_started(tmp_path, monkeypatch):
    """docs/agent-reach.md: switched off, Agent-Reach is never started. The environment's
    switch already put it first in the default news chain, which a Python config copies."""
    monkeypatch.setenv("TRADINGAGENTS_AGENT_REACH", "1")
    defaults = default_config.build_default_config()
    assert defaults["data_vendors"]["news_data"] == "agent_reach,yfinance"
    monkeypatch.setattr(default_config, "DEFAULT_CONFIG", defaults)
    monkeypatch.setattr(trading_graph, "DEFAULT_CONFIG", defaults)
    started = []
    monkeypatch.setitem(router.VENDOR_METHODS["get_news"], "agent_reach",
                        lambda *a, **k: started.append("agent_reach") or "Exa news")
    monkeypatch.setitem(router.VENDOR_METHODS["get_news"], "yfinance",
                        lambda *a, **k: started.append("yfinance") or "Yahoo news")

    graph = _graph(tmp_path, monkeypatch, ScriptedModel(), agent_reach_enabled=False)
    with run_config(graph._run_scope("AAPL")):
        answer = router.route_to_vendor("get_news", "AAPL", "2026-01-02", "2026-01-09")

    assert graph.config["data_vendors"]["news_data"] == "yfinance"
    assert graph.run_settings()["data_vendors"]["news_data"] == "yfinance"
    assert (started, answer) == (["yfinance"], "Yahoo news")
    # A chain the config names is still its choice.
    chosen = _graph(tmp_path, monkeypatch, ScriptedModel(), agent_reach_enabled=False, news_data="agent_reach")
    assert chosen.config["data_vendors"]["news_data"] == "agent_reach"


@pytest.mark.unit
def test_from_python_the_tool_vendors_keep_yahoo_news_beside_the_web_tools(tmp_path, monkeypatch):
    """docs/agent-reach.md: switched on in a config, a news_data of yfinance cannot be told
    from the default it replaces; naming Yahoo for the two news tools keeps their news there."""
    started = []
    for method in ("get_news", "get_global_news"):
        monkeypatch.setitem(router.VENDOR_METHODS[method], "agent_reach",
                            lambda *a, _m=method, **k: started.append(("agent_reach", _m)) or "Exa news")
        monkeypatch.setitem(router.VENDOR_METHODS[method], "yfinance",
                            lambda *a, _m=method, **k: started.append(("yfinance", _m)) or "Yahoo news")

    named = _graph(tmp_path, monkeypatch, ScriptedModel(), agent_reach_enabled=True, news_data="yfinance")
    assert named.config["data_vendors"]["news_data"] == "agent_reach,yfinance"

    yahoo = {"get_news": "yfinance", "get_global_news": "yfinance"}
    graph = _graph(tmp_path, monkeypatch, ScriptedModel(), agent_reach_enabled=True, tool_vendors=yahoo)
    with run_config(graph._run_scope("AAPL")):
        answers = [router.route_to_vendor("get_news", "AAPL", "2026-01-02", "2026-01-09"),
                   router.route_to_vendor("get_global_news", "2026-01-09", None, None)]

    assert answers == ["Yahoo news", "Yahoo news"]
    assert started == [("yfinance", "get_news"), ("yfinance", "get_global_news")]
    assert graph.run_settings()["tool_vendors"] == yahoo and graph.run_settings()["agent_reach_enabled"]
    (spec,) = build_analyst_execution_plan(["news"], graph.config).specs
    assert spec.tools == news_analyst.TOOLS + news_analyst.WEB_RESEARCH_TOOLS


# --- the News Analyst's tools ---------------------------------------------------------

@pytest.mark.unit
def test_the_news_analyst_is_offered_the_web_tools_only_when_agent_reach_is_on():
    (off,) = build_analyst_execution_plan(["news"], _config()).specs
    (bare,) = build_analyst_execution_plan(["news"]).specs
    (on,) = build_analyst_execution_plan(["news"], _config(agent_reach_enabled=True)).specs
    assert off.tools == bare.tools == news_analyst.TOOLS
    assert on.tools == news_analyst.TOOLS + news_analyst.WEB_RESEARCH_TOOLS
    assert [t.name for t in news_analyst.WEB_RESEARCH_TOOLS] == [
        "read_web_page", "get_video_coverage", "get_video_transcript"]


@pytest.mark.unit
@pytest.mark.parametrize("enabled", [False, True])
def test_the_analyst_and_its_tool_node_get_the_same_tools(monkeypatch, enabled):
    offered, nodes = [], []
    real_analyst, real_node = graph_setup.create_news_analyst, graph_setup.ToolNode
    monkeypatch.setattr(graph_setup, "create_news_analyst",
                        lambda llm, tools: offered.append(tools) or real_analyst(llm, tools))
    monkeypatch.setattr(graph_setup, "ToolNode", lambda tools: nodes.append(tuple(tools)) or real_node(tools))
    setup = graph_setup.GraphSetup(ScriptedModel(), ScriptedModel(), ConditionalLogic(), 3,
                                   config=_config(agent_reach_enabled=enabled))
    setup.setup_graph(["news"])
    expected = news_analyst.TOOLS + (news_analyst.WEB_RESEARCH_TOOLS if enabled else ())
    assert offered == [expected] and nodes == [expected]


# --- a whole run with Agent-Reach on --------------------------------------------------

TEXT = "Report.\n\n**Rating**: Hold\n\nFINAL TRANSACTION PROPOSAL: **HOLD**"
ARGS = {"curr_date": TRADE_DATE, "start_date": "2026-10-21", "end_date": TRADE_DATE, "indicator": "cpi",
        "topic": "RBI rate cut", "url": "https://example.com/story", "query": "Reliance results",
        "video": "dQw4w9WgXcQ"}


class ScriptedModel(BaseChatModel):
    """Calls every bound tool once, then answers with TEXT."""

    tools: tuple = ()
    prompts: list = Field(default_factory=list)   # shared across bound copies

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self.model_copy(update={"tools": tuple(tools)})

    def with_structured_output(self, schema, **kwargs):
        raise NotImplementedError

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.prompts.append(str(messages[0].content))
        if self.tools and not isinstance(messages[-1], ToolMessage):
            calls = [{"name": t.name, "id": f"call_{i}",
                      "args": {k: v for k, v in ARGS.items()
                               if k in t.tool_call_schema.model_json_schema()["properties"]}}
                     for i, t in enumerate(self.tools)]
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=calls))])
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=TEXT))])


class _Client:
    def __init__(self, model):
        self.model = model

    def get_llm(self):
        return self.model


@pytest.fixture
def offline(monkeypatch):
    """Every vendor answers offline; returns the (method, vendor) pairs called."""
    called = []
    for method, vendors in router.VENDOR_METHODS.items():
        for vendor in vendors:
            monkeypatch.setitem(vendors, vendor,
                                lambda *a, _m=method, _v=vendor, **k: called.append((_m, _v)) or f"{_m} data")
    prices = pd.DataFrame({"Date": pd.bdate_range(end=TRADE_DATE, periods=60), "Open": 100.0, "High": 101.0,
                           "Low": 99.0, "Close": 100.5, "Volume": 1_000_000})
    monkeypatch.setattr(snapshot, "load_ohlcv", lambda *a, **k: prices.copy())
    monkeypatch.setattr(sentiment_analyst, "fetch_stocktwits_messages", lambda *a, **k: "no posts")
    monkeypatch.setattr(sentiment_analyst, "fetch_reddit_posts", lambda *a, **k: "no posts")
    monkeypatch.setattr(yahoo_market.yf, "Ticker", lambda s: type("T", (), {"info": {"longName": "Reliance"}})())
    context._identity.cache_clear()
    return called


@pytest.mark.unit
def test_a_run_with_agent_reach_on_reads_news_and_the_web_through_it(tmp_path, monkeypatch, offline):
    model = ScriptedModel()
    graph = _graph(tmp_path, monkeypatch, model, agent_reach_enabled=True)

    state, _ = graph.propagate("RELIANCE.NS", TRADE_DATE)

    assert state["news_report"] == TEXT
    assert ("get_news", "agent_reach") in offline and ("get_global_news", "agent_reach") in offline
    for method in ("read_web_page", "get_video_coverage", "get_video_transcript"):
        assert (method, "agent_reach") in offline
    assert not [pair for pair in offline if pair[0] in ("get_news", "get_global_news") and pair[1] != "agent_reach"]
    assert any(news_analyst.WEB_RESEARCH_BRIEF in p for p in model.prompts)


@pytest.mark.unit
def test_a_run_with_agent_reach_off_never_starts_it(tmp_path, monkeypatch, offline):
    model = ScriptedModel()
    _graph(tmp_path, monkeypatch, model).propagate("RELIANCE.NS", TRADE_DATE)

    assert not [pair for pair in offline if pair[1] == "agent_reach"]
    assert ("get_news", "yfinance") in offline
    assert not any(news_analyst.WEB_RESEARCH_BRIEF in p for p in model.prompts)
