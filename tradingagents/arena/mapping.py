"""Which runs may go to AlphArena, and the paper trades a decision becomes there.

Only a swing decision on a plain US stock or ETF, dated today in New York and not from a
backtest, is sent. AlphArena fills at its own current price, so a past-dated decision or a
backtest cell would be traded at a price it never saw. Indian listings are refused with
the reason, and intraday plans stay local: the arena has no entry triggers or timed exits.

A rating becomes (sizes are settings):

    Buy          open a long of arena_buy_position_usd (1950: under the $2,000 from which
                 AlphArena requires a public thesis)
    Overweight   open a long of arena_overweight_position_usd (1000)
    Hold         nothing
    Underweight  close the open longs in the ticker (AlphArena closes a trade in full only)
    Sell         close the open longs; with arena_allow_short, also open a short of
                 arena_buy_position_usd

Nothing is opened in a direction that already has an open trade in the ticker, and a Buy
or Overweight closes an open short first. The entry price is the latest close, sent as the
reference AlphArena checks against its own mark; the target and stop are sent only on the
side of it where they belong. A thesis is public and cannot be edited, so it is sent only
with arena_send_thesis: the executive summary, cut to 300 characters. opened_at is never
sent. Nothing here reads an arena answer into a prompt.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from tradingagents.arena.alpharena import (
    ID,
    US_TICKER,
    AlphArenaClient,
    ArenaError,
    _positive,
    plain_text,
)
from tradingagents.arena.ledger import ArenaLedger
from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.errors import NoMarketDataError, VendorError
from tradingagents.dataflows.symbols import crypto_base, normalize_symbol
from tradingagents.markets.exchanges import exchange_for_symbol

SEBI_REFUSAL = (
    "AlphArena trades US stocks only; Indian listings stay in the local paper log "
    "(SEBI has cautioned against public paper-trading platforms built on Indian "
    "listed-stock prices)"
)
NEW_YORK = ZoneInfo("America/New_York")
# PortfolioDecision.confidence as AlphArena's 0-1 score.
CONFIDENCE = {"low": 0.4, "medium": 0.6, "high": 0.8}
# AlphArena rejects a trade of $2,000 or more without a thesis of at least 8 characters.
THESIS_REQUIRED_FROM_USD = 2000
THESIS_MIN_CHARS = 8
THESIS_CHARS = 300

# The setting that sizes each opening rating, and its default.
_SIZES = {"Buy": ("arena_buy_position_usd", 1950),
          "Overweight": ("arena_overweight_position_usd", 1000)}
_RATINGS = {rating.lower(): rating for rating in ("Buy", "Overweight", "Hold", "Underweight", "Sell")}
# Yahoo spells a share class with a dash (BRK-B); a dot always starts an exchange suffix (VOD.L).
_SHARE_CLASS = re.compile(r"[A-Z]{1,5}-[A-Z]")
# A "**Label**: value" line as the decision renderers write it ("**Label:** value" too).
_LABEL_LINE = re.compile(r"\s*(?:[-*+]\s+)?\*\*(?P<label>[^*\n]+?)\s*(?::\s*\*\*|\*\*\s*:)(?P<value>.*)")


def new_york_today(now: datetime | None = None) -> date:
    """Today's date on the US market's clock (New York); a naive ``now`` is read as UTC."""
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return now.astimezone(NEW_YORK).date()


def ticker_refusal(ticker, asset_type: str = "stock") -> str | None:
    """Why AlphArena cannot take ``ticker``, or None for a plain US stock or ETF ticker."""
    symbol = normalize_symbol(ticker).strip() if isinstance(ticker, str) else ""
    if not symbol:
        return "No ticker to send"
    if exchange_for_symbol(symbol) is not None:
        return SEBI_REFUSAL
    if asset_type != "stock":
        return f"AlphArena trades US stocks and ETFs only; this run analysed a {plain_text(asset_type, 20)} asset"
    if US_TICKER.fullmatch(symbol) and not crypto_base(symbol):
        return None
    return f"AlphArena trades US stocks and ETFs only; {plain_text(symbol, 32)} is {_kind(symbol)}"


def _kind(symbol: str) -> str:
    if crypto_base(symbol):
        return "a crypto pair"
    if symbol.startswith("^"):
        return "an index"
    if symbol.endswith("=X"):
        return "a currency pair"
    if symbol.endswith("=F"):
        return "a futures contract"
    if _SHARE_CLASS.fullmatch(symbol):
        return "a share-class ticker, whose spelling AlphArena does not document"
    if "." in symbol:
        return "a listing on another exchange"
    return "not a plain ticker of 1-5 letters"


def refusal(ticker: str, trade_date: str, *, horizon: str = "swing", backtest: bool = False,
            asset_type: str = "stock", now: datetime | None = None) -> str | None:
    """Why this run's decision cannot go to AlphArena, or None when it can.

    ``ticker`` is the run's resolved ticker (``RELIANCE.NS``, not ``RELIANCE``), as the
    graph state holds it in ``company_of_interest``.
    """
    reason = ticker_refusal(ticker, asset_type)
    if reason:
        return reason
    if backtest:
        return "Backtest decisions are never sent to AlphArena"
    reason = _horizon_refusal(horizon)
    if reason:
        return reason
    try:
        day = datetime.strptime(str(trade_date), "%Y-%m-%d").date()
    except ValueError:
        return f"{plain_text(trade_date, 20)!r} is not a date (YYYY-MM-DD)"
    today = new_york_today(now)
    if day != today:
        return (f"AlphArena fills at today's market price, so only a decision dated today in "
                f"New York ({today}) is sent; this run is dated {day}")
    return None


def _run_horizon(final_state: dict, horizon: str | None) -> str:
    """The horizon a run's plan is judged by: the run's own or the caller's, whichever is
    not swing (a swing run's state carries no ``decision_horizon``)."""
    named = (final_state.get("decision_horizon"), horizon)
    return next((str(h) for h in named if (h or "swing") != "swing"), "swing")


def _horizon_refusal(horizon: str | None) -> str | None:
    if (horizon or "swing") == "swing":
        return None
    return (f"AlphArena paper trades are sent for swing decisions only; "
            f"{plain_text(horizon, 20)} plans stay in the local log")


def idempotency_key(ticker: str, trade_date: str, rating: str, horizon: str = "swing") -> str:
    """sha256 of ``ticker|trade_date|rating|horizon``: one decision opens one trade, however often it is sent."""
    return hashlib.sha256(f"{ticker}|{trade_date}|{rating}|{horizon}".encode()).hexdigest()


@dataclass(frozen=True)
class ArenaPlan:
    """The paper trades one decision becomes: closes first, then at most one open.

    ``closes`` hold ``trade_id``, ``direction`` and ``close_reason``; ``opens`` are
    ``POST /trades`` payloads. ``notes`` say what was left out and why; ``refused`` is the
    reason nothing at all may be sent.
    """

    ticker: str
    trade_date: str
    rating: str
    idempotency_key: str
    reference_price: float | None = None
    reference_date: str | None = None
    closes: tuple[dict, ...] = ()
    opens: tuple[dict, ...] = ()
    notes: tuple[str, ...] = ()
    refused: str | None = None

    @property
    def empty(self) -> bool:
        """Whether there is nothing to send."""
        return not (self.closes or self.opens)

    def describe(self) -> str:
        """The plan in plain lines, for the person to confirm before anything is sent."""
        if self.refused:
            return f"Not sent to AlphArena: {self.refused}"
        lines = [f"Close the open {close['direction']} in {self.ticker} (trade {close['trade_id']})"
                 for close in self.closes]
        for payload in self.opens:
            seen = f" (close of {self.reference_date})" if self.reference_date else ""
            parts = [f"Open a {payload['direction']} in {self.ticker}: {_usd(payload['position_value_usd'])} "
                     f"at a reference price of {payload['entry_price']}{seen}"]
            parts += [f"{label} {payload[field]}" for field, label in
                      (("target", "target"), ("stop_loss", "stop"), ("confidence", "confidence"))
                      if field in payload]
            lines.append("; ".join(parts))
            if "thesis" in payload:
                lines.append(f"Thesis (public, cannot be edited): {payload['thesis']}")
        if not lines:
            lines.append(f"Nothing to send to AlphArena for {self.ticker}")
        return "\n".join(lines + [f"Note: {note}" for note in self.notes])


def _usd(value: float) -> str:
    return f"${value:,.0f}" if float(value).is_integer() else f"${value:,.2f}"


def _plan(ticker, trade_date: str, rating, horizon: str) -> dict:
    """The fields every plan for this decision shares."""
    symbol = normalize_symbol(ticker).strip() if isinstance(ticker, str) else ""
    rating = _RATINGS.get(str(rating or "").strip().lower(), str(rating or "").strip())
    return {"ticker": symbol, "trade_date": trade_date, "rating": rating,
            "idempotency_key": idempotency_key(symbol, trade_date, rating, horizon)}


def plan_trades(
    ticker: str,
    trade_date: str,
    rating: str,
    *,
    reference_price: float | None = None,
    reference_date: str | None = None,
    price_target: float | None = None,
    stop_loss: float | None = None,
    confidence: str | None = None,
    summary: str | None = None,
    open_trades: Iterable[dict] = (),
    horizon: str = "swing",
    config: dict | None = None,
) -> ArenaPlan:
    """The paper trades ``rating`` becomes for ``ticker``, given the agent's open trades.

    ``reference_price`` and ``reference_date`` are the latest close (``latest_close``);
    ``price_target``, ``confidence`` and ``summary`` come from the Portfolio Manager's
    decision and ``stop_loss`` from the Trader's proposal (``decision_fields``);
    ``open_trades`` is ``list_trades("open")["trades"]``. Nothing is fetched or sent.
    """
    cfg = get_config() if config is None else config
    base = _plan(ticker, trade_date, rating, horizon)
    base.update(reference_price=reference_price, reference_date=reference_date)
    refused = ticker_refusal(ticker) or _horizon_refusal(horizon)
    if refused:
        return ArenaPlan(**base, refused=refused)

    symbol, rating = base["ticker"], base["rating"]
    held = _open_in(open_trades, symbol)
    longs = [trade for trade in held if trade["direction"] == "long"]
    shorts = [trade for trade in held if trade["direction"] == "short"]
    closes, notes, direction, sizing = [], [], None, None
    if rating in ("Buy", "Overweight"):
        closes = [_close(trade, rating) for trade in shorts]
        direction, sizing = "long", _SIZES[rating]
    elif rating in ("Underweight", "Sell"):
        closes = [_close(trade, rating) for trade in longs]
        if not longs:
            notes.append(f"No open long in {symbol} to close")
        if rating == "Sell" and cfg.get("arena_allow_short", False):
            direction, sizing = "short", _SIZES["Buy"]
        elif rating == "Sell":
            notes.append("Shorts are off (arena_allow_short), so no short is opened")
    elif rating == "Hold":
        notes.append("Hold keeps any open trade as it is; nothing is sent")
    else:
        notes.append(f"{plain_text(rating, 20) or 'No rating'} is not a tradeable rating; nothing is sent")

    opens = []
    if direction:
        same = longs if direction == "long" else shorts
        if same:
            notes.append(f"An open {direction} in {symbol} already exists (trade {same[0]['trade_id']}), "
                         "so none is added")
        else:
            size_key, default = sizing
            payload, more = _open_payload(
                symbol, direction, size_key, cfg.get(size_key, default),
                reference_price=reference_price, price_target=price_target, stop_loss=stop_loss,
                confidence=confidence, summary=summary,
                send_thesis=bool(cfg.get("arena_send_thesis", False)), key=base["idempotency_key"],
            )
            notes.extend(more)
            if payload:
                opens.append(payload)
    return ArenaPlan(**base, closes=tuple(closes), opens=tuple(opens), notes=tuple(notes))


def close_plan(ticker: str, open_trades: Iterable[dict], *, now: datetime | None = None) -> ArenaPlan:
    """A plan closing every open trade in ``ticker``, for a close the person asks for by hand.

    ``open_trades`` is ``list_trades("open")["trades"]``. Sent with ``send_plan`` once the
    person confirms, so the closes are recorded in the ledger like any other request.
    """
    base = _plan(ticker, new_york_today(now).isoformat(), "manual", "swing")
    refused = ticker_refusal(ticker)
    if refused:
        return ArenaPlan(**base, refused=refused)
    closes = tuple({**trade, "close_reason": "manual"} for trade in _open_in(open_trades, base["ticker"]))
    notes = () if closes else (f"No open trade in {base['ticker']} to close",)
    return ArenaPlan(**base, closes=closes, notes=notes)


def _open_in(trades: Iterable[dict], symbol: str) -> list[dict]:
    """The open trades in ``symbol`` from a ``list_trades`` answer; rows not of the documented shape are skipped."""
    held = []
    for trade in trades or ():
        if not isinstance(trade, dict) or str(trade.get("ticker", "")).upper() != symbol:
            continue
        if trade.get("status", "open") != "open" or trade.get("direction") not in ("long", "short"):
            continue
        if isinstance(trade.get("trade_id"), str) and ID.fullmatch(trade["trade_id"]):
            held.append({"trade_id": trade["trade_id"], "direction": trade["direction"]})
    return held


def _close(trade: dict, rating: str) -> dict:
    return {"trade_id": trade["trade_id"], "direction": trade["direction"], "close_reason": f"{rating} rating"}


def _open_payload(symbol: str, direction: str, size_key: str, size, *, reference_price,
                  price_target, stop_loss, confidence, summary, send_thesis: bool,
                  key: str) -> tuple[dict | None, list[str]]:
    """The ``POST /trades`` payload for an open, or None; and notes on what was left out."""
    if not _positive(reference_price):
        return None, [f"No reference price for {symbol}, so nothing is opened"]
    if not _positive(size):
        return None, [f"{size_key} must be a positive dollar amount, so nothing is opened"]
    entry = round(float(reference_price), 4)
    payload = {"ticker": symbol, "direction": direction, "entry_price": entry,
               "position_value_usd": float(size), "idempotency_key": key}
    notes = []
    long = direction == "long"
    for field, label, value, above in (("target", "Price target", price_target, long),
                                       ("stop_loss", "Stop loss", stop_loss, not long)):
        level, note = _on_side(label, value, entry, above)
        if level is not None:
            payload[field] = level
        if note:
            notes.append(note)
    score = CONFIDENCE.get(str(confidence).strip().lower()) if confidence else None
    if score is not None:
        payload["confidence"] = score
    thesis = plain_text(summary, THESIS_CHARS) if send_thesis and summary else ""
    if thesis:
        payload["thesis"] = thesis
    if size >= THESIS_REQUIRED_FROM_USD and len(thesis) < THESIS_MIN_CHARS:
        return None, notes + [f"AlphArena requires a public thesis for a position of $2,000 or more; "
                              f"set {size_key} below 2000 or turn on arena_send_thesis. Nothing is opened"]
    return payload, notes


def _on_side(label: str, value, entry: float, above: bool) -> tuple[float | None, str | None]:
    """``value`` when it is a price on the wanted side of ``entry``; else None and why it is left out."""
    if value is None:
        return None, None
    if not _positive(value):
        return None, f"{label} {value!r} is not a price, so it is not sent"
    if (value > entry) if above else (value < entry):
        return float(value), None
    side = "above" if above else "below"
    return None, f"{label} {value} is not {side} the reference price {entry}, so it is not sent"


def decision_fields(final_state: dict) -> dict:
    """What the mapping reads from a finished run: the rating; the Portfolio Manager's price
    target, confidence and executive summary; and the Trader's stop loss.

    Taken from the agents' structured output when the run kept it (``portfolio_decision``,
    ``trader_proposal``), so a label line their free text quotes cannot stand in for a
    field. A decision or proposal written as free text is read from its ``**Label**: value``
    lines: the first Price Target, and the last Confidence and Stop Loss, where the prompts
    ask for them. A field absent or "not provided" is None.
    """
    # Imported here: the agents package loads every agent, which the arena client does not need.
    from tradingagents.agents.rating import run_rating
    from tradingagents.agents.schemas import _coerce_optional_float

    decision = str(final_state.get("final_trade_decision") or "")
    proposal = str(final_state.get("trader_investment_plan") or "")
    typed, trader = final_state.get("portfolio_decision"), final_state.get("trader_proposal")
    if isinstance(typed, dict):
        price_target = _coerce_optional_float(typed.get("price_target"))
        confidence = str(typed.get("confidence") or "").lower()
        summary = " ".join(str(typed.get("executive_summary") or "").split()) or None
    else:
        price_target = _coerce_optional_float(_field(decision, "Price Target"))
        confidence = (_field(decision, "Confidence", last=True) or "").lower()
        summary = _field(decision, "Executive Summary", block=True)
    stop_loss = trader.get("stop_loss") if isinstance(trader, dict) else _field(proposal, "Stop Loss", last=True)
    return {
        "rating": run_rating(final_state),
        "price_target": price_target,
        "stop_loss": _coerce_optional_float(stop_loss),
        "confidence": confidence if confidence in CONFIDENCE else None,
        "summary": summary,
    }


def _field(text: str, label: str, block: bool = False, last: bool = False) -> str | None:
    """The value of the first ``**Label**: value`` line, or the last with ``last``; ``block``
    reads on to the next label."""
    lines = text.splitlines()
    for i in range(len(lines) - 1, -1, -1) if last else range(len(lines)):
        match = _LABEL_LINE.match(lines[i])
        if not match or match.group("label").strip().lower() != label.lower():
            continue
        parts = [match.group("value")]
        if block:
            for more in lines[i + 1:]:
                if _LABEL_LINE.match(more) or more.lstrip().startswith("#"):
                    break
                parts.append(more)
        value = " ".join(" ".join(parts).split()).strip(" *")
        return value if value and value.lower() not in ("not provided", "n/a", "none") else None
    return None


def latest_close(ticker: str, trade_date: str) -> tuple[float, str]:
    """The latest close on or before ``trade_date``, and its date: the trade's reference price.

    The verified market snapshot's data path: Yahoo daily bars as reported, not gap-filled.
    During a session the newest bar's close is the latest price. Raises NoMarketDataError
    without one, so nothing is opened on a price that was not fetched.
    """
    # Imported here: the data layer loads yfinance, which the arena client does not need.
    from tradingagents.dataflows.vendors.yahoo.ohlcv import load_ohlcv

    frame = load_ohlcv(ticker, trade_date, fill_gaps=False)
    rows = frame.dropna(subset=["Close"]) if frame is not None and "Close" in frame else None
    if rows is None or rows.empty or not _positive(float(rows["Close"].iloc[-1])):
        raise NoMarketDataError(ticker, normalize_symbol(ticker), "no closing price to use as a reference price")
    last = rows.iloc[-1]
    return float(last["Close"]), f"{last['Date']:%Y-%m-%d}"


def propose_plan(final_state: dict, client: AlphArenaClient, *, horizon: str | None = None,
                 backtest: bool = False, now: datetime | None = None,
                 config: dict | None = None) -> ArenaPlan:
    """The plan for a finished run, fetching what it needs: the agent's open trades (with
    the key) and, when a trade may be opened, the reference price.

    A run ``refusal`` turns away comes back refused, with no request made. The run's own
    horizon (its state's ``decision_horizon``) is judged as well as ``horizon``, so an
    intraday run is refused whatever the caller passes. A failed read
    of the open trades raises ArenaError, before anything is sent. Without a reference
    price nothing is opened, the plan's notes say why, and its closes still stand.
    """
    cfg = get_config() if config is None else config
    ticker = final_state.get("company_of_interest", "")
    trade_date = final_state.get("trade_date", "")
    horizon = _run_horizon(final_state, horizon)
    fields = decision_fields(final_state)
    rating = fields["rating"]
    reason = refusal(ticker, trade_date, horizon=horizon, backtest=backtest,
                     asset_type=final_state.get("asset_type") or "stock", now=now)
    if reason:
        return ArenaPlan(**_plan(ticker, trade_date, rating, horizon), refused=reason)
    opens = rating in ("Buy", "Overweight") or (rating == "Sell" and cfg.get("arena_allow_short", False))
    trades = client.list_trades("open")["trades"] if rating in ("Buy", "Overweight", "Underweight", "Sell") else []
    price = day = unread = None
    if opens:
        try:
            price, day = latest_close(ticker, trade_date)
        except VendorError as exc:
            # A close needs no price: a Sell with shorts on closes as it does with them off.
            unread = f"The reference price could not be read ({exc})"
    plan = plan_trades(ticker, trade_date, rating, reference_price=price, reference_date=day,
                       price_target=fields["price_target"], stop_loss=fields["stop_loss"],
                       confidence=fields["confidence"], summary=fields["summary"],
                       open_trades=trades, horizon=horizon, config=cfg)
    return replace(plan, notes=(*plan.notes, unread)) if unread else plan


def send_plan(plan: ArenaPlan, client: AlphArenaClient, ledger: ArenaLedger | None = None) -> list[dict]:
    """Send a plan the person confirmed: its closes, then its open. Returns one result per trade.

    Each request goes into the ledger with AlphArena's answer or the error; an ArenaError
    is recorded, not raised. An open AlphArena already accepted for this decision (same
    idempotency key and mode) is skipped, and nothing is opened after a close failed, so a
    long is never left open beside a new short.
    """
    if plan.refused or plan.empty:
        return []
    ledger = ledger or ArenaLedger()
    mode = client.mode
    results = [
        _send(ledger, plan, mode, f"close_{close['direction']}",
              {"trade_id": close["trade_id"], "close_reason": close["close_reason"]},
              client.close_trade, close["trade_id"], close["close_reason"])
        for close in plan.closes
    ]
    closed = all("response" in result for result in results)
    for payload in plan.opens:
        action = f"open_{payload['direction']}"
        if not closed:
            skipped = "a close failed, so nothing is opened"
        elif ledger.already_submitted(plan.idempotency_key, mode):
            skipped = "already sent for this decision (see the ledger)"
        else:
            results.append(_send(ledger, plan, mode, action, payload, client.submit_trade, payload))
            continue
        results.append({"mode": mode, "ticker": plan.ticker, "action": action,
                        "payload": payload, "skipped": skipped})
    return results


def _send(ledger: ArenaLedger, plan: ArenaPlan, mode: str, action: str, payload: dict,
          call, *args) -> dict:
    """Make one request and record it with AlphArena's answer or the error."""
    entry = {"mode": mode, "ticker": plan.ticker, "trade_date": plan.trade_date,
             "rating": plan.rating, "action": action,
             "idempotency_key": plan.idempotency_key, "payload": payload}
    try:
        entry["response"] = call(*args)
    except ArenaError as exc:
        entry["error"] = {"code": exc.code, "message": exc.message, "status": exc.status}
    return ledger.append(entry)


def report_note(results: list[dict]) -> str | None:
    """The saved report's line on what reached AlphArena, or None when nothing did."""
    done, mode = [], None
    for result in results:
        if not isinstance(result.get("response"), dict):
            continue
        mode = result.get("mode") or mode
        payload = result.get("payload") or {}
        if result["action"].startswith("open_"):
            trade = result["response"].get("trade_id")
            ref = f", trade {trade}" if isinstance(trade, str) and ID.fullmatch(trade) else ""
            done.append(f"opened a {payload['direction']} in {result['ticker']} "
                        f"({_usd(payload['position_value_usd'])}{ref})")
        else:
            done.append(f"closed the {result['action'].removeprefix('close_')} in {result['ticker']} "
                        f"(trade {payload['trade_id']})")
    if not done:
        return None
    return f"Paper trade sent to AlphArena (simulated money, {mode} mode): {'; '.join(done)}."
