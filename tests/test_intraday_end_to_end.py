"""An intraday run, end to end, with scripted models, stubbed Yahoo bars and no network.

The first run, dated Fri 23 Oct 2026 after the close, plans Monday's session: the
Day Trader joins the analysts, the Trader and Portfolio Manager are told the
session and the price the plan is read against, and the plan is logged tagged
intraday. The second, dated Monday once its close is final, settles that plan on
Monday's 5m bars inside its Memory Log step, never by the holding-period return,
and its Portfolio Manager reads the lesson. Monday's bars are served from the
start, so the first run also shows none of them reaches a plan made on Friday.
"""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda
from pydantic import Field

from tradingagents.agents import context, intraday_plan, schemas
from tradingagents.dataflows import router
from tradingagents.dataflows.vendors.yahoo import (
    intraday as yahoo_intraday,
    market as yahoo_market,
    snapshot,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph import trading_graph
from tradingagents.memory import intraday_settlement, settlement

IST = ZoneInfo("Asia/Kolkata")
TRADE_DATE = "2026-10-23"        # a Friday
SESSION = "2026-10-26"           # the Monday its plan is for
FRIDAY_CLOSE = 100.5

# The free-text answer: a report for the analysts, a decision with its plan's lines for the managers.
TEXT = ("Report.\n\n**Rating**: Buy\n\n**Direction**: long\n\n**Entry Price**: ₹101.00\n\n"
        "**Stop Loss**: 99.50\n\n**Target Price**: 104\n\n**Exit By**: 15:00\n\n"
        "**Position Size (shares)**: 100\n\n**Invalidation**: a close under 99.5\n\n"
        "FINAL TRANSACTION PROPOSAL: **BUY**")

STRUCTURED = {
    schemas.ResearchPlan: schemas.ResearchPlan(
        recommendation=schemas.PortfolioRating.BUY, rationale="r", strategic_actions="a"),
    schemas.IntradayTraderProposal: schemas.IntradayTraderProposal(
        action=schemas.TraderAction.BUY, reasoning="Breakout above Friday's high.", direction="long",
        entry_price=101.0, stop_loss=99.5, target_price=104.0, exit_by="15:05", position_size_shares=100),
    schemas.IntradayPortfolioDecision: schemas.IntradayPortfolioDecision(
        rating=schemas.PortfolioRating.BUY, executive_summary="Long the opening breakout.",
        investment_thesis="Momentum into the week.", direction="long", entry_price=101.0, stop_loss=99.5,
        target_price=104.0, exit_by="15:20", position_size_shares=100, invalidation="a close under 99.5"),
}

ARGS = {"curr_date": TRADE_DATE, "start_date": "2026-10-16", "end_date": TRADE_DATE, "indicator": "rsi",
        "topic": "RBI rate decision"}


def _text(prompt) -> str:
    if isinstance(prompt, str):
        return prompt
    return "\n".join(str(m.get("content", "")) if isinstance(m, dict) else str(getattr(m, "content", m))
                     for m in prompt)


class Scripted(BaseChatModel):
    """Calls every bound tool once, then answers TEXT; records each prompt with its graph step."""

    structured: bool = True
    tools: tuple = ()
    seen: list = Field(default_factory=list)      # shared across bound copies
    bound: list = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self.model_copy(update={"tools": tuple(tools)})

    def with_structured_output(self, schema, **kwargs):
        if not self.structured:
            raise NotImplementedError
        self.bound.append(schema)
        return RunnableLambda(lambda prompt: self.seen.append((schema.__name__, _text(prompt))) or STRUCTURED[schema])

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        step = (run_manager.metadata or {}).get("langgraph_node") if run_manager else None
        self.seen.append((step, _text(messages)))
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


def _session(day: str, price: float) -> pd.DataFrame:
    index = pd.date_range(f"{day} 09:15", f"{day} 15:30", freq="5min", inclusive="left", tz=IST, name="Datetime")
    return pd.DataFrame({"Open": price, "High": price + 0.2, "Low": price - 0.2, "Close": price,
                         "Adj Close": price, "Volume": 1000}, index=index)


def _bars() -> pd.DataFrame:
    """Wed-Fri around 100 (Friday closing at 100.5), then Monday's breakout through 101 at 09:20 and 104 at 09:30."""
    friday = _session(TRADE_DATE, 100.3)
    friday.iloc[-1, friday.columns.get_loc("Close")] = FRIDAY_CLOSE
    monday = _session(SESSION, 100.2)
    for column, values in (("Open", (100.2, 101.8)), ("High", (101.2, 104.5)), ("Low", (100.0, 101.5)),
                           ("Close", (101.0, 104.2))):
        monday.iloc[[1, 3], monday.columns.get_loc(column)] = values
    return pd.concat([_session("2026-10-21", 99.8), _session("2026-10-22", 100.0), friday, monday])


def _ist(day: str, clock: str):
    return pd.Timestamp(f"{day} {clock}", tz=IST).to_pydatetime()


@pytest.fixture
def offline(monkeypatch):
    """Vendors answer offline and Yahoo's intraday bars are synthetic; set ``now`` and ``decided`` per run."""
    state = SimpleNamespace(called=set(), now=None, decided=None, downloads=0)
    for method, vendors in router.VENDOR_METHODS.items():
        for vendor in vendors:
            monkeypatch.setitem(vendors, vendor,
                                lambda *a, _m=method, **k: state.called.add(_m) or f"{_m} data")
    prices = pd.DataFrame({"Date": pd.bdate_range(end=TRADE_DATE, periods=60), "Open": 100.0, "High": 101.0,
                           "Low": 99.0, "Close": 100.5, "Volume": 1_000_000})
    monkeypatch.setattr(snapshot, "load_ohlcv", lambda *a, **k: state.called.add("ohlcv") or prices.copy())

    class Ticker:      # one stand-in for the identity lookup and the intraday download
        info = {"longName": "Reliance Industries Limited"}

        def __init__(self, symbol):
            self.symbol = symbol

        def history(self, **kwargs):
            state.downloads += 1
            assert kwargs["interval"] == "5m" and kwargs["prepost"] is False
            return _bars()

    monkeypatch.setattr(yahoo_market.yf, "Ticker", Ticker)
    monkeypatch.setattr(yahoo_intraday, "_now", lambda: state.now)
    monkeypatch.setattr(intraday_settlement, "_now", lambda: state.now)
    monkeypatch.setattr(intraday_plan, "_now", lambda: state.decided)
    monkeypatch.setattr(settlement, "fetch_returns",
                        lambda *a, **k: pytest.fail("an intraday plan was scored by the holding-period return"))
    context._identity.cache_clear()
    return state


def _graph(tmp_path, monkeypatch, model):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg.update(results_dir=str(tmp_path / "results"), data_cache_dir=str(tmp_path / "cache"),
               memory_log_path=str(tmp_path / "log.md"), decision_horizon="intraday")
    monkeypatch.setattr(trading_graph, "create_tier_client", lambda config, tier, **k: _Client(model))
    return trading_graph.TradingAgentsGraph(selected_analysts=("market", "news"), config=cfg)


def _prompts(model, step):
    return [prompt for name, prompt in model.seen if name == step]


@pytest.mark.unit
def test_an_intraday_run_plans_the_next_session_and_the_next_run_settles_it(tmp_path, monkeypatch, offline):
    model = Scripted()
    graph = _graph(tmp_path, monkeypatch, model)
    assert graph.selected_analysts == ("market", "news", "day_trader")

    # --- Friday evening: the plan for Monday -----------------------------------------
    offline.now, offline.decided = _ist(TRADE_DATE, "18:00"), _ist(TRADE_DATE, "18:20")
    state, signal = graph.propagate("RELIANCE.NS", TRADE_DATE)

    assert signal == state["final_rating"] == "Buy"
    assert state["intraday_report"] == TEXT
    plan_context = state["intraday_context"]
    assert (plan_context["session"], plan_context["exit_by"], plan_context["reference_price"],
            plan_context["reference_through"]) == (SESSION, "15:10", FRIDAY_CLOSE, "2026-10-23T15:30:00+05:30")
    assert {"get_intraday_bars", "get_market_context", "ohlcv", "get_stock_data", "get_news"} <= offline.called
    assert schemas.IntradayTraderProposal in model.bound and schemas.IntradayPortfolioDecision in model.bound
    assert schemas.TraderProposal not in model.bound and schemas.PortfolioDecision not in model.bound

    day_trader_turns = [p for p in _prompts(model, "agent") if "get_intraday_snapshot" in p]
    assert day_trader_turns and "the plan is for Mon 26 Oct 2026" in day_trader_turns[0]
    for decider in ("IntradayTraderProposal", "IntradayPortfolioDecision"):
        (prompt,) = _prompts(model, decider)
        # Dated today on Friday's clock, after its close: the next trading day.
        assert "the plan is for Mon 26 Oct 2026, the next trading day; today's session closed at 15:30 IST" in prompt
        assert "The plan is read against 100.50, the close of the newest complete 5m bar" in prompt
        assert "Exit by 15:10 IST at the latest" in prompt
    for debater in ("Bull Researcher", "Aggressive Analyst"):
        assert "Report." in _prompts(model, debater)[0] and "day trader report" in _prompts(model, debater)[0].lower()

    plan = state["intraday_plan"]
    assert {k: plan[k] for k in ("direction", "entry_price", "stop_loss", "target_price", "exit_by",
                                 "position_size_shares", "session", "reference_price", "valid_from")} == {
        "direction": "long", "entry_price": 101.0, "stop_loss": 99.5, "target_price": 104.0, "exit_by": "15:10",
        "position_size_shares": 100, "session": SESSION, "reference_price": FRIDAY_CLOSE, "valid_from": None}
    assert state["final_trade_decision"].startswith("**Rating**: Buy")
    assert "**Plan Session**: Mon 26 Oct 2026" in state["final_trade_decision"]

    (entry,) = graph.memory_log.load_entries()
    assert entry["pending"] and entry["horizon"] == "intraday" and entry["intraday_plan"] == plan
    logged = json.loads(next((tmp_path / "results").rglob("full_states_log_2026-10-23.json")).read_text())
    assert logged["intraday_plan"] == plan and logged["run_settings"]["decision_horizon"] == "intraday"
    assert "horizon=intraday" in graph._run_signature("stock")
    folder = graph.save_reports(state, "RELIANCE.NS", tmp_path / "report").parent
    assert (folder / "1_analysts" / "day_trader.md").read_text() == TEXT
    assert "- Horizon: intraday paper plan for Mon 26 Oct 2026" in (folder / "complete_report.md").read_text()

    # --- Monday after the close: the plan settles on Monday's bars -------------------
    model.seen.clear()
    offline.now, offline.decided = _ist(SESSION, "16:30"), _ist(SESSION, "16:40")
    state, _ = graph.propagate("RELIANCE.NS", SESSION)

    (reflection_prompt,) = _prompts(model, "Memory Log")
    assert "one trading session" in reflection_prompt
    assert "filled at 101.00 at 09:20 (stop entry), closed at 104.00 at 09:30 (target)" in reflection_prompt
    settled = next(e for e in graph.memory_log.load_entries() if e["date"] == TRADE_DATE)
    assert not settled["pending"] and settled["resolved"] == SESSION and settled["holding"] == "intraday"
    outcome = settled["intraday_outcome"]
    assert (outcome["exit_reason"], outcome["shares"]) == ("target", 100)
    assert outcome["net_pnl"] == pytest.approx(289.073177)       # 300.00 gross less India's intraday charges
    assert settled["raw"] == "+2.9%" and settled["alpha"] == "n/a"
    (pm_prompt,) = _prompts(model, "IntradayPortfolioDecision")
    assert "INTRADAY OUTCOME: 2026-10-26: long filled at 101, closed at 104 (target)" in pm_prompt
    # Monday's run, after the close, plans Tuesday; its own plan waits.
    assert state["intraday_context"]["session"] == "2026-10-27"
    assert state["intraday_context"]["reference_price"] == pytest.approx(100.2)
    assert [e["pending"] for e in graph.memory_log.load_entries()] == [False, True]


@pytest.mark.unit
def test_a_free_text_intraday_run_reads_its_plan_from_the_decisions_lines(tmp_path, monkeypatch, offline):
    model = Scripted(structured=False)
    graph = _graph(tmp_path, monkeypatch, model)
    offline.now, offline.decided = _ist(TRADE_DATE, "18:00"), _ist(TRADE_DATE, "18:20")

    state, signal = graph.propagate("RELIANCE.NS", TRADE_DATE)

    assert signal == "Buy" and state["final_trade_decision"] == TEXT
    plan = state["intraday_plan"]
    assert (plan["direction"], plan["entry_price"], plan["stop_loss"], plan["target_price"], plan["exit_by"],
            plan["position_size_shares"], plan["invalidation"]) == ("long", 101.0, 99.5, 104.0, "15:00", 100,
                                                                    "a close under 99.5")
    (entry,) = graph.memory_log.load_entries()
    assert entry["horizon"] == "intraday" and entry["intraday_plan"]["session"] == SESSION


@pytest.mark.unit
def test_a_swing_graph_has_no_day_trader_and_no_intraday_state(tmp_path, monkeypatch, offline):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg.update(results_dir=str(tmp_path / "results"), data_cache_dir=str(tmp_path / "cache"),
               memory_log_path=str(tmp_path / "log.md"))
    monkeypatch.setattr(trading_graph, "create_tier_client", lambda config, tier, **k: _Client(Scripted()))
    graph = trading_graph.TradingAgentsGraph(selected_analysts=("market", "news"), config=cfg)
    offline.now = _ist(TRADE_DATE, "18:00")

    state, _ = graph.propagate("RELIANCE.NS", TRADE_DATE)

    assert graph.selected_analysts == ("market", "news")
    assert not {"intraday_report", "intraday_context", "intraday_plan", "decision_horizon"} & set(state)
    assert offline.downloads == 0 and "get_intraday_bars" not in offline.called
    assert "horizon" not in graph.memory_log.load_entries()[0]
