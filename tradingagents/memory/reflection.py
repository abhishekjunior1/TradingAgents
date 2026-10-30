"""Reflection: a settled decision's outcome turned into a short lesson for later runs."""

from typing import Any


class Reflector:
    """Handles reflection on trading decisions."""

    def __init__(self, quick_thinking_llm: Any):
        """Initialize the reflector with an LLM."""
        self.quick_thinking_llm = quick_thinking_llm

    def _system_prompt(self, holding_days: int) -> str:
        """Concise prompt for reflect_on_final_decision (settled memory log entries).

        Produces 2-4 sentences of plain prose, compact enough to be re-injected
        into future agent prompts without bloating the context window. The
        window is named because it bounds what the outcome can show: a thesis
        written for months is not disproved by a week, and a lesson that ignores
        the difference is read by later runs as an established failure.
        """
        return (
            "You are a trading analyst reviewing your own past decision now that the outcome is known.\n"
            f"The outcome covers {holding_days} trading days from the first close after the analysis date, "
            "which may be shorter than the horizon the decision was written for.\n"
            "Write exactly 2-4 sentences of plain prose (no bullets, no headers, no markdown).\n\n"
            "Cover in order:\n"
            f"1. What the {holding_days}-day alpha shows about the directional call (cite the figure), "
            "and say so plainly if the window is too short to judge the thesis.\n"
            "2. Which part of the investment thesis this window supports or undercuts.\n"
            "3. One concrete lesson to apply to the next similar analysis.\n\n"
            "Be specific and terse. Your output will be stored verbatim in a memory log "
            "and re-read by future analysts, so every word must earn its place."
        )

    def reflect_on_final_decision(
        self,
        final_decision: str,
        raw_return: float,
        alpha_return: float,
        benchmark_name: str = "SPY",
        holding_days: int = 5,
    ) -> str:
        """Single reflection call on the final trade decision with outcome context.

        Used when a pending decision is settled. The final_trade_decision already
        synthesises all analyst insights, so no separate market context is needed.
        ``benchmark_name`` is the label used for the alpha line (e.g. ``"SPY"``
        for US tickers, ``"^N225"`` for ``.T`` listings); defaults to SPY for
        callers that haven't been updated to thread the benchmark through.
        """
        messages = [
            ("system", self._system_prompt(holding_days)),
            (
                "human",
                (
                    f"Raw return over {holding_days} trading days: {raw_return:+.1%}\n"
                    f"Alpha vs {benchmark_name}: {alpha_return:+.1%}\n\n"
                    f"Final Decision:\n{final_decision}"
                ),
            ),
        ]
        return self.quick_thinking_llm.invoke(messages).content

    @staticmethod
    def _intraday_system_prompt() -> str:
        """The intraday variant: a paper plan for one session, replayed on that session's bars.

        The replay's rules are named because they bound what the outcome shows: a
        plan stopped in a bar that also reached its target may not have been, and
        one session says little about a setup's edge.
        """
        return (
            "You are a day trader reviewing your own paper plan for one trading session now that the session "
            "has closed.\n"
            "The outcome replays the plan on that session's 5-minute bars: an order fills only when a bar reaches "
            "its price, a bar that reaches both the stop and the target counts as stopped, charges are estimates, "
            "and it covers that one session only.\n"
            "Write exactly 2-4 sentences of plain prose (no bullets, no headers, no markdown).\n\n"
            "Cover in order:\n"
            "1. What the plan did in the session (filled or not, how it exited, the net result and R multiple; "
            "cite the figures), judged against the session's range.\n"
            "2. Which part of the plan (direction, entry trigger, stop, target, timing) the session supports or "
            "undercuts, and say so plainly if one session is too little to judge the setup.\n"
            "3. One concrete lesson to apply to the next intraday plan.\n\n"
            "Be specific and terse. Your output will be stored verbatim in a memory log "
            "and re-read by future analysts, so every word must earn its place."
        )

    def reflect_on_intraday_plan(self, final_decision: str, outcome_summary: str) -> str:
        """Single reflection call on a settled intraday plan.

        ``outcome_summary`` is the plan, what its replay on the session's bars did
        and the session's range, as the settlement writes them.
        """
        messages = [
            ("system", self._intraday_system_prompt()),
            ("human", f"{outcome_summary}\n\nFinal Decision:\n{final_decision}"),
        ]
        return self.quick_thinking_llm.invoke(messages).content
