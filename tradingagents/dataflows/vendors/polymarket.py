"""Polymarket prediction-market vendor.

Surfaces live, market-implied probabilities for forward-looking events (Fed
decisions, recession, elections, geopolitics, crypto) to the news analyst, as a
complement to news (what happened) and FRED macro data (where things stand):
what the crowd actually prices to happen next.

Uses Polymarket's public Gamma API (https://gamma-api.polymarket.com) — no key,
no auth. Each market's ``outcomePrices`` are the implied probabilities of its
outcomes (a "Yes" at 0.76 means the market prices a 76% chance).
"""
import json
import logging
import math
from datetime import UTC, datetime
from time import monotonic

import requests
from urllib3.exceptions import ConnectTimeoutError

from tradingagents.dataflows.date_window import is_historical

logger = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"

# Seconds to wait for a connection, then for the answer (the read timeout matches
# the other vendors). urllib3 counts the TLS handshake (and a proxy's CONNECT) as
# connecting, so a slow one needs room; a network that cannot reach Polymarket,
# or stalls the handshake, waits this long once per RETRY_AFTER (see _request).
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 30

# Seconds after a failure to connect, or a timeout, during which requests fail at once.
RETRY_AFTER = 300

# Default number of markets to return, ranked by traded volume.
DEFAULT_LIMIT = 6

# When (``monotonic``) and how the last request failed to connect or timed out;
# None until one has.
_connect_failure: tuple[float, str] | None = None


def _request(path: str, params: dict) -> dict:
    """GET a Gamma endpoint.

    After a failure to connect, or a timeout before Polymarket answers, requests within
    ``RETRY_AFTER`` seconds raise at once: the news analyst asks about several topics
    a run, and on 2026-10-30 each of them waited out a 30 s connect timeout to a
    network that could not reach Polymarket. A timeout costs its whole wait each time,
    and urllib3 reports a TLS handshake or a proxy's CONNECT that stalls as a read
    timeout. A server that answers, even with an error, is asked again, and so is one
    that drops the connection or rejects the TLS handshake: that fails at once.
    """
    global _connect_failure
    if _connect_failure is not None:
        ago = monotonic() - _connect_failure[0]
        if ago < RETRY_AFTER:
            raise requests.ConnectionError(
                f"{_connect_failure[1]} {ago:.0f} s ago; not tried again for another {RETRY_AFTER - ago:.0f} s")
    try:
        response = requests.get(f"{GAMMA_BASE}/{path}", params=params, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
    except (requests.ConnectionError, requests.Timeout) as exc:
        if isinstance(exc, requests.Timeout) or _not_connected(exc):
            # Not the error's text: it holds this request's address, with its topic.
            _connect_failure = (monotonic(), f"a request to {GAMMA_BASE} failed ({type(exc).__name__})")
        raise
    response.raise_for_status()
    return response.json()


def _not_connected(exc: requests.ConnectionError) -> bool:
    """Whether no connection was made, to Polymarket or the proxy: a connect timeout,
    a refusal or an unknown host. urllib3 tells these apart the same way
    (``Retry._is_connection_error``); its NewConnectionError is a ConnectTimeoutError."""
    reason = getattr(exc.args[0], "reason", None) if exc.args else None
    reason = getattr(reason, "original_error", reason)   # urllib3's ProxyError wraps it
    return isinstance(exc, requests.ConnectTimeout) or isinstance(reason, ConnectTimeoutError)


def _parse_json_list(value) -> list:
    """Gamma encodes ``outcomes``/``outcomePrices`` as JSON-string arrays."""
    if isinstance(value, list):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []


def _is_forward_looking(market: dict, now: datetime) -> bool:
    """Keep only open markets that resolve in the future.

    ``closed`` is the reliable resolved flag (``active`` stays True even for
    settled markets), and a past ``endDate`` means the event already resolved —
    either way it is not a forward-looking signal.
    """
    if market.get("closed"):
        return False
    end_date = market.get("endDate")
    if end_date:
        try:
            if datetime.fromisoformat(end_date.replace("Z", "+00:00")) < now:
                return False
        except ValueError:
            pass
    return bool(_parse_json_list(market.get("outcomePrices"))) and bool(
        _parse_json_list(market.get("outcomes"))
    )


def _odds(prob: float) -> str:
    """A probability as a percentage; within 1% of 0 or 100 its distance from
    there keeps two significant digits, so 0.05% is not 0% and 99.95% not 100%."""
    def decimals(distance: float) -> int:
        return 1 - math.floor(math.log10(distance))

    if 0 < prob < 0.01:
        percent = prob * 100
        return f"{percent:.{decimals(percent)}f}".rstrip("0").rstrip(".") + "%"
    if 0.99 < prob < 1:
        distance = (1 - prob) * 100
        return f"{100 - distance:.{decimals(distance)}f}".rstrip("0").rstrip(".") + "%"
    return f"{prob:.0%}"


def get_prediction_markets(topic: str, limit: int | None = None, as_of_date: str | None = None) -> str:
    """Return live prediction-market probabilities for an event topic.

    Args:
        topic: Event keyword(s), e.g. "Fed rate cut", "recession 2026",
            "US election", or a sector/company event.
        limit: Max markets to return (ranked by traded volume); ``None`` uses
            DEFAULT_LIMIT.
        as_of_date: The analysis date. Polymarket serves only live odds, so a
            date before today withholds them.

    Returns:
        A markdown report of the most-traded open markets matching the topic,
        each with its implied probability, traded volume, resolution date, and
        recent (1-week) move.
    """
    if is_historical(as_of_date):
        return (
            f"Prediction-market odds are withheld for {as_of_date}. Polymarket serves "
            f"only live odds on open markets, with no historical vintage, so serving "
            f"them would put post-decision information into a {as_of_date} analysis."
        )
    if limit is None:
        limit = DEFAULT_LIMIT

    try:
        data = _request("public-search", {"q": topic, "limit_per_type": 20})
    except requests.RequestException as e:
        logger.warning("Polymarket search failed for %r: %s", topic, e)
        return (
            f"Polymarket data is currently unavailable (network error: {e}). "
            f"Proceed without prediction-market signal for '{topic}'."
        )

    now = datetime.now(UTC)
    candidates = [
        m
        for event in data.get("events", [])
        for m in event.get("markets", [])
        if _is_forward_looking(m, now)
    ]
    candidates.sort(key=lambda m: m.get("volumeNum") or 0, reverse=True)

    header = (
        f'## Polymarket prediction markets: "{topic}"\n'
        f"Live, market-implied probabilities (higher traded volume = deeper, "
        f"more reliable). A probability is the crowd's priced odds of the event, "
        f"not a forecast you should take as certain.\n\n"
    )

    if not candidates:
        return header + (
            f"No open prediction markets matched '{topic}'. Polymarket coverage "
            f"is concentrated in macro, political, geopolitical, and crypto "
            f"events; a specific equity may have none."
        )

    lines = []
    for m in candidates[:limit]:
        prices = _parse_json_list(m.get("outcomePrices"))
        outcomes = _parse_json_list(m.get("outcomes"))
        try:
            prob = float(prices[0])
        except (ValueError, IndexError):
            continue
        label = outcomes[0] if outcomes else "Yes"
        volume = m.get("volumeNum") or 0
        end_date = (m.get("endDate") or "")[:10]
        wk = m.get("oneWeekPriceChange")
        wk_str = (
            f", 1-week {wk * 100:+.1f}pp"
            if isinstance(wk, (int, float)) and wk
            else ""
        )
        lines.append(
            f"- **{m.get('question')}** — {label} {_odds(prob)} "
            f"(${volume:,.0f} volume, resolves {end_date}{wk_str})"
        )

    return header + "\n".join(lines) + "\n"
