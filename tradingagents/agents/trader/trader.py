"""Trader: turns the Research Manager's investment plan into a concrete transaction proposal.

In intraday mode (``horizon="intraday"``) the proposal is a paper plan for one
trading session, grounded in the Day Trader's intraday snapshot.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage

from tradingagents.agents.context import (
    get_instrument_context_from_state,
    get_language_instruction,
    get_portfolio_context_from_state,
    report_or_absent,
)
from tradingagents.agents.intraday_plan import PLAN_LABELS_NOTE, session_brief
from tradingagents.agents.schemas import (
    IntradayTraderProposal,
    TraderProposal,
    render_intraday_trader_proposal,
    render_trader_proposal,
)
from tradingagents.agents.structured import (
    NO_EXTERNAL_TOOLS,
    bind_structured,
    invoke_structured,
    invoke_structured_or_freetext,
)


def create_trader(llm, horizon: str = "swing"):
    if horizon == "intraday":
        return _create_intraday_trader(llm)
    structured_llm = bind_structured(llm, TraderProposal, "Trader")

    def trader_node(state):
        company_name = state["company_of_interest"]
        instrument_context = get_instrument_context_from_state(state)
        investment_plan = state["investment_plan"]
        # The research plan digests the debate but loses exact price structure;
        # give the Trader the technical market report so entry/stop levels are
        # grounded in real ATR / support-resistance / current price (#1167). The
        # report is empty when the user did not select the market analyst, so
        # only offer it (and the grounding instruction) when it has content.
        market_report = (state["market_report"] or "").strip()
        portfolio_context = get_portfolio_context_from_state(state)

        if market_report:
            grounding = (
                "Ground concrete price levels (entry, stop-loss, position sizing) in the technical "
                "market report's price structure -- current price, support/resistance, ATR, and "
                "volatility -- and use the research plan for direction and strategy. "
            )
            report_section = f"Technical Market Report:\n{market_report}\n\n"
        else:
            grounding = ""
            report_section = ""

        messages = [
            {
                "role": "system",
                "content": (
                    "You are a trading agent analyzing market data to make investment decisions. "
                    "Based on your analysis, provide a specific recommendation to buy, sell, or hold. "
                    + grounding
                    # Entry/stop are numeric price fields. Asking for concrete
                    # levels invites a percentage ("15%"), which is not a price
                    # and fails the structured parse (#1288).
                    + "State entry price and stop-loss as absolute price levels in the "
                    "instrument's quote currency (for example 189.5), never a percentage "
                    "or a range; convert a percentage distance to the price level it "
                    "implies, or omit the field if you cannot state a number. "
                    + NO_EXTERNAL_TOOLS
                    + get_language_instruction()
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Here is the research team's investment plan for {company_name}. "
                    f"{instrument_context}\n\n"
                    f"{report_section}"
                    f"{portfolio_context}\n\n"
                    f"Proposed Investment Plan:\n{investment_plan}\n\n"
                    "Make an informed, strategic trading decision.\n\n"
                    "## Output\n\n"
                    "Write these sections, in this order, starting with the action "
                    "on its own line:\n\n"
                    "- **Action**: exactly one of Buy / Hold / Sell. A research "
                    "recommendation of Overweight is a Buy and Underweight is a Sell, "
                    "sized by how strong the case is; conflict alone is not a Hold.\n"
                    "- **Reasoning**: why, against the plan and the price structure\n"
                    "- **Entry Price**, **Stop Loss**, **Position Sizing**: when you can state them"
                ),
            },
        ]

        # The proposal is also kept as data: its stop is read from there, not
        # from a label line its reasoning might quote.
        proposal = invoke_structured(structured_llm, messages, "Trader")
        trader_plan = render_trader_proposal(proposal) if proposal is not None else llm.invoke(messages).content

        return {
            "messages": [AIMessage(content=trader_plan)],
            "trader_investment_plan": trader_plan,
            **({"trader_proposal": proposal.model_dump(mode="json")} if proposal is not None else {}),
        }

    return trader_node



def _create_intraday_trader(llm):
    structured_llm = bind_structured(llm, IntradayTraderProposal, "Trader")

    def intraday_trader_node(state):
        company_name = state["company_of_interest"]
        instrument_context = get_instrument_context_from_state(state)
        investment_plan = state["investment_plan"]
        portfolio_context = get_portfolio_context_from_state(state)
        brief = session_brief(state.get("intraday_context"))
        day_trader_report = report_or_absent(state.get("intraday_report", ""), "day trader")
        market_report = (state.get("market_report") or "").strip()
        market_section = f"Technical Market Report:\n{market_report}\n\n" if market_report else ""
        language = get_language_instruction()

        messages = [
            {
                "role": "system",
                "content": (
                    "You are a day trader turning the research team's investment plan into a paper plan for one "
                    "trading session; no order is placed. Based on your analysis, provide a specific recommendation "
                    "to buy, sell short, or stay out for that session. Ground the entry trigger, stop-loss, target "
                    "and share count in the Day Trader's intraday snapshot, the source of truth for intraday prices, "
                    "levels and paper sizes, and use the research plan for direction. State every price as an "
                    "absolute level in the instrument's quote currency (for example 1405.5), never a percentage or "
                    "a range, take the share count from the snapshot's paper sizing for your stop, and leave a field "
                    "empty rather than estimate it. The position is closed by the exit-by time the session brief "
                    "gives. "
                    + NO_EXTERNAL_TOOLS
                    + language
                    + (PLAN_LABELS_NOTE if language else "")
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Here is the research team's investment plan for {company_name}. "
                    f"{instrument_context}\n\n"
                    f"{brief}\n\n"
                    f"Day Trader Report:\n{day_trader_report}\n\n"
                    f"{market_section}"
                    f"{portfolio_context}\n\n"
                    f"Proposed Investment Plan:\n{investment_plan}\n\n"
                    "Make an informed paper plan for that session.\n\n"
                    "## Output\n\n"
                    "Write these sections, in this order, starting with the action "
                    "on its own line:\n\n"
                    "- **Action**: exactly one of Buy / Hold / Sell. Buy opens a long, Sell opens a short "
                    "(intraday only), Hold is no trade. A research recommendation of Overweight is a Buy and "
                    "Underweight is a Sell or no trade; conflict alone is not a Hold, but a session the Day "
                    "Trader flags as one not to trade is.\n"
                    "- **Reasoning**: why, against the plan and the intraday levels\n"
                    "- **Direction**: long, short or none\n"
                    "- **Entry Price**: the trigger price\n"
                    "- **Stop Loss**, **Target Price**: price levels, with the target's R multiple in the reasoning\n"
                    "- **Exit By**: HH:MM in the exchange's time zone, no later than the exit-by time above\n"
                    "- **Position Size (shares)**: from the snapshot's paper sizing for this stop\n"
                    "- **Position Sizing**: the capital, money at risk and charges that size implies\n"
                    "- **Invalidation**: what cancels the plan"
                ),
            },
        ]

        trader_plan = invoke_structured_or_freetext(
            structured_llm,
            llm,
            messages,
            render_intraday_trader_proposal,
            "Trader",
        )

        return {
            "messages": [AIMessage(content=trader_plan)],
            "trader_investment_plan": trader_plan,
        }

    return intraday_trader_node
