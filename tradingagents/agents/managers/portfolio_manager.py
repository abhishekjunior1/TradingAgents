"""Portfolio Manager: synthesises the risk-analyst debate into the final decision.

Uses LangChain's ``with_structured_output`` so the LLM produces a typed
``PortfolioDecision`` directly, in a single call. Its rating is the run's
``final_rating``, and the decision is rendered to markdown as
``final_trade_decision`` for the memory log, CLI display and saved reports.
When a provider does not expose structured output, the agent falls back to
free-text generation and the rating is read from that text.

In intraday mode (``horizon="intraday"``) the decision also carries a paper plan
for one session (``IntradayPortfolioDecision``), kept as data in
``intraday_plan`` for the memory log to score once that session has closed.
"""

from __future__ import annotations

from datetime import date

from tradingagents.agents import intraday_plan
from tradingagents.agents.context import (
    get_instrument_context_from_state,
    get_language_instruction,
    get_portfolio_context_from_state,
    report_or_absent,
)
from tradingagents.agents.rating import parse_rating
from tradingagents.agents.schemas import (
    IntradayPortfolioDecision,
    PortfolioDecision,
    intraday_plan_fields,
    plan_checks_line,
    render_intraday_pm_decision,
    render_pm_decision,
)
from tradingagents.agents.structured import NO_EXTERNAL_TOOLS, bind_structured, invoke_structured
from tradingagents.dataflows.config import get_config


def create_portfolio_manager(llm, horizon: str = "swing"):
    if horizon == "intraday":
        return _create_intraday_portfolio_manager(llm)
    structured_llm = bind_structured(llm, PortfolioDecision, "Portfolio Manager")

    def portfolio_manager_node(state) -> dict:
        instrument_context = get_instrument_context_from_state(state)
        portfolio_context = get_portfolio_context_from_state(state)

        history = state["risk_debate_state"]["history"]
        risk_debate_state = state["risk_debate_state"]
        research_plan = state["investment_plan"]
        trader_plan = state["trader_investment_plan"]

        past_context = state.get("past_context", "")
        lessons_line = (
            f"- Lessons from prior decisions and outcomes:\n{past_context}\n"
            if past_context
            else ""
        )

        prompt = f"""As the Portfolio Manager, synthesize the risk analysts' debate and deliver the final trading decision.

{instrument_context}

{portfolio_context}

---

**Rating Scale** (use exactly one):
- **Buy**: Strong conviction to enter or add to position
- **Overweight**: Favorable outlook, gradually increase exposure
- **Hold**: Maintain current position, no action needed
- **Underweight**: Reduce exposure, take partial profits
- **Sell**: Exit position or avoid entry

**Context:**
- Research Manager's investment plan: **{research_plan}**
- Trader's transaction proposal: **{trader_plan}**
{lessons_line}
**Risk Analysts Debate History:**
{history}

---

Ground every conclusion in specific evidence from the analysts. The risk debate always contains conflicting stances; deciding which is stronger is the job, so conflict alone is not a reason to Hold. Commit to the stronger case, sized by how decisively it wins. Choose Hold only when the evidence is still balanced after that weighing, or too thin to support a call; do not force a direction to appear decisive. Weigh the analysts on their merits, independent of speaking order.

## Output

Write these sections, in this order, starting with the rating on its own line:

- **Rating**: exactly one of Buy / Overweight / Hold / Underweight / Sell
- **Executive Summary**: the call and how to act on it
- **Investment Thesis**: the evidence that decided it, and what would change it
- **Bull Case**, **Base Case**, **Bear Case**: each scenario and the evidence for it
- **Key Risks**: what could make the call wrong, including data the analysis lacked
- **Confidence**: low, medium or high, given the evidence and its gaps

This is a research conclusion, not a prediction or a guarantee.

{NO_EXTERNAL_TOOLS}{get_language_instruction()}"""

        # The typed rating is the decision; the rendered text only carries it.
        # Read back from text, a rating the thesis quotes could replace it. The
        # other typed fields are kept as data for the same reason.
        decision = invoke_structured(structured_llm, prompt, "Portfolio Manager")
        if decision is not None:
            final_trade_decision = render_pm_decision(decision)
            final_rating = decision.rating.value
        else:
            final_trade_decision = llm.invoke(prompt).content
            final_rating = parse_rating(final_trade_decision)

        return {
            "risk_debate_state": _judged(risk_debate_state),
            "final_trade_decision": final_trade_decision,
            "final_rating": final_rating,
            **({"portfolio_decision": decision.model_dump(mode="json")} if decision is not None else {}),
        }

    return portfolio_manager_node


def _judged(risk_debate_state) -> dict:
    """The risk debate as the Portfolio Manager closes it."""
    return {
        "history": risk_debate_state["history"],
        "aggressive_history": risk_debate_state["aggressive_history"],
        "conservative_history": risk_debate_state["conservative_history"],
        "neutral_history": risk_debate_state["neutral_history"],
        "latest_speaker": "Judge",
        "current_aggressive_response": risk_debate_state["current_aggressive_response"],
        "current_conservative_response": risk_debate_state["current_conservative_response"],
        "current_neutral_response": risk_debate_state["current_neutral_response"],
        "count": risk_debate_state["count"],
    }


def _session_name(context) -> str | None:
    """The plan's session as the decision names it: "Fri 30 Oct 2026, <why>"."""
    try:
        day = date.fromisoformat(str((context or {}).get("session")))
    except ValueError:
        return None
    return f"{day:%a %d %b %Y}, {context.get('why')}" if context.get("why") else f"{day:%a %d %b %Y}"


def _paper_sizing(state) -> dict:
    """What the snapshot's paper sizing used: the portfolio's value when the run has one, else the
    configured capital; the risk, the leverage cap, and the configured intraday charges."""
    config = get_config()
    value = state.get("portfolio_value")
    return {"capital": value if value and value > 0 else config.get("intraday_paper_capital", 100_000),
            "risk_pct": config.get("intraday_risk_pct", 0.5),
            "leverage_cap": config.get("intraday_leverage_cap", 1.0),
            **{key: config[key] for key in ("intraday_cost_profile", "intraday_cost_overrides") if key in config}}


def _create_intraday_portfolio_manager(llm):
    structured_llm = bind_structured(llm, IntradayPortfolioDecision, "Portfolio Manager")

    def intraday_portfolio_manager_node(state) -> dict:
        instrument_context = get_instrument_context_from_state(state)
        portfolio_context = get_portfolio_context_from_state(state)
        context = state.get("intraday_context") or {}
        brief = intraday_plan.session_brief(context)
        exit_by = context.get("exit_by") or "the session close"

        risk_debate_state = state["risk_debate_state"]
        history = risk_debate_state["history"]
        research_plan = state["investment_plan"]
        trader_plan = state["trader_investment_plan"]
        day_trader_report = report_or_absent(state.get("intraday_report", ""), "day trader")

        past_context = state.get("past_context", "")
        lessons_line = (
            f"- Lessons from prior decisions and outcomes:\n{past_context}\n"
            if past_context
            else ""
        )
        language = get_language_instruction()

        prompt = f"""As the Portfolio Manager, synthesize the risk analysts' debate and deliver the final intraday decision: a paper plan for one trading session.

{instrument_context}

{portfolio_context}

{brief}

---

**Rating Scale** (use exactly one):
- **Buy**: Strong conviction to enter or add to position
- **Overweight**: Favorable outlook, gradually increase exposure
- **Hold**: Maintain current position, no action needed
- **Underweight**: Reduce exposure, take partial profits
- **Sell**: Exit position or avoid entry

**Context:**
- Research Manager's investment plan: **{research_plan}**
- Trader's intraday proposal: **{trader_plan}**
- Day Trader's report (intraday levels, setup and paper sizing; the source of truth for intraday numbers): {day_trader_report}
{lessons_line}
**Risk Analysts Debate History:**
{history}

---

Ground every conclusion in specific evidence from the analysts. The risk debate always contains conflicting stances; deciding which is stronger is the job, so conflict alone is not a reason to Hold. Commit to the stronger case, sized by how decisively it wins. Choose Hold only when the evidence is still balanced after that weighing, or too thin to support a call; do not force a direction to appear decisive. Weigh the analysts on their merits, independent of speaking order.

The plan's direction follows the rating: Buy or Overweight is a long; Underweight or Sell is a short (intraday only) or no trade; Hold is no trade. Take every price, level and share count from the reports, the Day Trader's intraday snapshot first; if a number the plan needs is not there, leave its field empty and say so rather than estimate it. Stand aside (direction none) on a session the reports flag as one not to trade: a results or other scheduled event day, very low relative volume, a gap near the stock's price band, or a square-off time already passed.

## Output

Write these sections, in this order, starting with the rating on its own line:

- **Rating**: exactly one of Buy / Overweight / Hold / Underweight / Sell
- **Executive Summary**: the call and how to act on it during the session
- **Investment Thesis**: the evidence that decided it, and what would change it
- **Direction**: long, short or none
- **Entry Price**: the price that triggers the entry
- **Stop Loss**: the price that closes the position at a loss
- **Target Price**: the price that takes the profit
- **Exit By**: HH:MM in the exchange's time zone, no later than {exit_by}
- **Position Size (shares)**: the whole number of shares the Day Trader's paper sizing gives for this stop
- **Invalidation**: what cancels the plan before or after the entry
- **Bull Case**, **Base Case**, **Bear Case**: each scenario for the session and the evidence for it
- **Key Risks**: what could make the call wrong, including data the analysis lacked
- **Confidence**: low, medium or high, given the evidence and its gaps

This is a paper plan for research, not an order, a prediction or a guarantee.

{NO_EXTERNAL_TOOLS}{language}{intraday_plan.PLAN_LABELS_NOTE if language else ""}"""

        decision = invoke_structured(structured_llm, prompt, "Portfolio Manager")
        if decision is not None:
            final_rating = decision.rating.value
            fields = intraday_plan_fields(decision)
        else:
            text = llm.invoke(prompt).content
            final_rating = parse_rating(text)
            fields = intraday_plan.parse_plan_lines(text)
        # The reports show the plan as it is stored and scored, with its notes.
        plan = intraday_plan.plan_from_fields(fields, context, rating=final_rating, sizing=_paper_sizing(state))
        if decision is not None:
            final_trade_decision = render_intraday_pm_decision(decision, _session_name(context), plan)
        else:
            checks = plan_checks_line(plan["notes"])
            final_trade_decision = f"{text}\n\n{checks}" if checks else text

        return {
            "risk_debate_state": _judged(risk_debate_state),
            "final_trade_decision": final_trade_decision,
            "final_rating": final_rating,
            "intraday_plan": plan,
        }

    return intraday_portfolio_manager_node
