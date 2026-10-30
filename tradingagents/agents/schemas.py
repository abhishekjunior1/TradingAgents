"""Pydantic schemas used by agents that produce structured output.

The framework's primary artifact is still prose: each agent's natural-language
reasoning is what users read in the saved markdown reports and what the
downstream agents read as context.  Structured output is layered onto the
three decision-making agents (Research Manager, Trader, Portfolio Manager)
so that:

- Their outputs follow consistent section headers across runs and providers
- Each provider's native structured-output mode is used (json_schema for
  OpenAI/xAI, response_schema for Gemini, tool-use for Anthropic)
- Schema field descriptions become the model's output instructions, freeing
  the prompt body to focus on context and the rating-scale guidance
- A render helper turns the parsed Pydantic instance back into the same
  markdown shape the rest of the system already consumes, so display,
  memory log, and saved reports keep working unchanged
"""

from __future__ import annotations

import math
import re
from datetime import time
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# LLMs sometimes write a placeholder string ("None", "N/A", ...) into an optional
# numeric field instead of omitting it. Coerce those to None so the structured
# call validates instead of erroring (#1058). Pydantic still parses real numeric
# strings ("189.5") to float.
_NULLISH_FLOAT = {"", "none", "n/a", "na", "null", "nil", "-", "tbd", "unknown"}


def _coerce_optional_float(value):
    """Normalise an LLM-written optional numeric field before validation.

    Three shapes show up in practice: a placeholder string ("None", "N/A") in
    place of an omitted value (#1058); a percentage where a price was asked for
    ("15%", #1288); and a human-formatted price ("$1,234.50"). A percentage
    cannot be salvaged into an absolute level -- reading "15%" as 15 would put a
    stop at $15 on a $600 stock -- so it is dropped like a placeholder, leaving
    one bad field to null out instead of failing the whole proposal. A formatted
    price is reduced to its number.

    Anything that is not a single number is dropped the same way. A range
    ("150-160") or a hedge ("around 150") would otherwise reach pydantic, fail
    validation, and discard the whole decision, losing every field the model got
    right along with the price.
    """
    if not isinstance(value, str):
        return value
    text = value.strip()
    if text.lower() in _NULLISH_FLOAT or text.endswith("%"):
        return None
    cleaned = text.replace(",", "").lstrip("$€£¥").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Shared rating types
# ---------------------------------------------------------------------------


class PortfolioRating(StrEnum):
    """5-tier rating used by the Research Manager and Portfolio Manager."""

    BUY = "Buy"
    OVERWEIGHT = "Overweight"
    HOLD = "Hold"
    UNDERWEIGHT = "Underweight"
    SELL = "Sell"


class TraderAction(StrEnum):
    """3-tier transaction direction used by the Trader.

    The Trader's job is to translate the Research Manager's investment plan
    into a concrete transaction proposal: should the desk execute a Buy, a
    Sell, or sit on Hold this round.  Position sizing and the nuanced
    Overweight / Underweight calls happen later at the Portfolio Manager.
    """

    BUY = "Buy"
    HOLD = "Hold"
    SELL = "Sell"


# ---------------------------------------------------------------------------
# Research Manager
# ---------------------------------------------------------------------------


class ResearchPlan(BaseModel):
    """Structured investment plan produced by the Research Manager.

    Hand-off to the Trader: the recommendation pins the directional view,
    the rationale captures which side of the bull/bear debate carried the
    argument, and the strategic actions translate that into concrete
    instructions the trader can execute against.
    """

    recommendation: PortfolioRating = Field(
        description=(
            "The investment recommendation. Exactly one of Buy / Overweight / "
            "Hold / Underweight / Sell. Conflicting arguments alone are not a "
            "reason to Hold: commit to the stronger side, sized by how "
            "decisively it wins. Choose Hold only when the evidence is still "
            "balanced after weighing, or too thin to support a call."
        ),
    )
    rationale: str = Field(
        description=(
            "Conversational summary of the key points from both sides of the "
            "debate, ending with which arguments led to the recommendation. "
            "Speak naturally, as if to a teammate."
        ),
    )
    strategic_actions: str = Field(
        description=(
            "Concrete steps for the trader to implement the recommendation, "
            "including sizing guidance relative to a standard allocation. The "
            "research team does not see the caller's holdings; the trader and "
            "portfolio manager apply the actual position."
        ),
    )


def render_research_plan(plan: ResearchPlan) -> str:
    """Render a ResearchPlan to markdown for storage and the trader's prompt context."""
    return "\n".join([
        f"**Recommendation**: {plan.recommendation.value}",
        "",
        f"**Rationale**: {plan.rationale}",
        "",
        f"**Strategic Actions**: {plan.strategic_actions}",
    ])


# ---------------------------------------------------------------------------
# Trader
# ---------------------------------------------------------------------------


class TraderProposal(BaseModel):
    """Structured transaction proposal produced by the Trader.

    The trader reads the Research Manager's investment plan and the analyst
    reports, then turns them into a concrete transaction: what action to
    take, the reasoning that justifies it, and the practical levels for
    entry, stop-loss, and sizing.
    """

    action: TraderAction = Field(
        description="The transaction direction. Exactly one of Buy / Hold / Sell.",
    )
    reasoning: str = Field(
        description=(
            "The case for this action, anchored in the analysts' reports and "
            "the research plan. Two to four sentences."
        ),
    )
    entry_price: float | None = Field(
        default=None,
        description=(
            "Optional entry price target as an absolute number in the instrument's "
            "quote currency (e.g. 189.5), never a percentage or a range. Omit it "
            "if you cannot state a specific level."
        ),
    )
    stop_loss: float | None = Field(
        default=None,
        description=(
            "Optional stop-loss as an absolute price in the instrument's quote "
            "currency (e.g. 172.0), never a percentage. Convert a percentage "
            "distance to the price level it implies, or omit it."
        ),
    )
    position_sizing: str | None = Field(
        default=None,
        description="Optional sizing guidance, e.g. '5% of portfolio'.",
    )

    @field_validator("entry_price", "stop_loss", mode="before")
    @classmethod
    def _nullish_float_to_none(cls, v):
        return _coerce_optional_float(v)


def render_trader_proposal(proposal: TraderProposal) -> str:
    """Render a TraderProposal to markdown.

    The trailing ``FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL**`` line is
    preserved for backward compatibility with the analyst stop-signal text
    and any external code that greps for it.
    """
    parts = [
        f"**Action**: {proposal.action.value}",
        "",
        f"**Reasoning**: {proposal.reasoning}",
    ]
    # Named even when absent, so a reader can tell a level the trader chose not
    # to give from one the schema never asked for.
    for label, value in (("Entry Price", proposal.entry_price),
                         ("Stop Loss", proposal.stop_loss),
                         ("Position Sizing", proposal.position_sizing)):
        parts.extend(["", f"**{label}**: {value if value is not None and value != '' else 'not provided'}"])
    parts.extend([
        "",
        f"FINAL TRANSACTION PROPOSAL: **{proposal.action.value.upper()}**",
    ])
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Portfolio Manager
# ---------------------------------------------------------------------------


class PortfolioDecision(BaseModel):
    """Structured output produced by the Portfolio Manager.

    The model fills every field as part of its primary LLM call; no separate
    extraction pass is required. Field descriptions double as the model's
    output instructions, so the prompt body only needs to convey context and
    the rating-scale guidance.
    """

    rating: PortfolioRating = Field(
        description=(
            "The final position rating. Exactly one of Buy / Overweight / Hold / "
            "Underweight / Sell, picked based on the analysts' debate. "
            "Conflicting arguments alone are not a reason to Hold: commit to the "
            "stronger side, sized by how decisively it wins. Choose Hold only "
            "when the evidence is still balanced after weighing, or too thin to "
            "support a call."
        ),
    )
    executive_summary: str = Field(
        description=(
            "A concise action plan covering entry strategy, position sizing, "
            "key risk levels, and time horizon. Two to four sentences."
        ),
    )
    investment_thesis: str = Field(
        description=(
            "Detailed reasoning anchored in specific evidence from the analysts' "
            "debate. If prior lessons are referenced in the prompt context, "
            "incorporate them; otherwise rely solely on the current analysis."
        ),
    )
    price_target: float | None = Field(
        default=None,
        description="Optional target price in the instrument's quote currency.",
    )
    time_horizon: str | None = Field(
        default=None,
        description="Optional recommended holding period, e.g. '3-6 months'.",
    )
    bull_case: str | None = Field(
        default=None,
        description="The bullish scenario: what would have to go right and the evidence for it. One to three sentences.",
    )
    base_case: str | None = Field(
        default=None,
        description="The most likely, neutral scenario and the evidence for it. One to three sentences.",
    )
    bear_case: str | None = Field(
        default=None,
        description="The bearish scenario: what would go wrong and the evidence for it. One to three sentences.",
    )
    key_risks: str | None = Field(
        default=None,
        description=(
            "The main risks to this call, including data the analysis lacked (unavailable, stale "
            "or delayed figures). One to three sentences."
        ),
    )
    confidence: Literal["low", "medium", "high"] | None = Field(
        default=None,
        description="Confidence in the rating given the evidence and its gaps: low, medium or high.",
    )

    @field_validator("price_target", mode="before")
    @classmethod
    def _nullish_float_to_none(cls, v):
        return _coerce_optional_float(v)

    @field_validator("confidence", mode="before")
    @classmethod
    def _confidence_word(cls, v):
        # "Medium" or "MEDIUM" must not fail the whole decision; an unknown word is dropped.
        word = str(v).strip().lower() if v is not None else None
        return word if word in ("low", "medium", "high") else None


def render_pm_decision(decision: PortfolioDecision) -> str:
    """Render a PortfolioDecision back to the markdown shape the rest of the system expects.

    Memory log, CLI display, and saved report files all read this markdown,
    so the rendered output preserves the exact section headers (``**Rating**``,
    ``**Executive Summary**``, ``**Investment Thesis**``) that downstream
    parsers and the report writers already handle.
    """
    parts = [
        f"**Rating**: {decision.rating.value}",
        "",
        f"**Executive Summary**: {decision.executive_summary}",
        "",
        f"**Investment Thesis**: {decision.investment_thesis}",
    ]
    # Named even when absent: a missing line reads as a field nobody asked for,
    # so a reader cannot tell "no target" from "target not reported".
    target = decision.price_target if decision.price_target is not None else "not provided"
    parts.extend(["", f"**Price Target**: {target}"])
    parts.extend(["", f"**Time Horizon**: {decision.time_horizon or 'not provided'}"])
    for label, value in (("Bull Case", decision.bull_case), ("Base Case", decision.base_case),
                         ("Bear Case", decision.bear_case), ("Key Risks", decision.key_risks),
                         ("Confidence", decision.confidence)):
        parts.extend(["", f"**{label}**: {value or 'not provided'}"])
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Intraday (day-trader) mode
# ---------------------------------------------------------------------------
#
# Bound only when decision_horizon is "intraday": the swing schemas, and so what
# a swing run asks for and renders, are untouched. The plan fields are flat, not
# a nested object, so every provider's structured-output mode takes them.

# The plan's labelled lines, in the order they are rendered and asked for. The
# Portfolio Manager's free-text fallback is read back by these labels.
INTRADAY_PLAN_LABELS = {
    "direction": "Direction",
    "entry_price": "Entry Price",
    "stop_loss": "Stop Loss",
    "target_price": "Target Price",
    "exit_by": "Exit By",
    "position_size_shares": "Position Size (shares)",
    "invalidation": "Invalidation",
}

_DIRECTION_WORDS = {
    "long": "long", "buy": "long", "short": "short", "sell": "short",
    "none": "none", "no trade": "none", "flat": "none", "hold": "none", "stand aside": "none",
}
_CLOCK_RE = re.compile(
    r"\s*(?P<h>\d{1,2}):(?P<m>\d{2})(?::\d{2})?\s*(?P<ampm>[ap]\.?m\.?)?\s*(?:[a-z]{2,5})?\s*", re.IGNORECASE)
_RUPEE_PREFIX = re.compile(r"^\s*(?:\u20b9|rs\.?|inr)\s*", re.IGNORECASE)


def plan_price(value):
    """``_coerce_optional_float`` that also reads a rupee price ("\u20b91,405.50", "Rs 1405")."""
    if isinstance(value, str):
        value = _RUPEE_PREFIX.sub("", value)
    return _coerce_optional_float(value)


def intraday_direction(value) -> str | None:
    """long, short or none from the words a model writes for them ("Long", "no-trade", "Sell"); else None.

    Intraday there is no position to exit, so a sell opens a short.
    """
    if value is None:
        return None
    word = " ".join(str(value).strip().lower().replace("_", " ").replace("-", " ").split())
    return _DIRECTION_WORDS.get(word)


def clock_text(value) -> str | None:
    """A time of day as HH:MM (24-hour) from "15:10", "15:10 IST" or "3:10 pm"; None when it is not one."""
    if isinstance(value, time):
        return f"{value:%H:%M}"
    if value is None:
        return None
    match = _CLOCK_RE.fullmatch(str(value))
    if not match:
        return None
    hour, minute = int(match.group("h")), int(match.group("m"))
    if match.group("ampm"):
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if match.group("ampm").lower().startswith("p") else 0)
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def whole_shares(value) -> int | None:
    """A share count from 35, "35" or "1,200 shares"; None for anything else (a fraction, a negative, a range)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).strip().lower().replace(",", "").removesuffix("shares").removesuffix("share").strip()
        try:
            number = float(text)
        except ValueError:
            return None
    if not math.isfinite(number) or number < 0 or number != int(number):
        return None
    return int(number)


_DIRECTION_FIELD = (
    "The paper trade for the session: long (buy, then sell to close), short (sell first, then buy back; "
    "intraday only), or none (no trade)."
)
_TARGET_FIELD = (
    "Optional profit target as an absolute price in the instrument's quote currency, on the profitable "
    "side of the entry. Omit it if you cannot state a level."
)
_EXIT_BY_FIELD = (
    "Optional time to close the position by, as HH:MM in the exchange's time zone, no later than the "
    "exit-by time the prompt gives."
)
_SHARES_FIELD = (
    "Optional whole number of shares, taken from the intraday snapshot's paper sizing for the chosen "
    "stop. Omit it rather than estimate one."
)
_INVALIDATION_FIELD = (
    "Optional condition that cancels the plan, before or after the entry: a level broken, a time "
    "passed, a session the plan should not trade. One sentence."
)


class IntradayTraderProposal(TraderProposal):
    """The Trader's proposal as a paper plan for one trading session."""

    direction: Literal["long", "short", "none"] | None = Field(default=None, description=_DIRECTION_FIELD)
    target_price: float | None = Field(default=None, description=_TARGET_FIELD)
    exit_by: str | None = Field(default=None, description=_EXIT_BY_FIELD)
    position_size_shares: int | None = Field(default=None, description=_SHARES_FIELD)
    invalidation: str | None = Field(default=None, description=_INVALIDATION_FIELD)

    @field_validator("direction", mode="before")
    @classmethod
    def _direction_word(cls, v):
        return intraday_direction(v)

    # Named as the parent's, so it replaces it for the prices of an intraday plan.
    @field_validator("entry_price", "stop_loss", "target_price", mode="before")
    @classmethod
    def _nullish_float_to_none(cls, v):
        return plan_price(v)

    @field_validator("exit_by", mode="before")
    @classmethod
    def _exit_clock(cls, v):
        return clock_text(v)

    @field_validator("position_size_shares", mode="before")
    @classmethod
    def _shares(cls, v):
        return whole_shares(v)


class IntradayPortfolioDecision(PortfolioDecision):
    """The Portfolio Manager's decision with its paper plan for one trading session."""

    price_target: float | None = Field(
        default=None,
        description="Leave empty in an intraday plan: its profit target is target_price.",
    )
    direction: Literal["long", "short", "none"] | None = Field(default=None, description=_DIRECTION_FIELD)
    entry_price: float | None = Field(
        default=None,
        description=(
            "Optional entry trigger as an absolute price in the instrument's quote currency (e.g. 1405.5), "
            "never a percentage or a range."
        ),
    )
    stop_loss: float | None = Field(
        default=None,
        description="Optional stop-loss as an absolute price in the instrument's quote currency, never a percentage.",
    )
    target_price: float | None = Field(default=None, description=_TARGET_FIELD)
    exit_by: str | None = Field(default=None, description=_EXIT_BY_FIELD)
    position_size_shares: int | None = Field(default=None, description=_SHARES_FIELD)
    invalidation: str | None = Field(default=None, description=_INVALIDATION_FIELD)

    @field_validator("direction", mode="before")
    @classmethod
    def _direction_word(cls, v):
        return intraday_direction(v)

    # Named as the parent's, so it replaces it for the prices of an intraday plan.
    @field_validator("price_target", "entry_price", "stop_loss", "target_price", mode="before")
    @classmethod
    def _nullish_float_to_none(cls, v):
        return plan_price(v)

    @field_validator("exit_by", mode="before")
    @classmethod
    def _exit_clock(cls, v):
        return clock_text(v)

    @field_validator("position_size_shares", mode="before")
    @classmethod
    def _shares(cls, v):
        return whole_shares(v)


def intraday_plan_fields(source) -> dict:
    """The plan fields of an intraday Trader proposal or Portfolio Manager decision, as a dict.

    A decision whose target came only as ``price_target`` keeps it as the target.
    """
    fields = {key: getattr(source, key, None) for key in INTRADAY_PLAN_LABELS}
    if fields["target_price"] is None:
        fields["target_price"] = getattr(source, "price_target", None)
    return fields


def _plan_lines(fields: dict) -> list[str]:
    """The plan's labelled lines, each named even when absent."""
    lines = []
    for key, label in INTRADAY_PLAN_LABELS.items():
        value = fields.get(key)
        lines.extend(["", f"**{label}**: {value if value is not None and value != '' else 'not provided'}"])
    return lines


def plan_checks_line(notes) -> str | None:
    """The decision's line on what the stored plan changed or noted ("**Plan Checks**: ..."); None without notes."""
    return f"**Plan Checks**: {'; '.join(notes)}." if notes else None


def render_intraday_trader_proposal(proposal: IntradayTraderProposal) -> str:
    """Render an intraday proposal: the swing proposal's lines, the plan's, then the closing proposal line."""
    parts = [
        f"**Action**: {proposal.action.value}",
        "",
        f"**Reasoning**: {proposal.reasoning}",
        *_plan_lines(intraday_plan_fields(proposal)),
        "",
        f"**Position Sizing**: {proposal.position_sizing or 'not provided'}",
        "",
        f"FINAL TRANSACTION PROPOSAL: **{proposal.action.value.upper()}**",
    ]
    return "\n".join(parts)


def render_intraday_pm_decision(decision: IntradayPortfolioDecision, session: str | None = None,
                                plan: dict | None = None) -> str:
    """Render an intraday decision: the rating first, as in swing, then the paper plan and the scenarios.

    ``session`` names the session the plan is for, from the run; the plan's
    target stands in for the swing price target. ``plan`` is the plan as stored
    and scored (``intraday_plan.plan_from_fields``): the report then shows the
    exit-by that is scored, and the plan's notes as a "Plan Checks" line.
    """
    parts = [
        f"**Rating**: {decision.rating.value}",
        "",
        f"**Executive Summary**: {decision.executive_summary}",
        "",
        f"**Investment Thesis**: {decision.investment_thesis}",
    ]
    if session:
        parts.extend(["", f"**Plan Session**: {session}"])
    parts.extend(_plan_lines(plan if plan is not None else intraday_plan_fields(decision)))
    checks = plan_checks_line((plan or {}).get("notes"))
    if checks:
        parts.extend(["", checks])
    parts.extend(["", f"**Time Horizon**: {decision.time_horizon or 'intraday, one session'}"])
    for label, value in (("Bull Case", decision.bull_case), ("Base Case", decision.base_case),
                         ("Bear Case", decision.bear_case), ("Key Risks", decision.key_risks),
                         ("Confidence", decision.confidence)):
        parts.extend(["", f"**{label}**: {value or 'not provided'}"])
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Sentiment Analyst
# ---------------------------------------------------------------------------


class SentimentBand(StrEnum):
    """Discrete sentiment direction produced by the Sentiment Analyst.

    Six tiers keep the signal granular enough to be actionable while remaining
    small enough for every provider to map reliably from its JSON output.
    """

    BULLISH = "Bullish"
    MILDLY_BULLISH = "Mildly Bullish"
    NEUTRAL = "Neutral"
    MIXED = "Mixed"
    MILDLY_BEARISH = "Mildly Bearish"
    BEARISH = "Bearish"


class SentimentReport(BaseModel):
    """Structured sentiment report produced by the Sentiment Analyst.

    Replaces the previous free-form prose output so downstream consumers
    (dashboards, audit logs, PDF renderers, other agents) can read
    ``overall_band`` and ``overall_score`` without maintaining fragile regex
    fallbacks that drift with every model release. ``narrative`` preserves the
    rich source-by-source analysis; ``render_sentiment_report`` prepends a
    deterministic header so the saved report stays human-readable.
    """

    overall_band: SentimentBand = Field(
        description=(
            "Overall sentiment direction. Exactly one of: "
            "Bullish / Mildly Bullish / Neutral / Mixed / Mildly Bearish / Bearish. "
            "Use Mixed when sources point in clearly different directions. "
            "Use Neutral only when all sources are genuinely silent or non-committal."
        ),
    )
    overall_score: float = Field(
        ge=0.0,
        le=10.0,
        description=(
            "Numeric sentiment intensity on a 0–10 scale. "
            "0 = maximally bearish, 5 = neutral, 10 = maximally bullish. "
            "Guideline for consistency with overall_band: "
            "Bullish ~6.5–10, Mildly Bullish ~5.5–6.4, Neutral/Mixed ~4.5–5.5, "
            "Mildly Bearish ~3.5–4.4, Bearish ~0–3.4. "
            "Only the 0–10 bounds are enforced."
        ),
    )
    confidence: Literal["low", "medium", "high"] = Field(
        description=(
            "Confidence in the assessment based on data quality and sample size. "
            "Use 'low' when one or more sources returned a placeholder or fewer "
            "than 5 data points; 'medium' when data is present but sparse; "
            "'high' when all three sources returned substantive data."
        ),
    )
    narrative: str = Field(
        description=(
            "Full sentiment report covering, in order: "
            "(1) source-by-source breakdown with specific evidence (cite message "
            "counts, ratios, notable posts); "
            "(2) cross-source divergences and alignments; "
            "(3) dominant narrative themes; "
            "(4) catalysts and risks surfaced by the data; "
            "(5) a markdown table summarising key sentiment signals, their "
            "direction, source, and supporting evidence. "
            "Keep it informative and substantive: develop each section thoroughly "
            "with concrete evidence so every point adds new signal for the trader."
        ),
    )


def render_sentiment_report(report: SentimentReport) -> str:
    """Render a SentimentReport to the markdown shape the rest of the system expects.

    The structured header (band + score + confidence) is prepended to the
    narrative so the saved report is both human-readable and machine-parseable
    without regex.
    """
    return "\n".join([
        f"**Overall Sentiment:** **{report.overall_band.value}** "
        f"(Score: {report.overall_score:.1f}/10)",
        f"**Confidence:** {report.confidence.capitalize()}",
        "",
        report.narrative,
    ])
