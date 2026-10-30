"""A REST client for AlphArena's paper-trading API, https://alpharena.io/api/v1.

AlphArena is a public arena where trading agents compete with simulated money; nothing
here places a real order. The client covers the documented endpoints a TradingAgents run
uses: opening, reading and closing the agent's own trades, and the public profile, P&L,
portfolio and leaderboard. Registration is not here: the person registers, and accepts
AlphArena's terms, themselves.

- The base URL is fixed in this file and no setting changes it, so a bad config cannot
  send the key to another host. Redirects are not followed, for the same reason.
- The key is read from ``ALPHARENA_API_KEY`` at each call. ``arena_mode`` "sandbox" (the
  default) takes only an ``aa_test_`` key and "live" only an ``aa_live_`` one; a mismatch
  is refused before any request. The key goes only to the endpoints that need it, is never
  logged, and is replaced with ``***`` in any error text, which carries no exception chain.
- Rate limits (429) and server errors (5xx) are retried, three attempts in all, with
  jittered backoff that honours ``Retry-After`` up to 30 s. A spent daily signal limit and
  any other failure raise ``ArenaError`` at once.
- Answers are untrusted text. Bodies are capped at 5 MB and their shape is checked, and
  every string in them is put on one line without control characters and cut short. They
  are for the person to read, never for an LLM prompt.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import random
import re
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import requests

from tradingagents import __version__
from tradingagents.dataflows.config import get_config

logger = logging.getLogger(__name__)

BASE_URL = "https://alpharena.io/api/v1"   # fixed: no setting may point the key at another host
KEY_ENV = "ALPHARENA_API_KEY"
KEY_PREFIXES = {"sandbox": "aa_test_", "live": "aa_live_"}
USER_AGENT = f"TradingAgents/{__version__} arena-client"

# The documented values of the query parameters the client sends.
STATUSES = ("all", "open", "closed")
PERIODS = ("all", "7d", "30d", "90d")
SORTS = ("return", "sharpe", "consistency")

# What a stock trade may carry. Nothing else is sent: not options (AlphArena does not
# verify their P&L), not asset_type, and never opened_at, which would backdate the trade.
TRADE_FIELDS = frozenset({
    "ticker", "direction", "entry_price", "size_pct", "position_value_usd", "stop_loss",
    "target", "thesis", "confidence", "tags", "strategy", "idempotency_key",
})

# A plain US stock or ETF ticker: no exchange suffix, index caret, pair or share-class mark.
US_TICKER = re.compile(r"[A-Z]{1,5}")
# Trade and agent ids go into the URL path, so they are held to a slug.
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")

_TIMEOUT = 15.0
_ATTEMPTS = 3
_MAX_WAIT = 30.0
_MAX_BODY = 5 * 1024 * 1024
_MAX_TEXT = 2000        # longest string kept from an answer
_MAX_MESSAGE = 300      # longest error message kept
_MAX_DEPTH = 32
_TRANSIENT = (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError)
_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_HEADER_SAFE = re.compile(r"[\x21-\x7e]+")
# Control characters, C0 and C1, read as spaces; invisible spaces and direction marks are dropped.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_INVISIBLE = re.compile(r"[\u061c\u200b\u200e\u200f\u202a-\u202e\u2066-\u2069\ufeff]")


class ArenaError(Exception):
    """An AlphArena request that failed, or one refused before it was sent.

    ``code`` is AlphArena's error code (``INSUFFICIENT_CASH``), ``HTTP_<status>`` when the
    answer named none, or the client's own: ``MISSING_API_KEY``, ``KEY_MODE_MISMATCH``,
    ``INVALID_MODE``, ``INVALID_KEY``, ``BAD_INPUT``, ``NETWORK_ERROR``,
    ``MALFORMED_RESPONSE``. ``message`` is plain text without the key; ``status`` is the
    HTTP status, None when no answer came.
    """

    def __init__(self, code: str, message: str = "", status: int | None = None):
        super().__init__(code, message, status)
        self.code = code
        self.message = message
        self.status = status

    def __str__(self) -> str:
        status = f" (HTTP {self.status})" if self.status else ""
        return f"AlphArena {self.code}{status}" + (f": {self.message}" if self.message else "")


def plain_text(text, limit: int = _MAX_TEXT) -> str:
    """``text`` on one line, control characters as spaces and invisible marks dropped, cut to ``limit``."""
    flat = " ".join(_CONTROL.sub(" ", _INVISIBLE.sub("", str(text))).split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 3].rsplit(" ", 1)[0] + "..."


class AlphArenaClient:
    """AlphArena's documented endpoints, for the agent whose key is in ``ALPHARENA_API_KEY``.

    ``mode`` ("sandbox" or "live") defaults to the ``arena_mode`` setting, read at each call.
    Every method returns AlphArena's answer as a dict, or raises ArenaError.
    """

    def __init__(self, mode: str | None = None):
        self._mode = mode

    def __repr__(self) -> str:
        return f"AlphArenaClient(mode={self.mode!r})"

    @property
    def mode(self) -> str:
        return str(self._mode or get_config().get("arena_mode") or "sandbox").strip().lower()

    # --- the agent's own trades (sent with the key) ---

    def submit_trade(self, payload: dict) -> dict:
        """Open a paper trade, ``POST /trades``: a stock trade of AlphArena's documented fields.

        Returns AlphArena's answer: ``trade_id``, ``status``, ``fill_price``, ``filled_at``,
        the size, and ``warnings`` such as ``MISSING_THESIS``.
        """
        body = _trade_body(payload)
        return _expect(self._request("POST", "/trades", body=body, auth=True), "trade_id", str)

    def list_trades(self, status: str = "open", *, page: int = 1, limit: int = 100) -> dict:
        """The agent's trades, ``GET /trades``, newest first: ``{"trades": [...], "pagination": {...}}``."""
        params = {"status": _choice(status, STATUSES, "status"),
                  "page": _whole(page, 1, 10_000, "page"), "limit": _whole(limit, 1, 100, "limit")}
        return _expect(self._request("GET", "/trades", params=params, auth=True), "trades", list)

    def get_trade(self, trade_id: str) -> dict:
        """One of the agent's trades, ``GET /trades/{id}``."""
        return self._request("GET", f"/trades/{_segment(trade_id, 'trade id')}", auth=True)

    def close_trade(self, trade_id: str, reason: str | None = None) -> dict:
        """Close one open trade in full (AlphArena has no partial close), ``POST /trades/{id}/close``.

        The exit is priced by AlphArena from market data; no exit price is sent.
        """
        body = {"close_reason": plain_text(reason, 200)} if reason else {}
        return self._request("POST", f"/trades/{_segment(trade_id, 'trade id')}/close", body=body, auth=True)

    # --- public pages (no key sent) ---

    def agent_profile(self, agent_id: str) -> dict:
        """An agent's public profile and stats, ``GET /agents/{id}``."""
        return self._request("GET", f"/agents/{_segment(agent_id, 'agent id')}")

    def agent_pnl(self, agent_id: str, period: str = "all") -> dict:
        """An agent's P&L and equity curve, ``GET /agents/{id}/pnl``."""
        params = {"period": _choice(period, PERIODS, "period")}
        return self._request("GET", f"/agents/{_segment(agent_id, 'agent id')}/pnl", params=params)

    def agent_portfolio(self, agent_id: str) -> dict:
        """An agent's cash and open positions, ``GET /agents/{id}/portfolio``."""
        data = self._request("GET", f"/agents/{_segment(agent_id, 'agent id')}/portfolio")
        return _expect(data, "positions", list)

    def leaderboard(self, period: str = "30d", sort: str = "return", limit: int = 20) -> dict:
        """The public leaderboard, ``GET /leaderboard``. Names and strategies in it are other
        people's text: show them escaped, never put them into a prompt."""
        params = {"period": _choice(period, PERIODS, "period"), "sort": _choice(sort, SORTS, "sort"),
                  "limit": _whole(limit, 1, 100, "limit")}
        return _expect(self._request("GET", "/leaderboard", params=params), "leaderboard", list)

    # --- transport ---

    def check_key(self) -> None:
        """Check the key as a request would, sending nothing: ArenaError (MISSING_API_KEY,
        KEY_MODE_MISMATCH, ...) when it could not be used. The key is not returned."""
        self._key()

    def _key(self) -> str:
        """The key, read now and checked against the mode; anything amiss is refused here."""
        mode = self.mode
        prefix = KEY_PREFIXES.get(mode)
        if prefix is None:
            raise ArenaError("INVALID_MODE", f"arena_mode must be 'sandbox' or 'live', not {plain_text(mode, 20)!r}")
        key = os.environ.get(KEY_ENV, "").strip()
        if not key:
            raise ArenaError("MISSING_API_KEY", f"{KEY_ENV} is not set; register at alpharena.io and "
                                                "export the sandbox key (aa_test_) first")
        if not key.startswith(prefix):
            raise ArenaError("KEY_MODE_MISMATCH", f"arena_mode {mode!r} takes an {prefix} key, "
                                                  f"and {KEY_ENV} is not one")
        if not _HEADER_SAFE.fullmatch(key):
            raise ArenaError("INVALID_KEY", f"{KEY_ENV} holds characters an HTTP header cannot carry")
        return key

    def _request(self, method: str, path: str, *, params: dict | None = None,
                 body: dict | None = None, auth: bool = False) -> dict:
        """One API call with retries. The key is sent only when ``auth``; it never reaches a log."""
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        key = self._key() if auth else ""
        if key:
            headers["Authorization"] = f"Bearer {key}"
        backoff, wait = 1.0, None
        for attempt in range(_ATTEMPTS):
            if attempt:
                time.sleep(wait if wait is not None else backoff * random.uniform(0.8, 1.2))
                backoff *= 2
            wait, retry = None, False
            try:
                response = requests.request(method, BASE_URL + path, params=params, json=body,
                                            headers=headers, timeout=_TIMEOUT,
                                            allow_redirects=False, stream=True)
                with contextlib.closing(response):
                    status = response.status_code
                    logger.debug("AlphArena %s %s: HTTP %s", method, path, status)
                    if 200 <= status < 300:
                        return _clean(_read_json(response), key)
                    failure = _error(response, key)
                    wait = _retry_after(response)
                    # A spent daily signal limit is a 429 too, but waiting will not lift it.
                    retry = (status == 429 and failure.code != "DAILY_LIMIT_REACHED") or 500 <= status < 600
            except requests.RequestException as exc:
                # The exception holds the request, and with it the key: only its class is kept.
                failure, retry = ArenaError("NETWORK_ERROR", type(exc).__name__), isinstance(exc, _TRANSIENT)
            if not retry:
                break
            logger.info("AlphArena %s %s failed (%s), attempt %d of %d", method, path,
                        failure.code, attempt + 1, _ATTEMPTS)
        raise failure


def _choice(value, allowed: tuple[str, ...], name: str) -> str:
    if value not in allowed:
        raise ArenaError("BAD_INPUT", f"{name} must be one of {', '.join(allowed)}")
    return value


def _whole(value, low: int, high: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ArenaError("BAD_INPUT", f"{name} must be a whole number from {low} to {high}")
    return value


def _segment(value, name: str) -> str:
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ArenaError("BAD_INPUT", f"{plain_text(value, 40)!r} is not a valid {name}")
    return value


def _positive(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0)


def _trade_body(payload) -> dict:
    """``payload`` checked as a US stock trade; anything else is refused before it is sent."""
    if not isinstance(payload, dict):
        raise ArenaError("BAD_INPUT", "a trade is a dict of AlphArena's documented fields")
    unknown = sorted(str(field) for field in set(payload) - TRADE_FIELDS)
    if unknown:
        raise ArenaError("BAD_INPUT", f"never sent to AlphArena: {plain_text(', '.join(unknown), 200)}")
    ticker = payload.get("ticker")
    if not isinstance(ticker, str) or not US_TICKER.fullmatch(ticker):
        raise ArenaError("BAD_INPUT", f"{plain_text(ticker, 32)!r} is not a plain US stock or ETF ticker")
    if payload.get("direction") not in ("long", "short"):
        raise ArenaError("BAD_INPUT", "direction must be 'long' or 'short'")
    if not _positive(payload.get("entry_price")):
        raise ArenaError("BAD_INPUT", "entry_price must be a positive number")
    if ("size_pct" in payload) == ("position_value_usd" in payload):
        raise ArenaError("BAD_INPUT", "give exactly one of size_pct and position_value_usd")
    return dict(payload)


def _scrub(text: str, key: str) -> str:
    return text.replace(key, "***") if key else text


def _read_body(response) -> bytes | None:
    """The body, or None past ``_MAX_BODY``: a misbehaving endpoint cannot stream an
    unbounded answer into memory."""
    chunks, size = [], 0
    for chunk in response.iter_content(chunk_size=65536):
        size += len(chunk)
        if size > _MAX_BODY:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _no_constant(name: str):
    raise ValueError(f"{name} is not a number")


def _parse(body: bytes | None):
    """JSON from a body; None when there is none or it cannot be read. NaN and Infinity,
    which Python's parser would accept, are refused."""
    if not body:
        return None
    try:
        return json.loads(body, parse_constant=_no_constant)
    except (ValueError, RecursionError):
        return None


def _read_json(response) -> dict:
    body = _read_body(response)
    data = _parse(body)
    if not isinstance(data, dict):
        problem = "larger than 5 MB" if body is None else "not a JSON object"
        raise ArenaError("MALFORMED_RESPONSE", f"AlphArena's answer was {problem}", response.status_code)
    return data


def _error(response, key: str) -> ArenaError:
    """AlphArena's ``{"error": {"code", "message"}}`` as an ArenaError; ``HTTP_<status>`` without one."""
    status = response.status_code
    data = _parse(_read_body(response))
    error = data.get("error") if isinstance(data, dict) else None
    code, message = f"HTTP_{status}", ""
    if isinstance(error, dict):
        if isinstance(error.get("code"), str) and _CODE.fullmatch(error["code"]):
            code = error["code"]
        if isinstance(error.get("message"), str):
            message = _scrub(plain_text(_scrub(error["message"], key), _MAX_MESSAGE), key)
    return ArenaError(code, message, status)


def _retry_after(response) -> float | None:
    """``Retry-After`` in seconds (a number or an HTTP date), capped at 30 s; None without one."""
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            seconds = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
        except (TypeError, ValueError):
            return None
    return min(max(0.0, seconds), _MAX_WAIT)


def _clean(value, key: str, depth: int = 0):
    """An answer with every string plain and cut short, and the key removed."""
    if depth > _MAX_DEPTH:
        raise ArenaError("MALFORMED_RESPONSE", "AlphArena's answer is nested too deeply")
    if isinstance(value, str):
        return _scrub(plain_text(_scrub(value, key)), key)
    if isinstance(value, dict):
        return {_clean(k, key, depth + 1): _clean(v, key, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v, key, depth + 1) for v in value]
    return value


def _expect(data: dict, field: str, kind: type) -> dict:
    """``data`` when ``field`` has the documented type; a malformed answer is an error, not data."""
    if not isinstance(data.get(field), kind):
        raise ArenaError("MALFORMED_RESPONSE", f"AlphArena's answer has no {field}")
    return data
