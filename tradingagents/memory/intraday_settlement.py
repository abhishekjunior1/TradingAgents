"""Settling an intraday paper plan: replayed on its session's 5m bars once that session's close is final.

An intraday entry is never scored by the holding-period return. Its plan waits
while its session's close is not final (NSE/BSE by the exchange's calendar,
elsewhere until the bars' own day is over), then ``markets.intraday_eval``
replays it on that session's bars and the model reflects on what it did. A
session older than Yahoo serves 5m bars for is recorded as not scored ("outside
Yahoo's intraday window"), as is one whose bars are missing; a fetch that fails
leaves the plan pending for the next run.
"""

import logging
from datetime import UTC, date, datetime

import pandas as pd

from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.symbols import normalize_symbol
from tradingagents.dataflows.vendors.yahoo.common import price_text
from tradingagents.dataflows.vendors.yahoo.intraday import (
    SOURCE,
    IntradayWindowError,
    load_intraday_with_meta,
)
from tradingagents.markets.exchanges import exchange_for_symbol, session_status
from tradingagents.markets.intraday import intraday_cost_profile
from tradingagents.markets.intraday_eval import IntradayOutcome, IntradayPlan, evaluate_plan

logger = logging.getLogger(__name__)

INTERVAL = "5m"
OUTSIDE_WINDOW = "outside Yahoo's intraday window"


def _now() -> datetime:
    return datetime.now(UTC)


def stored_plan(entry: dict) -> IntradayPlan:
    """The entry's plan as ``intraday_eval`` reads it; raises ValueError (or TypeError) for one it cannot read."""
    stored = entry.get("intraday_plan") or {}
    return IntradayPlan.from_dict({
        "direction": stored.get("direction") or "none",
        "entry": stored.get("entry_price"),
        "stop": stored.get("stop_loss"),
        "target": stored.get("target_price"),
        "exit_by": stored.get("exit_by"),
        "shares": stored.get("position_size_shares"),
        "session": stored.get("session"),
        "reference_price": stored.get("reference_price"),
        "valid_from": stored.get("valid_from"),
    })


def session_closed(ticker: str, session: date, now: datetime) -> bool:
    """Whether ``session``'s close can be final by ``now``: on NSE/BSE once the exchange says so (16:00 IST);
    elsewhere once the session's date is over in UTC, after which the bars' own clock decides."""
    exchange = exchange_for_symbol(normalize_symbol(ticker))
    if exchange is not None:
        return session_status(exchange, now).last_complete_session >= session
    return now.astimezone(UTC).date() > session


def _session_range(bars: pd.DataFrame | None, session: date) -> str:
    """The session's open, high, low and close from its bars, as one sentence; empty without bars."""
    if bars is None or not len(bars):
        return ""
    day = bars[pd.to_datetime(bars["Datetime"]).dt.date == session]
    if not len(day):
        return ""
    return (f"The session traded: open {price_text(float(day['Open'].iloc[0]))}, high "
            f"{price_text(float(day['High'].max()))}, low {price_text(float(day['Low'].min()))}, close "
            f"{price_text(float(day['Close'].iloc[-1]))} ({SOURCE} {INTERVAL} bars).")


def _price(value) -> str:
    return price_text(float(value)) if value is not None else "not given"


def summarize(plan: IntradayPlan, outcome: IntradayOutcome, bars: pd.DataFrame | None, notes=()) -> str:
    """The plan, what its replay did and the session's range, for the reflection prompt."""
    size = f"{plan.shares:,} shares" if plan.shares else "no share count (scored per share)"
    exit_by = f"exit by {plan.exit_by:%H:%M}" if plan.exit_by else "exit at the session close"
    lines = [f"Plan for {plan.session:%a %d %b %Y}: {plan.direction}, entry {_price(plan.entry)}, stop "
             f"{_price(plan.stop)}, target {_price(plan.target)}, {exit_by}, {size}; read against "
             f"{_price(plan.reference_price)} when it was made."]
    if not outcome.triggered:
        lines.append(f"Replay on the session's {INTERVAL} bars: no trade ({outcome.note or outcome.exit_reason}).")
    else:
        lines.append(
            f"Replay on the session's {INTERVAL} bars: filled at {_price(outcome.entry_price)} at "
            f"{outcome.entry_time:%H:%M} ({outcome.entry_type} entry), closed at {_price(outcome.exit_price)} at "
            f"{outcome.exit_time:%H:%M} ({outcome.exit_reason.replace('_', ' ')}). Gross P&L "
            f"{outcome.gross_pnl:,.2f}, estimated charges {outcome.charges:,.2f}, net P&L {outcome.net_pnl:,.2f} "
            f"({outcome.net_return_pct:+.2f}% of the position at entry)"
            + (f", {outcome.r_multiple:+.2f}R." if outcome.r_multiple is not None else "; no stop, so no R multiple.")
        )
    session_range = _session_range(bars, plan.session)
    if session_range:
        lines.append(session_range)
    if notes:
        lines.append("Notes: " + "; ".join(notes) + ".")
    return "\n".join(lines)


def _not_scored(ticker: str, entry: dict, memory_log, reason: str, resolved: str,
                outcome: dict | None = None) -> tuple[str, str | None]:
    """Settle an entry that cannot be scored, with the reason and no reflection call."""
    record = {**(outcome or {}), "scored": False, "note": reason}
    if memory_log.update_with_intraday_outcome(ticker, entry["date"], record, f"Not scored: {reason}.",
                                               resolution_date=resolved):
        return "settled", None
    return "waiting", None


def settle(ticker: str, entry: dict, memory_log, reflector, now: datetime | None = None,
           config: dict | None = None) -> tuple[str, str | None]:
    """Settle one pending intraday entry: ("settled", None), ("waiting", None) when its session is not
    final yet, or ("failed", reason) when its bars or its reflection could not be had. The charges
    are ``config``'s intraday ones (``intraday_cost_profile``, ``intraday_cost_overrides``)."""
    now = now or _now()
    try:
        plan = stored_plan(entry)
    except (TypeError, ValueError) as exc:
        return _not_scored(ticker, entry, memory_log, f"the stored plan could not be read ({exc})", entry["date"])
    if not session_closed(ticker, plan.session, now):
        return "waiting", None
    try:
        bars, meta = load_intraday_with_meta(ticker, plan.session.isoformat(), INTERVAL, sessions=1, now=now)
    except IntradayWindowError:
        return _not_scored(ticker, entry, memory_log, OUTSIDE_WINDOW, plan.session.isoformat())
    except NoMarketDataError as exc:
        # Yahoo had no bars to serve: asked again next run, until the session leaves its window.
        logger.info("No %s bars yet for %s on %s: %s", INTERVAL, ticker, plan.session, exc)
        return "waiting", None
    except Exception as exc:  # noqa: BLE001 — a failed fetch leaves the plan pending for the next run
        logger.warning("Intraday bars for settling %s on %s were unavailable: %s", ticker, entry["date"], exc)
        return "failed", f"intraday bars unavailable: {exc}"
    if meta.live:
        return "waiting", None

    exchange = exchange_for_symbol(normalize_symbol(ticker))
    outcome = evaluate_plan(plan, bars, intraday_cost_profile(exchange, config),
                            exchange.code if exchange is not None else None, interval=INTERVAL)
    if outcome.exit_reason == "no_data":
        return _not_scored(ticker, entry, memory_log, outcome.note, plan.session.isoformat(), outcome.to_dict())
    notes = list((entry.get("intraday_plan") or {}).get("notes") or [])
    try:
        reflection = reflector.reflect_on_intraday_plan(entry.get("decision", ""),
                                                        summarize(plan, outcome, bars, notes))
    except Exception as exc:  # noqa: BLE001 — reflection calls a provider; the plan waits for the next pass
        logger.warning("Reflection failed for %s on %s: %s", ticker, entry["date"], exc)
        return "failed", f"reflection failed: {exc}"
    record = {**outcome.to_dict(), "scored": True, "interval": INTERVAL}
    if memory_log.update_with_intraday_outcome(ticker, entry["date"], record, reflection,
                                               resolution_date=plan.session.isoformat()):
        return "settled", None
    return "waiting", None
