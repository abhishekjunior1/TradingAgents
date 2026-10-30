"""Scoring an intraday paper plan on the bars of the session it was for.

Pure: the caller hands over that session's bars once its close is final. A bar
hides the order of the prices inside it, so every assumption leans against the
plan: an order fills only when a bar reaches its price (at the bar's open when
the bar opens beyond it), the bar that fills the entry is checked for the stop
and then for the target by its close only (the one price sure to come after the
fill), a bar that opens between the stop and the target and reaches both counts
as stopped, and a stop gapped through exits at the bar's open. Charges come from
markets/costs.py, as the caller configures them (``intraday_cost_profile``).

Times are the exchange's: ``exit_by`` is compared with each bar's local start
and end, so a bar it falls inside is the last one used.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time

import pandas as pd

from tradingagents.markets.intraday import _minutes, trade_charges

DIRECTIONS = ("long", "short", "none")
EXIT_REASONS = ("target", "stop", "exit_by", "session_close", "not_triggered", "no_plan", "no_data")


@dataclass(frozen=True)
class IntradayPlan:
    """A paper plan for one session. Prices are in the listing's currency."""

    direction: str                       # "long", "short" or "none" (no trade)
    entry: float | None
    stop: float | None
    target: float | None
    exit_by: time | None                 # exchange-local; None holds to the session close
    shares: int | None                   # None scores one share and reads returns in percent
    session: date                        # the session the plan is for
    reference_price: float | None        # the price when the plan was made; sets the entry order's type
    valid_from: datetime | None = None   # bars starting earlier are ignored (a plan made during its session)

    def to_dict(self) -> dict:
        """A JSON-ready form, read back by ``from_dict``."""
        return {
            "direction": self.direction, "entry": self.entry, "stop": self.stop, "target": self.target,
            "exit_by": f"{self.exit_by:%H:%M}" if self.exit_by else None, "shares": self.shares,
            "session": self.session.isoformat(), "reference_price": self.reference_price,
            "valid_from": self.valid_from.isoformat() if self.valid_from else None,
        }

    @classmethod
    def from_dict(cls, data: Mapping) -> IntradayPlan:
        """The plan ``to_dict`` wrote; numbers may be strings, and a missing one is None."""
        direction = str(data.get("direction") or "none").strip().lower()
        if direction not in DIRECTIONS:
            raise ValueError(f"direction must be one of {', '.join(DIRECTIONS)}, got {data.get('direction')!r}")
        session, exit_by, valid_from = data.get("session"), data.get("exit_by"), data.get("valid_from")
        if not session:
            raise ValueError("the plan names no session")
        shares = _number(data.get("shares"))
        return cls(
            direction=direction,
            entry=_number(data.get("entry")),
            stop=_number(data.get("stop")),
            target=_number(data.get("target")),
            exit_by=None if not exit_by else exit_by if isinstance(exit_by, time) else time.fromisoformat(str(exit_by)),
            shares=int(shares) if shares is not None else None,
            session=session if isinstance(session, date) else date.fromisoformat(str(session)),
            reference_price=_number(data.get("reference_price")),
            valid_from=(None if not valid_from else valid_from if isinstance(valid_from, datetime)
                        else datetime.fromisoformat(str(valid_from))),
        )


@dataclass(frozen=True)
class IntradayOutcome:
    """What the plan would have done. Money figures are for ``shares`` shares, or per share when it is None."""

    triggered: bool
    entry_time: datetime | None          # start of the bar that filled the entry
    entry_price: float | None
    exit_time: datetime | None           # start of the bar the position was closed in
    exit_price: float | None
    exit_reason: str                     # one of EXIT_REASONS
    gross_pnl: float | None
    charges: float | None
    net_pnl: float | None
    net_return_pct: float | None         # net P&L as a percentage of the position's value at entry
    r_multiple: float | None             # net P&L over the money risked: entry to stop, or fill to stop if more
    shares: int | None = None
    entry_type: str | None = None        # "stop" (breakout / breakdown) or "limit" (pullback)
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "triggered": self.triggered,
            "entry_time": self.entry_time.isoformat() if self.entry_time else None,
            "entry_price": self.entry_price,
            "exit_time": self.exit_time.isoformat() if self.exit_time else None,
            "exit_price": self.exit_price, "exit_reason": self.exit_reason, "gross_pnl": self.gross_pnl,
            "charges": self.charges, "net_pnl": self.net_pnl, "net_return_pct": self.net_return_pct,
            "r_multiple": self.r_multiple, "shares": self.shares, "entry_type": self.entry_type, "note": self.note,
        }


def _number(value) -> float | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def plan_problem(direction: str | None, entry: float | None, stop: float | None,
                 target: float | None) -> str | None:
    """Why a plan with these levels cannot be scored as a trade, or None."""
    if direction not in ("long", "short"):
        return "no trade was planned"
    if entry is None or entry <= 0:
        return "the plan gives no entry price"
    long = direction == "long"
    if stop is not None and (stop >= entry if long else stop <= entry):
        return f"the stop is not {'below' if long else 'above'} the entry for a {direction}"
    if target is not None and (target <= entry if long else target >= entry):
        return f"the target is not {'above' if long else 'below'} the entry for a {direction}"
    return None


def _session_bars(bars: pd.DataFrame | None, session: date) -> pd.DataFrame:
    if bars is None or len(bars) == 0 or not {"Datetime", "Open", "High", "Low", "Close"} <= set(bars.columns):
        return pd.DataFrame(columns=["Datetime", "Open", "High", "Low", "Close"])
    frame = bars.copy()
    frame["Datetime"] = pd.to_datetime(frame["Datetime"])
    for column in ("Open", "High", "Low", "Close"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["Datetime", "Open", "High", "Low", "Close"])
    frame = frame[frame["Datetime"].dt.date == session]
    return frame.sort_values("Datetime").reset_index(drop=True)


def _aligned(moment: datetime, like: pd.Timestamp) -> pd.Timestamp:
    """``moment`` comparable with the bars' timestamps: a naive one is read on the bars' clock."""
    stamp = pd.Timestamp(moment)
    if like.tzinfo is None:
        return stamp.tz_localize(None) if stamp.tzinfo is not None else stamp
    return stamp.tz_localize(like.tzinfo) if stamp.tzinfo is None else stamp.tz_convert(like.tzinfo)


def _fill(bar, entry: float, stop_order: bool, long: bool) -> float | None:
    """The entry's fill price in ``bar``, or None when the bar does not reach it."""
    rising = long == stop_order        # a buy stop or a sell limit fills as the price rises to it
    if rising:
        if bar.Open >= entry:
            return float(bar.Open)
        return entry if bar.High >= entry else None
    if bar.Open <= entry:
        return float(bar.Open)
    return entry if bar.Low <= entry else None


def _reaches(price: float, level: float, long: bool, against: bool) -> bool:
    """Whether ``price`` is at or beyond ``level``: below it for a long's stop, above it for its target."""
    return price <= level if long == against else price >= level


def _no_trade(reason: str, note: str, shares: int | None, entry_type: str | None = None) -> IntradayOutcome:
    return IntradayOutcome(False, None, None, None, None, reason, None, None, None, None, None,
                           shares, entry_type, note)


def evaluate_plan(plan: IntradayPlan, bars: pd.DataFrame | None, profile=None,
                  venue: str | None = None, interval: str | None = None) -> IntradayOutcome:
    """Score ``plan`` on its session's bars (columns Datetime, Open, High, Low, Close).

    The entry's order type follows from ``reference_price``: an entry above it
    is a buy stop for a long (a breakout) and a sell limit for a short; below it,
    a buy limit for a long (a pullback) and a sell stop for a short. An entry
    equal to it is taken as a stop order, which never fills at a better price
    than the entry; with no reference price, the session's first open stands in.
    An entry filled at an open already beyond the target exits there at once,
    and one whose bar closes at or beyond the target exits at the target in that
    bar. Once filled, later bars are checked bar by bar: a bar opening beyond the stop
    or the target exits at its open; otherwise a bar reaching both the stop and
    the target is stopped, and the target exits at its price. The position is closed at the open of the bar ``exit_by`` falls in, the
    first one ending after it (no entry fills from then on), or at the last
    bar's close. ``interval`` is the bars' length ("5m"); without it, the usual
    gap between them. ``profile`` is a ``CostProfile`` or a name. R is in the
    risk the plan was sized on (entry to stop), or the fill's when that is more.
    """
    shares = plan.shares if plan.shares and plan.shares > 0 else None
    problem = plan_problem(plan.direction, plan.entry, plan.stop, plan.target)
    if problem:
        return _no_trade("no_plan", problem, shares)
    session = _session_bars(bars, plan.session)
    if session.empty:
        return _no_trade("no_data", f"no bars for {plan.session:%d %b %Y}", shares)
    length = pd.Timedelta(minutes=_minutes(session, interval) or 0)
    made = None          # when a plan made during its session was made, if that leaves bars out
    if plan.valid_from is not None:
        since = _aligned(plan.valid_from, session["Datetime"].iloc[0])
        later = session[session["Datetime"] >= since]
        if later.empty:
            return _no_trade("not_triggered", "no bar starts after the plan was made", shares)
        if len(later) < len(session):
            made, session = since, later

    long = plan.direction == "long"
    reference = plan.reference_price if plan.reference_price is not None else float(session["Open"].iloc[0])
    stop_order = plan.entry >= reference if long else plan.entry <= reference
    entry_type = "stop" if stop_order else "limit"
    entry, stop, target = plan.entry, plan.stop, plan.target

    filled: tuple[datetime, float] | None = None
    exit_at: tuple[datetime, float, str] | None = None
    for bar in session.itertuples(index=False):
        start = bar.Datetime
        # The bar exit_by falls in is the last: the rest of it may come after the
        # square-off, so only its open is used.
        if plan.exit_by is not None and (start.time() >= plan.exit_by or (start + length).time() > plan.exit_by):
            if filled is None:
                if made is not None and made.time() >= plan.exit_by:
                    why = f"the exit-by time {plan.exit_by:%H:%M} had passed when the plan was made ({made:%H:%M})"
                elif made is not None:
                    why = (f"the entry did not fill between {made:%H:%M}, when the plan was made, and the exit-by "
                           f"time {plan.exit_by:%H:%M}")
                else:
                    why = f"the entry did not fill before the exit-by time {plan.exit_by:%H:%M}"
                return _no_trade("not_triggered", why, shares, entry_type)
            exit_at = (start, float(bar.Open), "exit_by")
            break
        if filled is None:
            price = _fill(bar, entry, stop_order, long)
            if price is None:
                continue
            filled = (start, price)
            # Filled at an open beyond the target too (a gap through both): the
            # target order fills at once, at the same price.
            if target is not None and _reaches(price, target, long, against=False):
                exit_at = (start, price, "target")
                break
            # The fill bar is checked for the stop first: its high and low may
            # both have come after the fill.
            if stop is not None and _reaches(bar.Low if long else bar.High, stop, long, against=True):
                exit_at = (start, min(stop, price) if long else max(stop, price), "stop")
                break
            # Its close did come after the fill: at or beyond the target, the
            # price passed the resting target order on the way there.
            if target is not None and _reaches(float(bar.Close), target, long, against=False):
                exit_at = (start, target, "target")
                break
            continue
        if stop is not None and _reaches(bar.Open, stop, long, against=True):
            exit_at = (start, float(bar.Open), "stop")
            break
        # A bar opening beyond the target fills it at the open, as it does an entry. The
        # open comes first, so a stop the rest of the bar reaches comes after the exit.
        if target is not None and _reaches(float(bar.Open), target, long, against=False):
            exit_at = (start, float(bar.Open), "target")
            break
        if stop is not None and _reaches(bar.Low if long else bar.High, stop, long, against=True):
            exit_at = (start, stop, "stop")
            break
        if target is not None and _reaches(bar.High if long else bar.Low, target, long, against=False):
            exit_at = (start, target, "target")
            break

    if filled is None:
        why = (f"the price never reached the entry after {made:%H:%M}, when the plan was made" if made is not None
               else "the session never reached the entry")
        return _no_trade("not_triggered", why, shares, entry_type)
    if exit_at is None:
        last = session.iloc[-1]
        exit_at = (last["Datetime"], float(last["Close"]), "session_close")

    entry_time, entry_price = filled
    exit_time, exit_price, reason = exit_at
    quantity = shares or 1
    gross = (exit_price - entry_price if long else entry_price - exit_price) * quantity
    charges = trade_charges(profile, long, entry_price, exit_price, quantity, venue)
    net = gross - charges
    at_risk = None
    if stop is not None:
        # A fill nearer the stop than the entry (a limit filled at a gap open) would leave next to
        # nothing, or less, to divide by; a worse fill took on more risk than was planned.
        planned, taken = (entry - stop, entry_price - stop) if long else (stop - entry, stop - entry_price)
        at_risk = max(planned, taken) * quantity
    note = "" if shares else "per share: the plan gives no share count"
    if reason == "session_close":
        note = "; ".join(filter(None, [note, f"closed at the last bar's close ({exit_time:%H:%M})"]))
    return IntradayOutcome(
        triggered=True,
        entry_time=entry_time.to_pydatetime() if hasattr(entry_time, "to_pydatetime") else entry_time,
        entry_price=entry_price,
        exit_time=exit_time.to_pydatetime() if hasattr(exit_time, "to_pydatetime") else exit_time,
        exit_price=exit_price,
        exit_reason=reason,
        gross_pnl=gross,
        charges=charges,
        net_pnl=net,
        net_return_pct=net / (entry_price * quantity) * 100,
        r_multiple=net / at_risk if at_risk and at_risk > 0 else None,
        shares=shares,
        entry_type=entry_type,
        note=note,
    )
