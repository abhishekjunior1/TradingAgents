"""Intraday (day-trader) mode for the decision agents: what they are told about the session, and the plan as data.

The run fixes the session the plan is for, its exit-by time and the price the
plan is read against when it starts (``intraday_context`` in the state, from the
data layer's ``plan_session``). The Trader and Portfolio Manager are told them;
the Portfolio Manager's plan fields are kept with them (``intraday_plan``) for
the memory log, which scores the plan on that session's bars once it has closed.
Plans are paper only: nothing here places or formats an order.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from tradingagents.agents.schemas import (
    INTRADAY_PLAN_LABELS,
    clock_text,
    intraday_direction,
    plan_price,
    whole_shares,
)
from tradingagents.dataflows.vendors.yahoo.common import price_text
from tradingagents.markets.exchanges import exchange_for_symbol
from tradingagents.markets.intraday import intraday_cost_profile, intraday_position
from tradingagents.markets.intraday_eval import plan_problem

# For a run in another language: the plan's labelled lines keep their English
# labels, as the rating line does, and their values the forms they are read in,
# since the Portfolio Manager's are read back by them.
PLAN_LABELS_NOTE = (
    " Keep the labels of the plan's lines (" + ", ".join(f"**{label}**" for label in INTRADAY_PLAN_LABELS.values())
    + ") in English, and write their values as they are read back: the direction as long, short or none, "
    "prices as plain numbers with a decimal point, the share count as a whole number, and the exit-by time as "
    "HH:MM on a 24-hour clock."
)


def _now() -> datetime:
    return datetime.now(UTC)


def _session_day(context: Mapping) -> date | None:
    try:
        return date.fromisoformat(str(context.get("session")))
    except ValueError:
        return None


def session_line(context: Mapping | None) -> str:
    """The session the plan is for and when it must be closed, as one sentence for a prompt; empty without a context."""
    if not context:
        return ""
    day = _session_day(context)
    if day is None:
        return "Intraday mode (paper only): the session the plan is for could not be worked out for this run."
    line = f"Intraday mode (paper only, no order is placed): the plan is for {day:%a %d %b %Y}, {context.get('why')}."
    exit_by = context.get("exit_by")
    if exit_by:
        when = f"{exit_by} {context.get('tz_label') or ''}".strip()
        line += (f" Exit by {when} at the latest, the configured broker square-off (intraday_square_off): "
                 "intraday (MIS) positions are squared off the same day, and short selling is allowed intraday only.")
    else:
        line += " Exit by the session close; no square-off time is configured for this listing."
    return line


def session_brief(context: Mapping | None) -> str:
    """``session_line`` plus the price the plan's entry is read against, for the Trader and Portfolio Manager."""
    line = session_line(context)
    if not line or _session_day(context) is None:
        return line
    reference = context.get("reference_price")
    if reference is None:
        why = f" ({context['note']})" if context.get("note") else ""
        return (f"{line} The latest intraday price is unavailable{why}: the plan's entry is read against the "
                "session's first price.")
    through = context.get("reference_through")
    when = ""
    if through:
        moment = datetime.fromisoformat(through)
        clock = f"{moment:%H:%M} {context.get('tz_label') or ''}".strip()
        when = f" (through {clock} on {moment:%a %d %b %Y})"
    return (f"{line} The plan is read against {price_text(float(reference))}, the close of the newest complete "
            f"{context.get('interval') or '5m'} bar{when} from {context.get('source') or 'the intraday bars'}: an "
            "entry above it is a buy stop (breakout) for a long and a sell limit for a short; an entry below it "
            "is a buy limit (pullback) for a long and a sell stop (breakdown) for a short.")


def _zone(context: Mapping):
    try:
        return ZoneInfo(str(context.get("timezone") or "UTC"))
    except (KeyError, ValueError):
        return UTC


def _valid_from(context: Mapping, decided_at: datetime) -> str | None:
    """When a plan decided at ``decided_at`` starts to count: then, if the run is not past-dated and that
    is on or after its session's day on the listing's clock; else None, the whole session. A past-dated
    run (``past_dated``) plans as of its date's close, whenever it is decided."""
    day = _session_day(context)
    if day is None or context.get("past_dated"):
        return None
    if decided_at.astimezone(_zone(context)).date() < day:
        return None
    return decided_at.astimezone(UTC).isoformat()


def _no_bar_before(exit_by: str, context: Mapping, valid_from: str | None) -> str | None:
    """Why an exit-by of ``exit_by`` (HH:MM) leaves the plan no bar to trade, or None: it is not after the
    session's open (``opens``), or not after the moment a plan made during its session was made."""
    opens = context.get("opens")
    if opens and exit_by <= opens:
        return f"is not after the session's {opens} open"
    if valid_from:
        made = datetime.fromisoformat(valid_from).astimezone(_zone(context))
        if made.date() == _session_day(context) and exit_by <= f"{made:%H:%M}":
            return f"is not after {made:%H:%M}, when the plan was made"
    return None


# The direction each rating calls for; Underweight and Sell may also stand aside.
_RATING_DIRECTIONS = {"Buy": ("long",), "Overweight": ("long",), "Hold": ("none",),
                      "Underweight": ("short", "none"), "Sell": ("short", "none")}


def _size_note(plan: Mapping, context: Mapping, sizing: Mapping) -> str | None:
    """A note when the plan's share count is more than its stop and the paper capital allow; else None.

    The count is the one ``intraday_position`` gives the snapshot's sizing:
    risking ``risk_pct`` of the capital, charges included (the configured
    intraday charges, named in ``sizing`` as in the config), under the leverage cap.
    """
    shares, entry, stop = plan.get("position_size_shares"), plan.get("entry_price"), plan.get("stop_loss")
    if not shares or entry is None or stop is None:
        return None
    exchange = exchange_for_symbol(context.get("symbol"))
    try:
        sized = intraday_position(float(sizing["capital"]), float(sizing["risk_pct"]), entry, stop,
                                  leverage_cap=float(sizing["leverage_cap"]),
                                  profile=intraday_cost_profile(exchange, sizing),
                                  venue=exchange.code if exchange is not None else None)
    except (KeyError, TypeError, ValueError):
        return None
    if sized is None or shares <= sized["shares"]:
        return None
    return (f"the plan's {shares:,} shares are more than the {sized['shares']:,} that risking "
            f"{sized['risk_pct']:g}% of the {sized['capital']:,.0f} paper capital with this stop allows "
            f"(leverage cap {sized['leverage_cap']:g}x)")


def plan_from_fields(fields: Mapping, context: Mapping | None, decided_at: datetime | None = None,
                     rating: str | None = None, sizing: Mapping | None = None) -> dict:
    """The Portfolio Manager's plan as stored: its fields, the session, the reference price, and notes.

    The exit-by time is the earlier of the plan's and the configured square-off,
    since a broker closes intraday positions then; a note says when it was moved.
    One that leaves the plan no bar to trade, not after the session's open or
    not after the moment a plan made during its session was made ("3:55" meant
    as 3:55 pm reads 03:55), is replaced by the square-off, or the session
    close, with a note. When the square-off itself leaves no bar (the plan was
    made after it), the note says the plan cannot trade. A free-text line given
    but not readable (``unread`` in ``fields``, from ``parse_plan_lines``) is
    left out, with a note.
    The plan is otherwise kept as given: a direction the ``rating`` does not
    call for, a long or short the replay cannot score (no entry, or a stop or
    target on the wrong side of it), or more shares than ``sizing`` (capital,
    risk_pct, leverage_cap) allows for its stop, is noted, not corrected.
    """
    context = context or {}
    plan = {key: fields.get(key) for key in INTRADAY_PLAN_LABELS}
    unread = fields.get("unread") or ()
    notes = [f"the {label} line could not be read, so it is left out"
             for key, label in INTRADAY_PLAN_LABELS.items() if key in unread and key != "direction"]
    valid_from = _valid_from(context, decided_at or _now())
    square_off = context.get("exit_by")
    # The replay's own test, so the decision says now what the score will.
    problem = (plan_problem(plan["direction"], plan["entry_price"], plan["stop_loss"], plan["target_price"])
               if plan["direction"] in ("long", "short") else None)
    # A square-off that leaves no bar (the plan was made after it) leaves none for any exit-by.
    closed = _no_bar_before(square_off, context, valid_from) if square_off else None
    no_bar = _no_bar_before(plan["exit_by"], context, valid_from) if plan["exit_by"] else None
    if closed:
        if plan["direction"] in ("long", "short"):
            notes.append(f"the {square_off} square-off {closed}, so the plan cannot trade"
                         + ("" if problem else " and is scored as not triggered"))
        plan["exit_by"] = square_off
    elif plan["exit_by"] and square_off and plan["exit_by"] > square_off:
        notes.append(f"the plan's exit-by {plan['exit_by']} is after the {square_off} square-off, "
                     f"so it is scored to {square_off}")
        plan["exit_by"] = square_off
    elif no_bar:
        notes.append(f"the plan's exit-by {plan['exit_by']} {no_bar}, so it is scored to "
                     f"{square_off or 'the session close'}")
        plan["exit_by"] = square_off
    elif not plan["exit_by"] and square_off:
        plan["exit_by"] = square_off
    if plan["direction"] is None:
        notes.append("the Direction line could not be read, so it is scored as no trade" if "direction" in unread
                     else "the decision gave no direction, so it is scored as no trade")
    elif rating in _RATING_DIRECTIONS and plan["direction"] not in _RATING_DIRECTIONS[rating]:
        notes.append(f"the direction {plan['direction']} does not follow the {rating} rating; "
                     "the plan is scored as given")
    if problem:
        notes.append(f"{problem}, so it is scored as no trade")
    size_note = _size_note(plan, context, sizing) if sizing and not problem else None
    if size_note:
        notes.append(size_note)
    plan.update(
        session=context.get("session"),
        reference_price=context.get("reference_price"),
        valid_from=valid_from,
        timezone=context.get("timezone"),
        notes=notes,
    )
    return plan


_LABEL_LINE = re.compile(r"^\s*(?:[-*+]\s+)?\**\s*(?P<label>[A-Za-z ()]+?)\s*\**\s*[:\uff1a]\s*\**\s*(?P<value>.*?)\s*$")
_NOT_GIVEN = {"", "not provided", "n/a", "na", "none given", "-"}
# A remark in brackets after a value ("96.60 (2R)", "Short (intraday only)"), read past.
_REMARK = re.compile(r"\s*\([^()]*\)$")


def parse_plan_lines(text: str) -> dict:
    """The plan fields from a free-text decision's labelled lines ("**Entry Price**: 1405.5").

    A value is read without a remark in brackets after it. A line that is
    missing, says "not provided", or does not read as its kind of value gives
    None; a later line with the same label replaces an earlier one. A line
    meant as a value that cannot be read (a direction word, or a number, that
    is not one the plan takes) is listed by key under ``unread``, for the
    plan's notes.
    """
    by_label = {label.lower(): key for key, label in INTRADAY_PLAN_LABELS.items()}
    raw: dict[str, str] = {}
    for line in (text or "").splitlines():
        match = _LABEL_LINE.match(line)
        if match and match.group("label").strip().lower() in by_label:
            raw[by_label[match.group("label").strip().lower()]] = match.group("value").strip().strip("*").strip()
    fields: dict = dict.fromkeys(INTRADAY_PLAN_LABELS)
    unread = []
    for key, value in raw.items():
        given = _REMARK.sub("", value).strip().strip("*").strip()
        if given.lower() in _NOT_GIVEN:
            continue
        if key == "direction":
            fields[key] = intraday_direction(given)
        elif key == "exit_by":
            fields[key] = clock_text(given)
        elif key == "position_size_shares":
            fields[key] = whole_shares(given)
        elif key == "invalidation":
            fields[key] = value
        else:
            fields[key] = plan_price(given)
        # "TBD" or "none" for a price is a value left out; "about 96.60" or "15h10" is one not read.
        if fields[key] is None and (key == "direction" or any(c.isdigit() for c in given)):
            unread.append(key)
    fields["unread"] = unread
    return fields
