"""Intraday (day-trader) mode in the agents: the Day Trader analyst, the decision horizon, the plan
fields, the memory log's intraday entries and their settlement on the session's bars.

Models are scripted, Yahoo is stubbed and the clocks are pinned: no test reaches
the network or depends on today. A swing run is checked to read as before.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda
from pydantic import Field

import tradingagents.default_config as default_config_module
from tradingagents.agents import intraday_plan, schemas
from tradingagents.agents.analysts import day_trader_analyst
from tradingagents.agents.managers.portfolio_manager import create_portfolio_manager
from tradingagents.agents.rating import parse_rating
from tradingagents.agents.trader.trader import create_trader
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.vendors.yahoo import intraday as yahoo_intraday
from tradingagents.graph.analyst_execution import ANALYST_NODE_SPECS, analysts_for_horizon
from tradingagents.graph.propagation import Propagator
from tradingagents.graph.trading_graph import TradingAgentsGraph, decision_horizon
from tradingagents.markets.intraday_eval import IntradayOutcome
from tradingagents.memory import TradingMemoryLog, intraday_settlement, settlement
from tradingagents.memory.reflection import Reflector

IST = ZoneInfo("Asia/Kolkata")
SESSION = "2026-10-26"          # a Monday; the plan of a run dated Fri 23 Oct
CONTEXT = {
    "symbol": "RELIANCE.NS", "session": SESSION,
    "why": "the next trading day after the analysis date Fri 23 Oct 2026",
    "exit_by": "15:10", "timezone": "Asia/Kolkata", "tz_label": "IST",
    "reference_price": 100.5, "reference_through": "2026-10-23T15:30:00+05:30",
    "interval": "5m", "source": "Yahoo Finance", "note": None, "past_dated": True,
}
# A run dated Mon 26 Oct during that session: its plan is for the rest of it.
TODAY_CONTEXT = {**CONTEXT, "why": "the rest of today's session, open until 15:30 IST",
                 "reference_through": "2026-10-26T10:40:00+05:30", "past_dated": False}
PLAN = {
    "direction": "long", "entry_price": 101.0, "stop_loss": 99.5, "target_price": 104.0, "exit_by": "15:10",
    "position_size_shares": 100, "invalidation": "a close under 99.5", "session": SESSION,
    "reference_price": 100.5, "valid_from": None, "timezone": "Asia/Kolkata", "notes": [],
}


def _ist(day: str, clock: str) -> datetime:
    return pd.Timestamp(f"{day} {clock}", tz=IST).to_pydatetime()


class Capturing(BaseChatModel):
    """Records every prompt; answers ``reply`` and, bound to a schema, ``structured[schema]``."""

    reply: str = "Report."
    structured: dict = Field(default_factory=dict)
    seen: list = Field(default_factory=list)
    bound: list = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "capturing"

    def bind_tools(self, tools, **kwargs):
        return self

    def with_structured_output(self, schema, **kwargs):
        self.bound.append(schema)
        if schema not in self.structured:
            raise NotImplementedError
        return RunnableLambda(lambda prompt: self.seen.append(prompt) or self.structured[schema])

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.seen.append(messages)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.reply))])


def _text(prompt) -> str:
    if isinstance(prompt, str):
        return prompt
    return "\n".join(str(m.get("content", "")) if isinstance(m, dict) else str(getattr(m, "content", m))
                     for m in prompt)


def _risk():
    return {"history": "h", "aggressive_history": "a", "conservative_history": "c", "neutral_history": "n",
            "current_aggressive_response": "", "current_conservative_response": "",
            "current_neutral_response": "", "latest_speaker": "Neutral", "count": 1}


def _decision_state(**extra):
    return {"company_of_interest": "RELIANCE.NS", "trade_date": "2026-10-23", "investment_plan": "Buy on strength.",
            "trader_investment_plan": "Trader plan.", "market_report": "", "risk_debate_state": _risk(),
            "decision_horizon": "intraday", "intraday_context": dict(CONTEXT),
            "intraday_report": "Snapshot: last 100.5, R1 103.9, ATR 0.9; paper size 100 shares for a 1.5 stop.",
            **extra}


# --- the decision horizon -------------------------------------------------------------

@pytest.mark.unit
def test_the_horizon_defaults_to_swing_and_reads_the_environment(monkeypatch):
    for key in default_config_module._ENV_OVERRIDES:
        monkeypatch.delenv(key, raising=False)
    assert default_config_module.build_default_config()["decision_horizon"] == "swing"
    monkeypatch.setenv("TRADINGAGENTS_DECISION_HORIZON", " Intraday ")
    assert default_config_module.build_default_config()["decision_horizon"] == "intraday"


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["daily", "scalp", "1"])
def test_an_unknown_horizon_fails_at_startup_naming_the_variable(monkeypatch, bad):
    monkeypatch.setenv("TRADINGAGENTS_DECISION_HORIZON", bad)
    with pytest.raises(ValueError, match="TRADINGAGENTS_DECISION_HORIZON.*swing or intraday"):
        default_config_module.build_default_config()


@pytest.mark.unit
def test_a_graph_config_names_a_known_horizon():
    assert decision_horizon({}) == "swing"
    assert decision_horizon({"decision_horizon": "INTRADAY"}) == "intraday"
    with pytest.raises(ValueError, match="decision_horizon must be one of swing, intraday"):
        decision_horizon({"decision_horizon": "weekly"})


@pytest.mark.unit
def test_intraday_adds_the_day_trader_once_and_swing_changes_nothing():
    assert analysts_for_horizon(["market", "news"], "swing") == ("market", "news")
    assert analysts_for_horizon(["market", "news"], "intraday") == ("market", "news", "day_trader")
    assert analysts_for_horizon(["day_trader", "market"], "intraday") == ("day_trader", "market")


@pytest.mark.unit
def test_the_checkpoint_signature_names_the_horizon():
    graph = object.__new__(TradingAgentsGraph)
    graph.selected_analysts = ("market", "day_trader")
    graph.config = {"max_debate_rounds": 1, "max_risk_discuss_rounds": 1}
    swing = graph._run_signature("stock")
    graph.config = {**graph.config, "decision_horizon": "intraday"}
    intraday = graph._run_signature("stock")
    assert "horizon=swing" in swing and "horizon=intraday" in intraday and swing != intraday


@pytest.mark.unit
def test_run_settings_name_the_horizon():
    graph = object.__new__(TradingAgentsGraph)
    graph.selected_analysts, graph.config = ("market",), {}
    assert graph.run_settings()["decision_horizon"] == "swing"
    graph.config = {"decision_horizon": "intraday"}
    assert graph.run_settings()["decision_horizon"] == "intraday"


@pytest.mark.unit
def test_a_swing_state_has_no_intraday_keys_and_an_intraday_one_starts_with_its_session():
    swing = Propagator().create_initial_state("NVDA", "2026-10-23")
    assert not {"decision_horizon", "intraday_context", "intraday_plan", "intraday_report"} & set(swing)
    intraday = Propagator().create_initial_state("RELIANCE.NS", "2026-10-23", decision_horizon="intraday",
                                                 intraday_context=CONTEXT)
    assert (intraday["decision_horizon"], intraday["intraday_context"], intraday["intraday_report"],
            intraday["intraday_plan"]) == ("intraday", CONTEXT, "", None)


@pytest.mark.unit
def test_an_intraday_crypto_run_is_refused():
    graph = object.__new__(TradingAgentsGraph)
    graph.config = {"decision_horizon": "intraday"}
    with pytest.raises(ValueError, match="crypto"):
        graph.create_run_state("BTC-USD", "2026-10-23", asset_type="crypto")


@pytest.mark.unit
def test_the_backtest_refuses_an_intraday_horizon(tmp_path):
    from tradingagents.backtest import run_backtest

    with pytest.raises(ValueError, match="swing"):
        run_backtest(["RELIANCE.NS"], ["2026-10-23"], {"results_dir": str(tmp_path), "decision_horizon": "intraday"})


# --- the Day Trader analyst -----------------------------------------------------------

@pytest.mark.unit
def test_the_day_trader_is_registered_with_its_tools_and_report():
    spec = ANALYST_NODE_SPECS["day_trader"]
    assert (spec.agent_node, spec.report_key) == ("Day Trader", "intraday_report")
    assert [t.name for t in spec.tools] == ["get_intraday_snapshot", "get_verified_market_snapshot",
                                            "get_market_context"]
    assert spec.tools == day_trader_analyst.TOOLS


@pytest.mark.unit
def test_the_day_trader_is_asked_for_a_paper_plan_from_tool_numbers_only():
    llm = Capturing(reply="Plan: long above 101.")
    node = day_trader_analyst.create_day_trader_analyst(llm)

    out = node({"trade_date": "2026-10-23", "company_of_interest": "RELIANCE.NS", "messages": [],
                "intraday_context": CONTEXT})

    assert out == {"messages": [out["messages"][0]], "intraday_report": "Plan: long above 101."}
    prompt = _text(llm.seen[0])
    for asked in ("get_intraday_snapshot", "Bias: long, short or no trade", "Entry trigger", "Stop:",
                  "R multiple", "paper sizing", "Exit by", "Invalidation and no-trade conditions",
                  "results or other scheduled event day", "very low relative volume", "price band",
                  "squared off the same day", "short selling is allowed intraday only", "charges",
                  "Use only numbers the tools returned", "paper trading only", "tool-call date ranges",
                  "Markdown table"):
        assert asked in prompt, asked
    assert "the plan is for Mon 26 Oct 2026, the next trading day after the analysis date" in prompt
    assert "Exit by 15:10 IST at the latest" in prompt


@pytest.mark.unit
def test_the_day_trader_writes_in_the_configured_language():
    from tradingagents.dataflows.config import set_config

    set_config({"output_language": "French"})
    llm = Capturing()
    day_trader_analyst.create_day_trader_analyst(llm)({"trade_date": "2026-10-23",
                                                       "company_of_interest": "RELIANCE.NS", "messages": []})
    assert "Write your entire response in French" in _text(llm.seen[0])


# --- the plan's fields ----------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("written, read", [
    ("Long", "long"), ("SHORT", "short"), ("no-trade", "none"), ("No trade", "none"), ("Buy", "long"),
    ("Sell", "short"), ("maybe", None), (None, None),
])
def test_a_direction_is_read_from_the_words_a_model_writes(written, read):
    assert schemas.intraday_direction(written) == read


@pytest.mark.unit
@pytest.mark.parametrize("written, read", [
    ("15:10", "15:10"), ("9:20", "09:20"), ("15:10 IST", "15:10"), ("3:10 pm", "15:10"), ("12:05 AM", "00:05"),
    ("15:10:00", "15:10"), (time(14, 0), "14:00"), ("25:00", None), ("15 am", None), ("after lunch", None),
])
def test_an_exit_by_time_is_read_as_hh_mm(written, read):
    assert schemas.clock_text(written) == read


@pytest.mark.unit
@pytest.mark.parametrize("written, read", [
    (100, 100), (35.0, 35), ("1,200 shares", 1200), ("35", 35), ("35.5", None), (-3, None), ("10-20", None),
    (True, None),
])
def test_a_share_count_is_a_whole_number(written, read):
    assert schemas.whole_shares(written) == read


@pytest.mark.unit
def test_the_intraday_schemas_are_flat_and_the_swing_ones_unchanged():
    for model in (schemas.IntradayTraderProposal, schemas.IntradayPortfolioDecision):
        properties = model.model_json_schema()["properties"]
        for field in ("direction", "target_price", "exit_by", "position_size_shares", "invalidation",
                      "entry_price", "stop_loss"):
            assert field in properties
            assert "$ref" not in json.dumps(properties[field]) and properties[field].get("type") != "object"
    swing = set(schemas.TraderProposal.model_json_schema()["properties"]) | set(
        schemas.PortfolioDecision.model_json_schema()["properties"])
    assert not {"direction", "target_price", "exit_by", "position_size_shares", "invalidation"} & swing


@pytest.mark.unit
def test_rupee_prices_are_read_in_a_plan_but_a_percentage_is_not_a_price():
    plan = schemas.IntradayPortfolioDecision(
        rating="Buy", executive_summary="s", investment_thesis="t", entry_price="₹1,405.50",
        stop_loss="Rs 1392", target_price="2%", direction="long")
    assert (plan.entry_price, plan.stop_loss, plan.target_price) == (1405.5, 1392.0, None)


@pytest.mark.unit
def test_the_intraday_renders_name_every_plan_field_and_keep_the_lines_readers_parse():
    proposal = schemas.IntradayTraderProposal(action="Sell", reasoning="Breakdown.", direction="short",
                                              entry_price=99.0, stop_loss=100.2, target_price=96.6)
    text = schemas.render_intraday_trader_proposal(proposal)
    assert text.splitlines()[-1] == "FINAL TRANSACTION PROPOSAL: **SELL**"
    for line in ("**Direction**: short", "**Entry Price**: 99.0", "**Stop Loss**: 100.2", "**Target Price**: 96.6",
                 "**Exit By**: not provided", "**Position Size (shares)**: not provided",
                 "**Invalidation**: not provided"):
        assert line in text

    decision = schemas.IntradayPortfolioDecision(rating="Overweight", executive_summary="s",
                                                 investment_thesis="t", price_target=104.0, direction="long")
    rendered = schemas.render_intraday_pm_decision(decision, "Mon 26 Oct 2026, the next trading day")
    assert rendered.startswith("**Rating**: Overweight")
    assert parse_rating(rendered) == "Overweight"
    assert "**Plan Session**: Mon 26 Oct 2026, the next trading day" in rendered
    assert "**Target Price**: 104.0" in rendered          # the price target stands in for the plan's
    assert "**Price Target**" not in rendered


# --- the Trader and Portfolio Manager -------------------------------------------------

@pytest.mark.unit
def test_a_swing_trader_and_portfolio_manager_bind_the_swing_schemas():
    llm = Capturing()
    create_trader(llm)
    create_portfolio_manager(llm)
    assert llm.bound == [schemas.TraderProposal, schemas.PortfolioDecision]


@pytest.mark.unit
def test_the_intraday_trader_plans_the_session_from_the_day_traders_snapshot():
    proposal = schemas.IntradayTraderProposal(action="Buy", reasoning="r", direction="long", entry_price=101.0,
                                              stop_loss=99.5, target_price=104.0, exit_by="15:05",
                                              position_size_shares=100)
    llm = Capturing(structured={schemas.IntradayTraderProposal: proposal})

    out = create_trader(llm, horizon="intraday")(_decision_state())

    assert llm.bound == [schemas.IntradayTraderProposal]
    prompt = _text(llm.seen[0])
    assert "the plan is for Mon 26 Oct 2026" in prompt and "Exit by 15:10 IST at the latest" in prompt
    assert "The plan is read against 100.50, the close of the newest complete 5m bar (through 15:30 IST on " \
           "Fri 23 Oct 2026)" in prompt
    assert "buy stop (breakout)" in prompt and "buy limit (pullback)" in prompt
    assert "Day Trader Report:\nSnapshot: last 100.5" in prompt
    assert "Use only the evidence provided in this prompt" in prompt
    assert "**Exit By**: 15:05" in out["trader_investment_plan"]
    assert out["trader_investment_plan"].endswith("FINAL TRANSACTION PROPOSAL: **BUY**")


@pytest.mark.unit
def test_the_intraday_portfolio_manager_returns_its_plan_as_data(monkeypatch):
    monkeypatch.setattr(intraday_plan, "_now", lambda: _ist("2026-10-23", "18:30"))
    decision = schemas.IntradayPortfolioDecision(
        rating="Buy", executive_summary="Long the breakout.", investment_thesis="t", direction="long",
        entry_price=101.0, stop_loss=99.5, target_price=104.0, exit_by="15:20", position_size_shares=100,
        invalidation="a close under 99.5")
    llm = Capturing(structured={schemas.IntradayPortfolioDecision: decision})

    out = create_portfolio_manager(llm, horizon="intraday")(_decision_state(past_context="[lesson]"))

    assert llm.bound == [schemas.IntradayPortfolioDecision]
    prompt = _text(llm.seen[0])
    for asked in ("**Direction**: long, short or none", "**Exit By**: HH:MM in the exchange's time zone, no later "
                  "than 15:10", "**Position Size (shares)**", "**Invalidation**", "Buy or Overweight is a long",
                  "Hold is no trade", "paper plan for research, not an order", "Snapshot: last 100.5",
                  "Lessons from prior decisions", "[lesson]", "Use only the evidence provided in this prompt"):
        assert asked in prompt, asked
    assert out["final_rating"] == "Buy"
    assert out["final_trade_decision"].startswith("**Rating**: Buy")
    assert "**Plan Session**: Mon 26 Oct 2026, the next trading day after the analysis date" in out[
        "final_trade_decision"]
    plan = out["intraday_plan"]
    assert {k: plan[k] for k in ("direction", "entry_price", "stop_loss", "target_price", "position_size_shares",
                                 "invalidation", "session", "reference_price")} == {
        "direction": "long", "entry_price": 101.0, "stop_loss": 99.5, "target_price": 104.0,
        "position_size_shares": 100, "invalidation": "a close under 99.5", "session": SESSION,
        "reference_price": 100.5}
    # The broker squares off at 15:10, so a later exit-by is scored to it, and says so.
    assert plan["exit_by"] == "15:10" and "after the 15:10 square-off" in plan["notes"][0]
    assert plan["valid_from"] is None          # decided before its session: the whole session counts
    # The decision the reports show is the plan that is scored, with what was changed.
    decided = out["final_trade_decision"]
    assert "**Exit By**: 15:10" in decided and "**Exit By**: 15:20" not in decided
    assert ("**Plan Checks**: the plan's exit-by 15:20 is after the 15:10 square-off, so it is scored to "
            "15:10.\n\n**Time Horizon**") in decided


@pytest.mark.unit
def test_the_saved_decision_shows_every_check_on_the_plan(monkeypatch, tmp_path):
    from tradingagents.reporting import write_report_tree

    monkeypatch.setattr(intraday_plan, "_now", lambda: _ist("2026-10-23", "18:30"))
    decision = schemas.IntradayPortfolioDecision(
        rating="Buy", executive_summary="Long the breakout.", investment_thesis="t", direction="long",
        entry_price=101.0, stop_loss=99.5, target_price=104.0, exit_by="15:20", position_size_shares=5000)
    out = create_portfolio_manager(Capturing(structured={schemas.IntradayPortfolioDecision: decision}),
                                   horizon="intraday")(_decision_state())
    state = {"trade_date": "2026-10-23", "decision_horizon": "intraday", "intraday_context": CONTEXT,
             "final_trade_decision": out["final_trade_decision"], "final_rating": "Buy"}
    write_report_tree(state, "RELIANCE.NS", tmp_path, settings={"decision_horizon": "intraday"}, html=False)
    saved = (tmp_path / "5_portfolio" / "decision.md").read_text()
    assert "**Exit By**: 15:10" in saved and "**Position Size (shares)**: 5000" in saved
    assert "the plan's 5,000 shares are more than the 311 that risking 0.5%" in saved   # noted, not corrected

    # A stop above a long's entry is said at decision time, not first found by the replay.
    wrong_side = decision.model_copy(update={"stop_loss": 102.0, "position_size_shares": 100})
    out = create_portfolio_manager(Capturing(structured={schemas.IntradayPortfolioDecision: wrong_side}),
                                   horizon="intraday")(_decision_state())
    note = "the stop is not below the entry for a long, so it is scored as no trade"
    assert out["intraday_plan"]["notes"][-1] == note and note in out["final_trade_decision"]


@pytest.mark.unit
def test_a_plan_made_during_its_own_session_counts_from_when_it_was_made(monkeypatch):
    decided = _ist(SESSION, "10:42")
    monkeypatch.setattr(intraday_plan, "_now", lambda: decided)
    plan = intraday_plan.plan_from_fields({"direction": "short"}, TODAY_CONTEXT)
    assert datetime.fromisoformat(plan["valid_from"]) == decided
    assert plan["exit_by"] == "15:10"                  # none given: the square-off
    # Resumed the next day, it was made after its session: no bar of it counts.
    resumed = _ist("2026-10-27", "09:30")
    late = intraday_plan.plan_from_fields({"direction": "short"}, TODAY_CONTEXT, decided_at=resumed)
    assert datetime.fromisoformat(late["valid_from"]) == resumed


@pytest.mark.unit
@pytest.mark.parametrize("clock", ["11:00", "16:40"])
def test_a_past_dated_plan_counts_the_whole_session_whenever_it_is_decided(clock):
    # A run dated Fri 23 Oct plans Monday as of Friday's close, on Monday as on any later day.
    for day in (SESSION, "2026-10-27"):
        plan = intraday_plan.plan_from_fields({"direction": "long"}, CONTEXT, decided_at=_ist(day, clock))
        assert plan["valid_from"] is None


@pytest.mark.unit
def test_a_plan_out_of_line_with_its_rating_or_its_capital_is_noted_not_corrected(monkeypatch):
    monkeypatch.setattr(intraday_plan, "_now", lambda: _ist("2026-10-23", "18:30"))
    sizing = {"capital": 100_000, "risk_pct": 0.5, "leverage_cap": 1.0}
    fields = {"direction": "long", "entry_price": 101.0, "stop_loss": 99.5, "position_size_shares": 5000}

    plan = intraday_plan.plan_from_fields(fields, CONTEXT, rating="Sell", sizing=sizing)

    assert (plan["direction"], plan["position_size_shares"]) == ("long", 5000)
    assert "the direction long does not follow the Sell rating" in plan["notes"][0]
    # 500 at risk over a 1.50 stop plus about 0.10 a share of intraday charges: 311 shares.
    assert plan["notes"][1] == ("the plan's 5,000 shares are more than the 311 that risking 0.5% of the "
                                "100,000 paper capital with this stop allows (leverage cap 1x)")
    fine = intraday_plan.plan_from_fields({**fields, "position_size_shares": 100}, CONTEXT, rating="Buy",
                                          sizing=sizing)
    assert fine["notes"] == []
    assert intraday_plan.plan_from_fields({"direction": "none"}, CONTEXT, rating="Sell")["notes"] == []


@pytest.mark.unit
def test_the_free_text_fallback_reads_the_plan_from_its_labelled_lines(monkeypatch):
    monkeypatch.setattr(intraday_plan, "_now", lambda: _ist("2026-10-23", "18:30"))
    text = ("**Rating**: Sell\n\n**Executive Summary**: Short the breakdown.\n\n- **Direction**: Short\n"
            "**Entry Price**: ₹99.00\n**Stop Loss**: 100.2\n**Target Price**: not provided\n"
            "Exit By: 3:05 pm IST\n**Position Size (shares)**: 120 shares\n**Invalidation**: back above VWAP")
    llm = Capturing(reply=text)          # no structured output: the free-text path

    out = create_portfolio_manager(llm, horizon="intraday")(_decision_state())

    assert out["final_rating"] == "Sell" and out["final_trade_decision"] == text
    plan = out["intraday_plan"]
    assert (plan["direction"], plan["entry_price"], plan["stop_loss"], plan["target_price"], plan["exit_by"],
            plan["position_size_shares"], plan["invalidation"]) == ("short", 99.0, 100.2, None, "15:05", 120,
                                                                    "back above VWAP")
    # The model's own text is kept; what the stored plan changed is added after it.
    late = Capturing(reply=text.replace("3:05 pm", "3:25 pm"))
    out = create_portfolio_manager(late, horizon="intraday")(_decision_state())
    assert out["final_trade_decision"] == (late.reply + "\n\n**Plan Checks**: the plan's exit-by 15:25 is after the "
                                           "15:10 square-off, so it is scored to 15:10.")
    assert out["final_rating"] == "Sell" and out["intraday_plan"]["exit_by"] == "15:10"


@pytest.mark.unit
def test_a_free_text_value_is_read_past_its_remark_and_one_that_cannot_be_read_is_noted(monkeypatch):
    monkeypatch.setattr(intraday_plan, "_now", lambda: _ist("2026-10-23", "18:30"))
    text = ("**Rating**: Sell\n\n**Direction**: **Short** (intraday only)\n**Entry Price**: 99.00 (sell stop under "
            "VWAP)\n**Stop Loss**: 100.20\n**Target Price**: 96.60 (2R)\n**Exit By**: 15:05 IST\n"
            "**Position Size (shares)**: 120 shares (11,880 exposure)\n**Invalidation**: back above VWAP (15m close)")
    out = create_portfolio_manager(Capturing(reply=text), horizon="intraday")(_decision_state())
    plan = out["intraday_plan"]
    assert (plan["direction"], plan["entry_price"], plan["target_price"], plan["position_size_shares"],
            plan["invalidation"]) == ("short", 99.0, 96.6, 120, "back above VWAP (15m close)")
    assert plan["notes"] == []

    # A line given but not readable is left out with a note, not silently.
    garbled = text.replace("96.60 (2R)", "about 96.60").replace("**Short** (intraday only)", "courte")
    out = create_portfolio_manager(Capturing(reply=garbled), horizon="intraday")(_decision_state())
    assert out["intraday_plan"]["target_price"] is None and out["intraday_plan"]["notes"] == [
        "the Target Price line could not be read, so it is left out",
        "the Direction line could not be read, so it is scored as no trade"]
    assert out["final_trade_decision"].endswith("**Plan Checks**: the Target Price line could not be read, so it "
                                                "is left out; the Direction line could not be read, so it is "
                                                "scored as no trade.")


@pytest.mark.unit
def test_a_run_in_another_language_keeps_the_plan_labels_in_english():
    from tradingagents.dataflows.config import set_config

    set_config({"output_language": "Hindi"})
    llm = Capturing(reply="**Rating**: Hold")
    create_portfolio_manager(llm, horizon="intraday")(_decision_state())
    prompt = _text(llm.seen[0])
    assert "Write your entire response in Hindi" in prompt
    assert "Keep the labels of the plan's lines (**Direction**, **Entry Price**" in prompt
    # The values are read back too: in English words, plain numbers and a 24-hour time.
    assert "the direction as long, short or none" in prompt and "HH:MM on a 24-hour clock" in prompt


@pytest.mark.unit
def test_an_exit_by_that_leaves_the_plan_no_bar_is_replaced_with_a_note():
    # "3:05" written for 3:05 pm reads as 03:05, before the session opens: no bar would count.
    long, short = ({"direction": "long", "entry_price": 101.0, "stop_loss": 99.5},
                   {"direction": "short", "entry_price": 100.0, "stop_loss": 101.0})
    nse = {**CONTEXT, "opens": "09:15"}
    plan = intraday_plan.plan_from_fields({**long, "exit_by": "03:05"}, nse, decided_at=_ist("2026-10-23", "18:30"))
    assert plan["exit_by"] == "15:10" and plan["notes"] == [
        "the plan's exit-by 03:05 is not after the session's 09:15 open, so it is scored to 15:10"]
    us = {**CONTEXT, "symbol": "AAPL", "exit_by": None, "timezone": "America/New_York", "opens": "09:30"}
    plan = intraday_plan.plan_from_fields({**long, "exit_by": "03:55"}, us, decided_at=_ist("2026-10-24", "03:30"))
    assert plan["exit_by"] is None and plan["notes"] == [
        "the plan's exit-by 03:55 is not after the session's 09:30 open, so it is scored to the session close"]
    # Made at 10:42 during its own session, to exit by 10:30: also no bar.
    plan = intraday_plan.plan_from_fields({**short, "exit_by": "10:30"}, {**TODAY_CONTEXT, "opens": "09:15"},
                                          decided_at=_ist(SESSION, "10:42"))
    assert plan["exit_by"] == "15:10" and plan["notes"] == [
        "the plan's exit-by 10:30 is not after 10:42, when the plan was made, so it is scored to 15:10"]
    later = intraday_plan.plan_from_fields({**short, "exit_by": "14:45"}, nse, decided_at=_ist(SESSION, "10:42"))
    assert later["exit_by"] == "14:45" and later["notes"] == []


@pytest.mark.unit
def test_an_intraday_run_without_a_session_says_so_rather_than_guessing():
    llm = Capturing(reply="**Rating**: Hold")
    state = _decision_state(intraday_context={"note": "the plan's session could not be worked out (KeyError)"})
    out = create_portfolio_manager(llm, horizon="intraday")(state)
    assert "the session the plan is for could not be worked out" in _text(llm.seen[0])
    assert out["intraday_plan"]["session"] is None


# --- the other agents read the Day Trader's report ------------------------------------

def _debate_state(**extra):
    return {"company_of_interest": "NVDA", "trade_date": "2026-10-23", "asset_type": "stock",
            "market_report": "M", "sentiment_report": "", "news_report": "", "fundamentals_report": "",
            "investment_plan": "P", "trader_investment_plan": "T",
            "investment_debate_state": {"bull_history": "", "bear_history": "", "history": "",
                                        "current_response": "", "count": 0},
            "risk_debate_state": _risk(), **extra}


_DEBATERS = [
    ("tradingagents.agents.researchers.bull_researcher", "create_bull_researcher"),
    ("tradingagents.agents.researchers.bear_researcher", "create_bear_researcher"),
    ("tradingagents.agents.risk_mgmt.aggressive_debator", "create_aggressive_debator"),
    ("tradingagents.agents.risk_mgmt.conservative_debator", "create_conservative_debator"),
    ("tradingagents.agents.risk_mgmt.neutral_debator", "create_neutral_debator"),
]


def _debate_prompt(module, factory, state) -> str:
    import importlib

    llm = Capturing(reply="argument")
    getattr(importlib.import_module(module), factory)(llm)(state)
    return _text(llm.seen[0])


@pytest.mark.unit
@pytest.mark.parametrize("module, factory", _DEBATERS)
def test_the_debaters_read_the_day_traders_report_when_the_run_has_one(module, factory):
    assert "day trader" not in _debate_prompt(module, factory, _debate_state()).lower()
    with_report = _debate_prompt(module, factory, _debate_state(intraday_report="VWAP 100.2, R1 103.9"))
    assert "VWAP 100.2, R1 103.9" in with_report
    missing = _debate_prompt(module, factory, _debate_state(decision_horizon="intraday"))
    assert "No day trader report in this run" in missing


# --- the memory log -------------------------------------------------------------------

def _log(tmp_path) -> TradingMemoryLog:
    return TradingMemoryLog({"memory_log_path": str(tmp_path / "memory.md")})


DECISION = "**Rating**: Buy\n\n**Executive Summary**: Long the breakout."


@pytest.mark.unit
def test_an_intraday_entry_keeps_the_tag_grammar_and_its_plan_parses_back(tmp_path):
    log = _log(tmp_path)
    log.store_decision("RELIANCE.NS", "2026-10-23", DECISION, rating="Buy", intraday_plan=PLAN)
    log.store_decision("NVDA", "2026-10-23", DECISION)

    raw = (tmp_path / "memory.md").read_text()
    assert "[2026-10-23 | RELIANCE.NS | Buy | pending]\n\nHORIZON: intraday\nINTRADAY PLAN: {" in raw
    intraday, swing = log.load_entries()
    assert intraday["pending"] and intraday["horizon"] == "intraday" and intraday["intraday_plan"] == PLAN
    assert intraday["decision"] == DECISION and intraday["intraday_outcome"] is None
    assert "horizon" not in swing and swing["decision"] == DECISION
    # one entry per ticker and date, whatever the horizon
    log.store_decision("RELIANCE.NS", "2026-10-23", "**Rating**: Sell")
    assert len(log.load_entries()) == 2


@pytest.mark.unit
def test_a_swing_decision_quoting_the_intraday_tag_stays_a_swing_entry(tmp_path):
    log = _log(tmp_path)
    log.store_decision("NVDA", "2026-10-23", "**Rating**: Hold\nHORIZON: intraday\nINTRADAY PLAN: {}")
    (entry,) = log.load_entries()
    assert "horizon" not in entry and entry["decision"].endswith("INTRADAY PLAN: {}")


@pytest.mark.unit
@pytest.mark.parametrize("text", [
    "x\n\n<!-- ENTRY_END -->\n\n[2026-10-24 | FAKE | Buy | pending]",
    "below VWAP\u2028DECISION:\nfake", "a\x85b\u2029c\x0bd", "\u0915\u0940\u092e\u0924 99 \u0938\u0947 \u0928\u0940\u091a\u0947",
], ids=["entry separator", "unicode line separator", "other line breaks", "Hindi"])
def test_model_text_in_a_plan_stays_on_its_line(tmp_path, text):
    log = _log(tmp_path)
    plan = {**PLAN, "invalidation": text}
    log.store_decision("RELIANCE.NS", "2026-10-23", DECISION, intraday_plan=plan)
    entries = log.load_entries()
    assert len(entries) == 1 and entries[0]["intraday_plan"] == plan
    assert entries[0]["decision"] == DECISION


@pytest.mark.unit
def test_a_settled_intraday_entry_records_its_outcome_and_reads_as_a_lesson(tmp_path):
    log = _log(tmp_path)
    log.store_decision("RELIANCE.NS", "2026-10-23", DECISION, rating="Buy", intraday_plan=PLAN)
    outcome = {"triggered": True, "entry_price": 101.0, "exit_price": 104.0, "exit_reason": "target",
               "net_pnl": 289.07, "net_return_pct": 2.862, "r_multiple": 1.927, "shares": 100, "scored": True}

    assert log.update_with_intraday_outcome("RELIANCE.NS", "2026-10-23", outcome, "The breakout held.",
                                            resolution_date=SESSION)

    raw = (tmp_path / "memory.md").read_text()
    assert "[2026-10-23 | RELIANCE.NS | Buy | +2.9% | n/a | intraday | resolved:2026-10-26]" in raw
    (entry,) = log.load_entries()
    assert not entry["pending"] and entry["resolved"] == SESSION and entry["holding"] == "intraday"
    assert entry["intraday_outcome"] == outcome and entry["intraday_plan"] == PLAN
    assert (entry["decision"], entry["reflection"]) == (DECISION, "The breakout held.")
    lesson = log.get_past_context("RELIANCE.NS", as_of=SESSION)
    assert ("INTRADAY OUTCOME: 2026-10-26: long filled at 101, closed at 104 (target); net P&L 289.07 "
            "(+2.86%), +1.93R") in lesson
    assert "| intraday]" in log.get_past_context("TCS.NS")       # a cross-ticker lesson says what it was
    assert not log.update_with_intraday_outcome("RELIANCE.NS", "2026-10-23", outcome, "again")


@pytest.mark.unit
def test_record_decision_tags_an_intraday_run_and_leaves_a_swing_run_as_it_was(tmp_path):
    graph = object.__new__(TradingAgentsGraph)
    graph.config = {"memory_log_path": str(tmp_path / "memory.md")}
    graph.memory_log = TradingMemoryLog(graph.config)
    graph._log_state = lambda *a: None
    graph.record_decision("NVDA", "2026-10-23", {"final_trade_decision": "**Rating**: Hold"})
    graph.record_decision("RELIANCE.NS", "2026-10-23", {"final_trade_decision": DECISION, "final_rating": "Buy",
                                                        "decision_horizon": "intraday", "intraday_plan": PLAN})
    swing, intraday = graph.memory_log.load_entries()
    assert "horizon" not in swing
    assert intraday["horizon"] == "intraday" and intraday["intraday_plan"] == PLAN


@pytest.mark.unit
def test_the_state_log_keeps_the_intraday_keys_of_an_intraday_run_only(tmp_path):
    graph = object.__new__(TradingAgentsGraph)
    graph.config = {"results_dir": str(tmp_path)}
    graph.selected_analysts = ("market",)
    base = {"company_of_interest": "RELIANCE.NS", "trade_date": "2026-10-23", "market_report": "M",
            "sentiment_report": "", "news_report": "", "fundamentals_report": "", "investment_plan": "",
            "trader_investment_plan": "", "final_trade_decision": DECISION,
            "investment_debate_state": {"bull_history": "", "bear_history": "", "history": "", "current_response": ""},
            "risk_debate_state": {"aggressive_history": "", "conservative_history": "", "neutral_history": "",
                                  "history": ""}}
    path = tmp_path / "RELIANCE.NS" / "TradingAgentsStrategy_logs" / "full_states_log_2026-10-23.json"
    graph._log_state("2026-10-23", base)
    assert not {"intraday_report", "intraday_plan", "decision_horizon"} & set(json.loads(path.read_text()))
    graph._log_state("2026-10-23", {**base, "intraday_report": "levels", "decision_horizon": "intraday",
                                    "intraday_context": CONTEXT, "intraday_plan": PLAN})
    logged = json.loads(path.read_text())
    assert (logged["intraday_report"], logged["intraday_plan"], logged["intraday_context"]["session"]) == (
        "levels", PLAN, SESSION)


# --- settling an intraday plan --------------------------------------------------------

def _session_bars(day: str = SESSION, extra_day: str | None = "2026-10-23") -> pd.DataFrame:
    """One NSE session of 5m bars flat at 100, with a breakout through 101 at 09:20 and 104 at 09:30."""
    rows = []
    for stamp in pd.date_range(f"{day} 09:15", f"{day} 15:25", freq="5min", tz=IST):
        rows.append({"Datetime": stamp, "Open": 100.2, "High": 100.4, "Low": 100.0, "Close": 100.2, "Volume": 1000})
    rows[1].update(Open=100.2, High=101.2, Low=100.0, Close=101.0)
    rows[3].update(Open=101.8, High=104.5, Low=101.5, Close=104.2)
    frame = pd.DataFrame(rows)
    if extra_day:     # the loader serves the previous session when the plan's has no bars
        frame = pd.concat([_session_bars(extra_day, None).assign(Close=100.5), frame], ignore_index=True)
    return frame


class _Reflector:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def reflect_on_intraday_plan(self, decision, summary):
        self.calls.append((decision, summary))
        if self.fail:
            raise RuntimeError("provider timed out")
        return "The breakout held to the target."

    def reflect_on_final_decision(self, **kw):
        raise AssertionError("an intraday plan is not scored by the holding-period return")


@pytest.fixture
def pending(tmp_path, monkeypatch):
    """A log with a pending intraday entry for RELIANCE.NS, and a stub loader; ``loads`` records each fetch."""
    log = _log(tmp_path)
    log.store_decision("RELIANCE.NS", "2026-10-23", DECISION, rating="Buy", intraday_plan=PLAN)
    state = SimpleNamespace(log=log, loads=[], bars=_session_bars(), live=False, error=None)

    def load(symbol, as_of_date, interval="5m", sessions=5, now=None):
        state.loads.append((symbol, as_of_date, interval, sessions, now))
        if state.error is not None:
            raise state.error
        bars = state.bars[state.bars["Datetime"].dt.date <= date.fromisoformat(as_of_date)]
        bars = bars[bars["Datetime"].dt.date == bars["Datetime"].dt.date.max()].reset_index(drop=True)
        return bars, SimpleNamespace(live=state.live)

    monkeypatch.setattr(intraday_settlement, "load_intraday_with_meta", load)
    monkeypatch.setattr(settlement, "fetch_returns",
                        lambda *a, **k: pytest.fail("an intraday entry was sent to the holding-period return"))
    return state


def _settle(state, now: datetime, reflector=None):
    reflector = reflector or _Reflector()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(intraday_settlement, "_now", lambda: now)
        done = settlement.settle_pending("RELIANCE.NS", state.log, reflector, {})
    return done, reflector


@pytest.mark.unit
@pytest.mark.parametrize("clock", [("2026-10-23", "18:00"), (SESSION, "09:00"), (SESSION, "15:45")],
                         ids=["before the session", "before the open", "closing session, close not final"])
def test_a_plan_waits_until_its_sessions_close_is_final(pending, clock):
    done, reflector = _settle(pending, _ist(*clock))
    assert done.settled == [] and done.failed == [] and pending.loads == [] and reflector.calls == []
    assert pending.log.get_pending_entries()


@pytest.mark.unit
def test_a_plan_is_replayed_on_its_sessions_bars_once_the_close_is_final(pending):
    done, reflector = _settle(pending, _ist(SESSION, "16:05"))

    assert done.settled == [("RELIANCE.NS", "2026-10-23")]
    assert [(load[1], load[2], load[3]) for load in pending.loads] == [(SESSION, "5m", 1)]
    (entry,) = pending.log.load_entries()
    outcome = entry["intraday_outcome"]
    assert (outcome["exit_reason"], outcome["entry_price"], outcome["exit_price"], outcome["shares"]) == (
        "target", 101.0, 104.0, 100)
    # India's intraday charges come off the 300.00 gross: the same round trip intraday_eval's tests compute.
    assert outcome["net_pnl"] == pytest.approx(289.073177) and outcome["scored"] is True
    assert entry["resolved"] == SESSION and entry["reflection"] == "The breakout held to the target."
    decision, summary = reflector.calls[0]
    assert decision == DECISION
    for said in ("Plan for Mon 26 Oct 2026: long, entry 101.00, stop 99.50, target 104.00, exit by 15:10, "
                 "100 shares; read against 100.50", "filled at 101.00 at 09:20 (stop entry), closed at 104.00 at "
                 "09:30 (target)", "net P&L 289.07", "+1.93R", "The session traded: open 100.20, high 104.50"):
        assert said in summary, said


@pytest.mark.unit
def test_a_session_beyond_yahoos_window_is_recorded_as_not_scored_without_a_reflection(pending):
    pending.error = yahoo_intraday.IntradayWindowError("RELIANCE.NS", "RELIANCE.NS", "too old")
    done, reflector = _settle(pending, _ist("2026-12-30", "10:00"))
    assert done.settled == [("RELIANCE.NS", "2026-10-23")] and reflector.calls == []
    (entry,) = pending.log.load_entries()
    assert entry["intraday_outcome"] == {"scored": False, "note": "outside Yahoo's intraday window"}
    assert entry["reflection"] == "Not scored: outside Yahoo's intraday window." and entry["raw"] == "n/a"


@pytest.mark.unit
def test_a_session_with_no_bars_is_not_scored(pending):
    pending.bars = _session_bars("2026-10-23", None)        # the plan's session never printed
    done, reflector = _settle(pending, _ist(SESSION, "16:30"))
    assert done.settled and reflector.calls == []
    (entry,) = pending.log.load_entries()
    assert entry["intraday_outcome"]["exit_reason"] == "no_data" and entry["intraday_outcome"]["scored"] is False


@pytest.mark.unit
def test_an_outage_leaves_the_plan_pending_and_is_reported(pending):
    pending.error = VendorUnavailableError("Yahoo Finance is unreachable")
    done, _ = _settle(pending, _ist(SESSION, "16:30"))
    assert done.settled == [] and done.failed[0][:2] == ("RELIANCE.NS", "2026-10-23")
    assert "intraday bars unavailable" in done.failed[0][2]
    assert pending.log.get_pending_entries()


@pytest.mark.unit
def test_no_bars_served_yet_or_a_session_still_live_waits_quietly(pending):
    pending.error = NoMarketDataError("RELIANCE.NS", "RELIANCE.NS", "no 5m bars")
    assert _settle(pending, _ist(SESSION, "16:30"))[0].failed == []
    pending.error, pending.live = None, True
    done, reflector = _settle(pending, _ist(SESSION, "16:30"))
    assert (done.settled, done.failed, reflector.calls) == ([], [], [])
    assert pending.log.get_pending_entries()


@pytest.mark.unit
def test_a_failed_reflection_leaves_the_plan_pending(pending):
    done, _ = _settle(pending, _ist(SESSION, "16:30"), _Reflector(fail=True))
    assert done.failed and "reflection failed" in done.failed[0][2]
    assert pending.log.get_pending_entries()


@pytest.mark.unit
def test_a_plan_that_cannot_be_replayed_is_reported_and_the_next_ticker_still_settles(pending, monkeypatch):
    pending.log.store_decision("TCS.NS", "2026-10-23", DECISION, rating="Buy", intraday_plan=PLAN)
    real = intraday_settlement.settle

    def settle(ticker, *args, **kwargs):
        if ticker == "RELIANCE.NS":
            raise ZeroDivisionError("float division by zero")
        return real(ticker, *args, **kwargs)

    monkeypatch.setattr(intraday_settlement, "settle", settle)
    monkeypatch.setattr(intraday_settlement, "_now", lambda: _ist(SESSION, "16:30"))
    done = settlement.settle_all_pending(pending.log, _Reflector(), {})
    assert done.failed == [("RELIANCE.NS", "2026-10-23", "intraday settlement failed: float division by zero")]
    assert done.settled == [("TCS.NS", "2026-10-23")]
    assert [e["ticker"] for e in pending.log.get_pending_entries()] == ["RELIANCE.NS"]


@pytest.mark.unit
def test_an_unreadable_plan_is_settled_as_not_scored(tmp_path):
    log = _log(tmp_path)
    log.store_decision("RELIANCE.NS", "2026-10-23", DECISION, intraday_plan={"direction": "sideways"})
    done = settlement.settle_pending("RELIANCE.NS", log, _Reflector(), {})
    assert done.settled
    (entry,) = log.load_entries()
    assert "the stored plan could not be read" in entry["intraday_outcome"]["note"]
    assert entry["resolved"] == "2026-10-23"


@pytest.mark.unit
def test_a_listing_without_a_calendar_waits_for_the_sessions_date_to_pass(tmp_path, monkeypatch):
    log = _log(tmp_path)
    log.store_decision("AAPL", "2026-10-23", DECISION, intraday_plan={**PLAN, "timezone": "America/New_York"})
    monkeypatch.setattr(intraday_settlement, "load_intraday_with_meta",
                        lambda *a, **k: pytest.fail("fetched before the session's date had passed"))
    monkeypatch.setattr(intraday_settlement, "_now",
                        lambda: pd.Timestamp(f"{SESSION} 23:30", tz="UTC").to_pydatetime())
    assert settlement.settle_pending("AAPL", log, _Reflector(), {}).settled == []


@pytest.mark.unit
def test_swing_and_intraday_entries_settle_their_own_way(tmp_path, monkeypatch):
    log = _log(tmp_path)
    log.store_decision("RELIANCE.NS", "2026-10-16", "**Rating**: Hold")
    log.store_decision("RELIANCE.NS", "2026-10-23", DECISION, intraday_plan=PLAN)
    asked = []
    monkeypatch.setattr(settlement, "fetch_returns",
                        lambda ticker, day, holding_days=5, benchmark=None: asked.append(day) or (None,) * 4)
    monkeypatch.setattr(intraday_settlement, "_now", lambda: _ist("2026-10-23", "20:00"))
    settlement.settle_pending("RELIANCE.NS", log, _Reflector(), {})
    assert asked == ["2026-10-16"]


@pytest.mark.unit
def test_the_intraday_reflection_judges_one_session_on_its_bars():
    llm = Capturing(reply="lesson")
    assert Reflector(llm).reflect_on_intraday_plan("**Rating**: Buy", "Plan for Mon 26 Oct 2026: long") == "lesson"
    system, human = llm.seen[0]
    assert "one trading session" in system.content and "5-minute bars" in system.content
    assert "counts as stopped" in system.content and "R multiple" in system.content
    assert human.content == "Plan for Mon 26 Oct 2026: long\n\nFinal Decision:\n**Rating**: Buy"


@pytest.mark.unit
def test_a_not_triggered_plan_is_summarised_as_no_trade():
    plan = intraday_settlement.stored_plan({"intraday_plan": PLAN})
    outcome = IntradayOutcome(False, None, None, None, None, "not_triggered", None, None, None, None, None,
                              100, "stop", "the session never reached the entry")
    summary = intraday_settlement.summarize(plan, outcome, None, ["a note"])
    assert "no trade (the session never reached the entry)" in summary and "Notes: a note." in summary


# --- the plan's session, fixed at run start -------------------------------------------

def _yahoo_frame(days, zone=IST, start="09:15", end="15:30", close=100.0):
    frames = []
    for i, day in enumerate(days):
        index = pd.date_range(f"{day} {start}", f"{day} {end}", freq="5min", inclusive="left", tz=zone,
                              name="Datetime")
        price = close + i
        frames.append(pd.DataFrame({"Open": price, "High": price + 0.5, "Low": price - 0.5, "Close": price,
                                    "Adj Close": price, "Volume": 1000}, index=index))
    return pd.concat(frames)


@pytest.fixture
def yahoo(tmp_path, monkeypatch):
    state = SimpleNamespace(frame=None, calls=[])

    def ticker(symbol):
        def history(**kwargs):
            state.calls.append(kwargs)
            return state.frame.copy()
        return SimpleNamespace(history=history)

    monkeypatch.setattr(yahoo_intraday.yf, "Ticker", ticker)
    monkeypatch.setattr(yahoo_intraday, "get_config",
                        lambda: {"data_cache_dir": str(tmp_path), "intraday_square_off": "15:10"})
    return state


def _today_is(monkeypatch, day: str) -> None:
    """The machine's and every listing's date, as the run's tools read them (``date_window``)."""
    from tradingagents.dataflows import date_window

    monkeypatch.setattr(date_window, "get_current_date", lambda: day)
    monkeypatch.setattr(date_window, "listing_date", lambda symbol, now=None: day)


@pytest.mark.unit
def test_a_past_dated_plan_is_for_the_next_session_and_read_against_its_dates_close(yahoo, monkeypatch):
    yahoo.frame = _yahoo_frame(["2026-10-22", "2026-10-23", SESSION])     # Monday's bars must not leak
    _today_is(monkeypatch, SESSION)

    context = yahoo_intraday.plan_session("RELIANCE.NS", "2026-10-23", now=_ist(SESSION, "12:00"))

    assert context["session"] == SESSION
    assert context["why"] == "the next trading day after the analysis date Fri 23 Oct 2026"
    assert (context["reference_price"], context["reference_through"]) == (101.0, "2026-10-23T15:30:00+05:30")
    assert (context["exit_by"], context["timezone"], context["tz_label"], context["note"]) == (
        "15:10", "Asia/Kolkata", "IST", None)
    assert context["opens"] == "09:15"            # the calendar's open for the plan's session
    assert context["past_dated"] is True          # its plan counts the whole session, though made during it


@pytest.mark.unit
def test_a_plan_made_during_the_session_is_for_the_rest_of_it(yahoo, monkeypatch):
    yahoo.frame = _yahoo_frame(["2026-10-23", SESSION])
    _today_is(monkeypatch, SESSION)
    context = yahoo_intraday.plan_session("RELIANCE.NS", SESSION, now=_ist(SESSION, "10:42"))
    assert context["session"] == SESSION and context["why"].startswith("the rest of today's session")
    assert context["reference_through"] == "2026-10-26T10:40:00+05:30"     # the bar still forming is left out
    assert context["past_dated"] is False


@pytest.mark.unit
def test_a_run_dated_the_machines_today_counts_from_its_decision_though_india_is_a_day_ahead(yahoo, monkeypatch):
    # docs/intraday.md, "Two clocks": a machine in the US evening of Sun 25 Oct, India already on Monday.
    from tradingagents.dataflows import date_window

    yahoo.frame = _yahoo_frame(["2026-10-22", "2026-10-23", SESSION])
    monkeypatch.setattr(date_window, "get_current_date", lambda: "2026-10-25")
    monkeypatch.setattr(date_window, "listing_date", lambda symbol, now=None: SESSION)
    context = yahoo_intraday.plan_session("RELIANCE.NS", "2026-10-25", now=_ist(SESSION, "09:45"))
    assert context["session"] == SESSION and context["past_dated"] is False     # its tools served it as today
    plan = intraday_plan.plan_from_fields({"direction": "long"}, context, decided_at=_ist(SESSION, "09:50"))
    assert datetime.fromisoformat(plan["valid_from"]) == _ist(SESSION, "09:50")


@pytest.mark.unit
def test_without_bars_the_session_still_follows_the_calendar(yahoo):
    context = yahoo_intraday.plan_session("RELIANCE.NS", "2026-06-01", now=_ist(SESSION, "12:00"))
    assert context["session"] == "2026-06-02" and context["reference_price"] is None
    assert "outside that window" in context["note"] and yahoo.calls == []


@pytest.mark.unit
def test_a_us_listing_plans_on_its_own_clock_and_exits_by_the_close(yahoo):
    yahoo.frame = _yahoo_frame(["2026-10-22", "2026-10-23"], zone=ZoneInfo("America/New_York"),
                               start="09:30", end="16:00")
    context = yahoo_intraday.plan_session("AAPL", "2026-10-23", now=datetime(2026, 10, 24, 2, 0, tzinfo=UTC))
    assert (context["session"], context["exit_by"], context["timezone"]) == (SESSION, None, "America/New_York")
    assert context["opens"] == "09:30"            # the bars' usual first bar
    assert yahoo_intraday.plan_session("AAPL", "2026-06-01", now=datetime(2026, 10, 24, 2, 0, tzinfo=UTC))[
        "opens"] is None                          # no bars, so not known


# --- the report -----------------------------------------------------------------------

@pytest.mark.unit
def test_an_intraday_report_saves_the_day_traders_plan_and_names_its_session(tmp_path):
    from tradingagents.reporting import write_report_tree

    state = {"trade_date": "2026-10-23", "intraday_report": "Long above 101.", "decision_horizon": "intraday",
             "intraday_context": CONTEXT, "final_trade_decision": DECISION, "final_rating": "Buy"}
    report = write_report_tree(state, "RELIANCE.NS", tmp_path, settings={"decision_horizon": "intraday"})

    assert (tmp_path / "1_analysts" / "day_trader.md").read_text() == "Long above 101."
    text = report.read_text()
    assert ("- Horizon: intraday paper plan for Mon 26 Oct 2026 (the next trading day after the analysis date "
            "Fri 23 Oct 2026); exit by 15:10 IST") in text
    assert "### Day Trader\nLong above 101." in text
    assert "never sent to a broker" in text
    page = (tmp_path / "complete_report.html").read_text()
    assert "<dt>Horizon</dt>" in page and "never sent to a broker" in page


@pytest.mark.unit
def test_a_swing_report_names_no_horizon(tmp_path):
    from tradingagents.reporting import write_report_tree

    text = write_report_tree({"trade_date": "2026-10-23", "final_trade_decision": DECISION}, "NVDA",
                             tmp_path).read_text()
    assert "Horizon" not in text and "broker" not in text
    assert not (tmp_path / "1_analysts" / "day_trader.md").exists()


# --- the CLI ----------------------------------------------------------------------------

UNATTENDED_ENV = {
    "TRADINGAGENTS_OUTPUT_LANGUAGE": "English",
    "TRADINGAGENTS_MAX_DEBATE_ROUNDS": "1",
    "TRADINGAGENTS_MAX_RISK_ROUNDS": "1",
    "TRADINGAGENTS_LLM_PROVIDER": "openai",
    "TRADINGAGENTS_QUICK_THINK_LLM": "gpt-6-luna",
}
INTRADAY_FLAGS = {"ticker": "RELIANCE", "exchange": "NSE", "analysts": "day_trader,market", "save": True,
                  "show": False, "horizon": "intraday"}


@pytest.mark.unit
def test_the_analyze_command_takes_a_horizon(monkeypatch):
    from typer.testing import CliRunner

    from cli import main as cli_main

    seen = {}
    monkeypatch.setattr(cli_main, "run_analysis", lambda **kw: seen.update(kw))
    result = CliRunner().invoke(cli_main.app, ["--horizon", "Intraday"])
    assert result.exit_code == 0, result.output
    assert seen["flags"]["horizon"] == "intraday"
    bad = CliRunner().invoke(cli_main.app, ["--horizon", "weekly"])
    assert bad.exit_code == 1 and "--horizon takes swing or intraday" in bad.output


@pytest.mark.unit
def test_an_unattended_intraday_run_needs_no_date(monkeypatch):
    from cli import selections

    for name, value in UNATTENDED_ENV.items():
        monkeypatch.setenv(name, value)
    assert selections.unattended_gaps(INTRADAY_FLAGS) == []
    assert "--date" in selections.unattended_gaps({**INTRADAY_FLAGS, "horizon": "swing"})
    assert "--date" in selections.unattended_gaps({**INTRADAY_FLAGS, "horizon": None})


@pytest.mark.unit
def test_an_intraday_run_is_dated_today_without_asking(monkeypatch):
    from cli import selections
    from cli.models import AnalystType
    from tradingagents.dataflows import date_window

    for name, value in UNATTENDED_ENV.items():
        monkeypatch.setenv(name, value)

    def no_prompt(*a, **k):
        raise AssertionError("prompted although the flags and the horizon answered the step")

    for step in ("get_ticker", "get_analysis_date", "select_analysts"):
        monkeypatch.setattr(selections, step, no_prompt)
    monkeypatch.setattr(selections.updates, "enabled", lambda: False)
    # 01:00 on Friday in India: India's date is the machine's, New York's is still Thursday's.
    monkeypatch.setattr(date_window, "get_current_date", lambda: "2026-10-30")
    monkeypatch.setattr(date_window, "listing_date", lambda symbol, now=None: {
        "RELIANCE.NS": "2026-10-30", "AAPL": "2026-10-29"}.get(symbol))

    chosen = selections._prompt_selections({}, INTRADAY_FLAGS)

    assert chosen["ticker"] == "RELIANCE.NS" and chosen["horizon"] == "intraday"
    assert chosen["analysis_date"] == "2026-10-30"
    assert chosen["analysts"] == [AnalystType.MARKET, AnalystType.DAY_TRADER]
    us = selections._prompt_selections({}, {**INTRADAY_FLAGS, "ticker": "AAPL", "exchange": None})
    assert us["analysis_date"] == "2026-10-29"            # the session New York is still on
    past = selections._prompt_selections({}, {**INTRADAY_FLAGS, "date": "2026-10-23"})
    assert past["analysis_date"] == "2026-10-23"          # --date still names a past session's eve


@pytest.mark.unit
def test_a_crypto_pair_has_no_intraday_plan(monkeypatch):
    import typer

    from cli import selections

    monkeypatch.setattr(selections.updates, "enabled", lambda: False)
    with pytest.raises(typer.Exit):
        selections._prompt_selections({}, {**INTRADAY_FLAGS, "ticker": "BTC-USD", "exchange": None})


@pytest.mark.unit
def test_the_day_trader_is_named_and_kept_from_crypto_and_the_backtest_default(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    import cli.main as m
    from cli.models import AnalystType, AssetType
    from cli.prompts import parse_analysts

    assert parse_analysts("day-trader,market", AssetType.STOCK) == [AnalystType.MARKET, AnalystType.DAY_TRADER]
    with pytest.raises(ValueError, match="day_trader is not available for crypto"):
        parse_analysts("day_trader", AssetType.CRYPTO)

    swept = []
    result = SimpleNamespace(cells_run=0, skipped=0, log_path=tmp_path / "log.md", run_id="r", failures=[],
                             settlement_failures=[])
    monkeypatch.setattr(m, "run_backtest", lambda *a, **kw: swept.append(kw) or result)
    monkeypatch.setattr(m, "summarize", lambda log: SimpleNamespace(render=lambda: "scored"))
    out = CliRunner().invoke(m.app, ["backtest", "NVDA", "--start", "2026-06-01", "--end", "2026-06-01"])
    assert out.exit_code == 0, out.output
    assert swept[0]["selected_analysts"] == ["market", "social", "news", "fundamentals"]


@pytest.mark.unit
def test_every_analyst_is_in_the_cli_lists_so_none_is_dropped():
    """cli/run.py keeps only the analysts in ANALYST_ORDER: one missing would vanish from every run."""
    from cli.display import ANALYST_AGENT_NAMES, ANALYST_ORDER, ANALYST_REPORT_MAP, MessageBuffer

    for key, spec in ANALYST_NODE_SPECS.items():
        assert key in ANALYST_ORDER
        assert ANALYST_AGENT_NAMES[key] == MessageBuffer.ANALYST_MAPPING[key] == spec.agent_node
        assert ANALYST_REPORT_MAP[key] == spec.report_key
        assert MessageBuffer.REPORT_SECTIONS[spec.report_key] == (key, spec.agent_node)


@pytest.mark.unit
def test_the_live_view_follows_the_day_traders_report():
    from cli.display import MessageBuffer, update_analyst_statuses

    buffer = MessageBuffer()
    buffer.init_for_analysis(["market", "day_trader"])
    update_analyst_statuses(buffer, {"intraday_report": "Long above 101."})
    assert buffer.agent_status["Day Trader"] == "completed"
    assert buffer.agent_status["Market Analyst"] == "in_progress"
    assert buffer.current_report.startswith("### Day Trader Plan")


@pytest.mark.unit
def test_the_run_config_takes_the_horizon():
    from cli.run import _build_run_config

    selections = {"research_depth": 1, "quick_think_llm": "q", "deep_think_llm": "d", "backend_url": None,
                  "llm_provider": "openai"}
    assert _build_run_config({**selections, "horizon": "intraday"}, None)["decision_horizon"] == "intraday"
    assert _build_run_config(selections, None)["decision_horizon"] == "swing"


@pytest.mark.unit
def test_an_intraday_cli_run_adds_the_day_trader_to_the_graph_and_the_display(monkeypatch, tmp_path):
    import cli.run as cli_run
    from cli.models import AnalystType

    built, shown = [], []

    class Graph:
        _resuming = False

        def __init__(self, analysts, **kwargs):
            built.append(list(analysts))
            self.propagator = self

        def create_run_state(self, *a):
            return {"messages": [], "decision_horizon": "intraday", "intraday_context": CONTEXT}

        def get_graph_args(self, callbacks=None):
            return {}

        def begin_checkpoint(self, *a):
            return None

        def checkpoint_input(self, state):
            return state

        def stream_run(self, graph_input, **kwargs):
            yield [], {"messages": [], "intraday_report": "plan", "final_trade_decision": DECISION}

        def record_decision(self, *a):
            pass

        def clear_checkpoint_on_success(self, *a):
            pass

        def end_checkpoint(self):
            pass

    class Live:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(cli_run, "TradingAgentsGraph", Graph)
    monkeypatch.setattr(cli_run, "Live", Live)
    monkeypatch.setattr(cli_run, "create_layout", lambda: None)
    monkeypatch.setattr(cli_run, "update_display", lambda *a, **k: None)
    monkeypatch.setattr(cli_run, "_offer_reports", lambda *a, **k: None)
    monkeypatch.setattr(cli_run, "get_user_selections", lambda flags=None: {
        "ticker": "RELIANCE.NS", "analysis_date": "2026-10-23", "analysts": [AnalystType.MARKET],
        "asset_type": "stock", "horizon": "intraday"})
    monkeypatch.setattr(cli_run, "_build_run_config", lambda selections, checkpoint: {
        "results_dir": str(tmp_path), "decision_horizon": "intraday"})

    class Buffer:     # the live view's buffer, as the run drives it
        def __init__(self):
            self.messages, self.tool_calls, self.report_sections, self.agent_status = [], [], {}, {}
            self.selected_analysts, self._processed_message_ids = [], set()

        def init_for_analysis(self, analysts):
            self.selected_analysts = list(analysts)

        def add_message(self, kind, content):
            self.messages.append((0.0, kind, content))
            shown.append(content)

        def add_tool_call(self, name, args):
            self.tool_calls.append((0.0, name, args))

        def update_report_section(self, *a):
            pass

        def update_agent_status(self, agent, status):
            self.agent_status[agent] = status

    buffer = Buffer()
    monkeypatch.setattr(cli_run, "message_buffer", buffer)

    cli_run.run_analysis(flags={"save": False, "show": False})

    assert built == [["market", "day_trader"]]
    assert buffer.selected_analysts == ["market", "day_trader"]
    assert any(str(m).startswith("Decision horizon: intraday paper plan for Mon 26 Oct 2026") for m in shown)


@pytest.mark.unit
def test_a_backtest_summary_of_a_mixed_log_scores_the_swing_ratings_only(tmp_path):
    from tradingagents.backtest import summarize

    log = _log(tmp_path)
    log.store_decision("NVDA", "2026-10-16", "**Rating**: Buy")
    log.update_with_outcome("NVDA", "2026-10-16", 0.04, 0.02, 5, "worked", "2026-10-23")
    log.store_decision("RELIANCE.NS", "2026-10-23", DECISION, intraday_plan=PLAN)
    log.update_with_intraday_outcome("RELIANCE.NS", "2026-10-23", {"net_return_pct": 2.9, "exit_reason": "target"},
                                     "held", resolution_date=SESSION)
    summary = summarize(tmp_path / "memory.md")
    assert (summary.resolved, summary.pending, list(summary.by_rating)) == (1, 0, ["Buy"])
