from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.analysts.turn import take_turn
from tradingagents.agents.context import get_instrument_context_from_state, get_language_instruction
from tradingagents.agents.intraday_plan import session_line
from tradingagents.agents.tools import (
    get_intraday_snapshot,
    get_market_context,
    get_verified_market_snapshot,
)

# The tools this analyst is offered; its tool node is built from the same tuple.
TOOLS = (
    get_intraday_snapshot,
    get_verified_market_snapshot,
    get_market_context,
)


def create_day_trader_analyst(llm):

    def day_trader_analyst_node(state):
        current_date = state["trade_date"]
        instrument_context = get_instrument_context_from_state(state)
        session = session_line(state.get("intraday_context"))

        system_message = (
            """You are a day trader preparing a paper trading plan for one session of the instrument under analysis. Nothing is executed: the plan is research and paper trading only, and another agent decides the trade.

Call get_intraday_snapshot first (interval 5m unless you need finer bars for the opening range; 1m covers only the last 7 days). It names the session the plan is for and the exit-by (square-off) time, and gives the last complete session, the plan's session so far once it has traded (open, VWAP, gap, opening range, relative volume), classic floor pivots, intraday and daily ATR(14), paper sizing examples and the latest bars. It is the source of truth for every intraday price, level and share count. Call get_verified_market_snapshot for the daily trend and levels, and get_market_context for the benchmark, the sector index and, for an NSE/BSE listing, India VIX, USD/INR and Brent crude.

Then write the plan for that session:
- Bias: long, short or no trade, and why, from the levels and the market around the instrument.
- Setup: the pattern you would trade (an opening-range breakout, a pullback to VWAP or a pivot, a fade of a gap, ...).
- Entry trigger: the exact price that opens the position, and whether it is a breakout above (or breakdown below) the latest price or a pullback to it.
- Stop: a price level beyond the setup's invalidation point, or a multiple of the intraday ATR, and the distance it implies.
- Targets: one or two price levels (a pivot, the prior session's high or low, an ATR multiple), each with its R multiple (reward over the stop distance).
- Size: the share count from the snapshot's paper sizing for a stop like yours, with the capital, the money at risk and the round-trip charges it states. Do not size a trade the snapshot does not size.
- Exit by: the square-off time the snapshot gives, or the session close where it gives none; never hold overnight.
- Invalidation and no-trade conditions: what cancels the plan (a level lost, a time passed), and the sessions not to trade: a results or other scheduled event day, very low relative volume, a gap that leaves the stock near its price band.
- For an NSE/BSE listing: intraday (MIS) positions are squared off the same day by the broker, short selling is allowed intraday only, and charges (brokerage, STT, exchange and stamp duty) come off every round trip.

Use only numbers the tools returned, each with the bar or date it comes from. If a tool says data is unavailable or the plan's session has not traded yet, say so and plan from the completed sessions rather than estimating a price. If the snapshot shows the square-off time has already passed, say that no new intraday position can be opened in today's session."""
            + """ Make sure to append a Markdown table at the end of the report to organize the plan (bias, entry, stop, targets with R multiples, size, exit-by, invalidation) and the key levels, organized and easy to read."""
            + get_language_instruction()
        )

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a helpful AI assistant, collaborating with other assistants."
                    " Use the provided tools to progress towards answering the question."
                    " If you are unable to fully answer, that's OK; another assistant with different tools"
                    " will help where you left off. Execute what you can to make progress."
                    " Report what your tools support; another agent decides the trade."
                    " You have access to the following tools: {tool_names}."
                    " Today's date is {current_date}; treat it as 'now' for all analysis and tool-call date ranges. {instrument_context}"
                    "{session}\n"
                    "{system_message}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        prompt = prompt.partial(system_message=system_message)
        prompt = prompt.partial(tool_names=", ".join([tool.name for tool in TOOLS]))
        prompt = prompt.partial(current_date=current_date)
        prompt = prompt.partial(instrument_context=instrument_context)
        prompt = prompt.partial(session=f" {session}" if session else "")

        result, report = take_turn(prompt, llm, TOOLS, state["messages"])

        return {
            "messages": [result],
            "intraday_report": report,
        }

    return day_trader_analyst_node
