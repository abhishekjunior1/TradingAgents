from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace

from tradingagents.agents.analysts import (
    day_trader_analyst,
    fundamentals_analyst,
    market_analyst,
    news_analyst,
)


@dataclass(frozen=True)
class AnalystNodeSpec:
    key: str
    agent_node: str
    report_key: str
    tools: tuple = ()


@dataclass(frozen=True)
class AnalystExecutionPlan:
    specs: list[AnalystNodeSpec]


ANALYST_NODE_SPECS: dict[str, AnalystNodeSpec] = {
    "market": AnalystNodeSpec(
        key="market",
        agent_node="Market Analyst",
        report_key="market_report",
        tools=market_analyst.TOOLS,
    ),
    "social": AnalystNodeSpec(
        # Saved configs select this analyst as "social". It fetches its
        # sources before calling the model, so it has no tools.
        key="social",
        agent_node="Sentiment Analyst",
        report_key="sentiment_report",
    ),
    "news": AnalystNodeSpec(
        key="news",
        agent_node="News Analyst",
        report_key="news_report",
        tools=news_analyst.TOOLS,
    ),
    "fundamentals": AnalystNodeSpec(
        key="fundamentals",
        agent_node="Fundamentals Analyst",
        report_key="fundamentals_report",
        tools=fundamentals_analyst.TOOLS,
    ),
    "day_trader": AnalystNodeSpec(
        key="day_trader",
        agent_node="Day Trader",
        report_key="intraday_report",
        tools=day_trader_analyst.TOOLS,
    ),
}


# Tools an analyst is offered as well while a config switch is on.
SWITCHED_TOOLS: dict[str, tuple[str, tuple]] = {
    "news": ("agent_reach_enabled", news_analyst.WEB_RESEARCH_TOOLS),
}


def analysts_for_horizon(selected_analysts: Iterable[str], horizon: str | None) -> tuple[str, ...]:
    """The analysts a run uses: an intraday plan needs the Day Trader's levels, so it joins them."""
    chosen = tuple(selected_analysts)
    if horizon == "intraday" and "day_trader" not in chosen:
        chosen += ("day_trader",)
    return chosen


def build_analyst_execution_plan(
    selected_analysts: Iterable[str],
    config: Mapping | None = None,
) -> AnalystExecutionPlan:
    """The selected analysts' specs, in order, with the tools ``config`` switches on."""
    specs: list[AnalystNodeSpec] = []
    for analyst_key in selected_analysts:
        spec = ANALYST_NODE_SPECS.get(analyst_key)
        if spec is None:
            raise ValueError(f"unknown analyst key: {analyst_key}")
        switch, extra = SWITCHED_TOOLS.get(analyst_key, (None, ()))
        if switch and (config or {}).get(switch):
            spec = replace(spec, tools=spec.tools + extra)
        specs.append(spec)

    if not specs:
        raise ValueError("at least one analyst must be selected")

    return AnalystExecutionPlan(specs=specs)


