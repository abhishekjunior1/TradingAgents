"""AlphArena paper trading: the client, the decision mapping and the ledger, against the
documented API (https://alpharena.io/api/v1). Every request is faked; nothing reaches the network."""

import hashlib
import inspect
import json
import logging
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
import requests
from requests.structures import CaseInsensitiveDict

import tradingagents
from tradingagents.arena import (
    BASE_URL,
    KEY_ENV,
    SEBI_REFUSAL,
    AlphArenaClient,
    ArenaError,
    ArenaLedger,
    alpharena,
    close_plan,
    decision_fields,
    idempotency_key,
    latest_close,
    ledger as ledger_module,
    mapping,
    plan_trades,
    propose_plan,
    refusal,
    report_note,
    send_plan,
)
from tradingagents.dataflows.config import get_config, set_config
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError

KEY = "aa_test_k3y-for-the-suite"
NOW = datetime(2026, 10, 29, 15, 0, tzinfo=ZoneInfo("America/New_York"))
TODAY = "2026-10-29"
OPENED = {"trade_id": "abc-123-def-456", "status": "open", "filled_at": "2026-10-29T14:30:00Z",
          "fill_price": 195.5, "size_pct": 19.5, "position_value_usd": 1950}


class _Response:
    """What ``requests.request(..., stream=True)`` returns, as far as the client reads it."""

    def __init__(self, status, payload=None, *, body=None, headers=None):
        self.status_code = status
        self.headers = CaseInsensitiveDict(headers or {})
        self.body = body if body is not None else b"" if payload is None else json.dumps(payload).encode()
        self.closed = False

    def iter_content(self, chunk_size=1):
        for start in range(0, len(self.body), chunk_size):
            yield self.body[start:start + chunk_size]

    def close(self):
        self.closed = True


def _error(status, code, message="", headers=None):
    return _Response(status, {"error": {"code": code, "message": message}}, headers=headers)


class _Calls(list):
    def __init__(self):
        super().__init__()
        self.queue = []
        self.sleeps = []
        self.jitter = []


@pytest.fixture
def api(monkeypatch):
    """Queue answers on ``.queue``; the list records each request, ``.sleeps`` each wait."""
    calls = _Calls()

    def fake_request(method, url, **kwargs):
        calls.append(SimpleNamespace(method=method, url=url, **kwargs))
        item = calls.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def top_of_band(low, high):
        calls.jitter.append((low, high))
        return high

    monkeypatch.setattr(alpharena.requests, "request", fake_request)
    monkeypatch.setattr(alpharena.time, "sleep", calls.sleeps.append)
    monkeypatch.setattr(alpharena.random, "uniform", top_of_band)
    monkeypatch.setenv(KEY_ENV, KEY)
    return calls


def _trade(**changes):
    payload = {"ticker": "AAPL", "direction": "long", "entry_price": 195.51,
               "position_value_usd": 1950.0, "idempotency_key": "k"}
    payload.update(changes)
    return payload


def _open(direction, trade_id="t-1", ticker="AAPL"):
    return {"trade_id": trade_id, "ticker": ticker, "direction": direction, "status": "open",
            "fill_price": 190.0}


def _plan(rating, **kwargs):
    kwargs.setdefault("reference_price", 195.51)
    return plan_trades("AAPL", TODAY, rating, **kwargs)


# --- the client: requests, the key and the mode -------------------------------------------


@pytest.mark.unit
def test_a_trade_is_sent_as_documented_with_the_bearer_key(api):
    api.queue.append(_Response(200, OPENED))

    assert AlphArenaClient().submit_trade(_trade()) == OPENED

    call = api[0]
    assert (call.method, call.url) == ("POST", "https://alpharena.io/api/v1/trades")
    assert call.json == _trade()
    assert call.headers["Authorization"] == f"Bearer {KEY}"
    assert call.headers["User-Agent"] == f"TradingAgents/{tradingagents.__version__} arena-client"
    assert call.timeout == 15.0
    assert call.allow_redirects is False


@pytest.mark.unit
def test_own_trades_are_listed_read_and_closed_with_the_key(api):
    api.queue.extend([_Response(200, {"trades": [], "pagination": {"page": 1}}),
                      _Response(200, {"trade_id": "abc-123"}),
                      _Response(200, {"trade_id": "abc-123", "status": "closed"})])
    client = AlphArenaClient()

    client.list_trades()
    client.get_trade("abc-123")
    client.close_trade("abc-123", "Sell rating")

    assert [(c.method, c.url, c.params, c.json) for c in api] == [
        ("GET", f"{BASE_URL}/trades", {"status": "open", "page": 1, "limit": 100}, None),
        ("GET", f"{BASE_URL}/trades/abc-123", None, None),
        ("POST", f"{BASE_URL}/trades/abc-123/close", None, {"close_reason": "Sell rating"}),
    ]
    assert all(c.headers["Authorization"] == f"Bearer {KEY}" for c in api)


@pytest.mark.unit
def test_public_pages_need_no_key_and_are_sent_none(api, monkeypatch):
    api.queue.extend([_Response(200, {"agent_id": "my-agent"}), _Response(200, {"equity_curve": []}),
                      _Response(200, {"positions": [], "cash": 10000}), _Response(200, {"leaderboard": []})])
    client = AlphArenaClient()

    client.agent_profile("my-agent")
    client.agent_pnl("my-agent", "7d")
    monkeypatch.delenv(KEY_ENV)
    client.agent_portfolio("my-agent")
    client.leaderboard()

    assert [(c.method, c.url, c.params) for c in api] == [
        ("GET", f"{BASE_URL}/agents/my-agent", None),
        ("GET", f"{BASE_URL}/agents/my-agent/pnl", {"period": "7d"}),
        ("GET", f"{BASE_URL}/agents/my-agent/portfolio", None),
        ("GET", f"{BASE_URL}/leaderboard", {"period": "30d", "sort": "return", "limit": 20}),
    ]
    assert all("Authorization" not in c.headers for c in api)


@pytest.mark.unit
def test_the_base_url_is_fixed_whatever_the_config_or_environment_says(api, monkeypatch):
    for name in ("ALPHARENA_BASE_URL", "ALPHARENA_API_URL", "ALPHARENA_URL", "TRADINGAGENTS_ARENA_URL"):
        monkeypatch.setenv(name, "https://attacker.example/api/v1")
    set_config({"arena_base_url": "https://attacker.example", "arena_url": "https://attacker.example",
                "alpharena_base_url": "https://attacker.example", "backend_url": "https://attacker.example"})
    api.queue.append(_Response(200, {"trades": []}))

    AlphArenaClient().list_trades()

    assert BASE_URL == "https://alpharena.io/api/v1"
    assert api[0].url == "https://alpharena.io/api/v1/trades"
    assert list(inspect.signature(AlphArenaClient).parameters) == ["mode"]


@pytest.mark.unit
def test_the_key_is_read_at_each_call(api, monkeypatch):
    monkeypatch.delenv(KEY_ENV)
    client = AlphArenaClient()          # made before any key exists
    api.queue.extend([_Response(200, {"trades": []}), _Response(200, {"trades": []})])

    monkeypatch.setenv(KEY_ENV, "aa_test_first")
    client.list_trades()
    monkeypatch.setenv(KEY_ENV, "aa_test_second")
    client.list_trades()

    assert [c.headers["Authorization"] for c in api] == ["Bearer aa_test_first", "Bearer aa_test_second"]
    assert not any("aa_test" in repr(value) for value in vars(client).values())


@pytest.mark.unit
def test_a_missing_key_is_refused_before_any_request(api, monkeypatch):
    monkeypatch.delenv(KEY_ENV)

    with pytest.raises(ArenaError) as caught:
        AlphArenaClient().submit_trade(_trade())

    assert caught.value.code == "MISSING_API_KEY"
    assert KEY_ENV in caught.value.message
    assert api == []


@pytest.mark.unit
@pytest.mark.parametrize("mode, key", [
    ("sandbox", "aa_live_r3al-k3y"), ("live", "aa_test_s4ndbox"), ("sandbox", "sk-another-service"),
    ("live", "AA_LIVE_upper"),
])
def test_a_key_that_does_not_match_the_mode_is_refused_before_any_request(api, monkeypatch, mode, key):
    monkeypatch.setenv(KEY_ENV, key)

    for call in (lambda c: c.submit_trade(_trade()), lambda c: c.list_trades(),
                 lambda c: c.get_trade("abc-123"), lambda c: c.close_trade("abc-123")):
        with pytest.raises(ArenaError) as caught:
            call(AlphArenaClient(mode))
        assert caught.value.code == "KEY_MODE_MISMATCH"
        assert key not in str(caught.value) and key not in repr(caught.value)
    assert api == []


@pytest.mark.unit
def test_the_mode_is_the_arena_mode_setting_read_at_each_call(api, monkeypatch):
    client = AlphArenaClient()
    assert client.mode == "sandbox"     # the default

    set_config({"arena_mode": "live"})
    with pytest.raises(ArenaError, match="KEY_MODE_MISMATCH"):
        client.list_trades()
    monkeypatch.setenv(KEY_ENV, "aa_live_r3al-k3y")
    api.queue.append(_Response(200, {"trades": []}))
    client.list_trades()

    assert [c.headers["Authorization"] for c in api] == ["Bearer aa_live_r3al-k3y"]


@pytest.mark.unit
def test_an_unknown_mode_or_a_key_no_header_can_carry_is_refused(api, monkeypatch):
    with pytest.raises(ArenaError) as caught:
        AlphArenaClient("production").list_trades()
    assert caught.value.code == "INVALID_MODE"

    monkeypatch.setenv(KEY_ENV, "aa_test_two words")
    with pytest.raises(ArenaError) as caught:
        AlphArenaClient().list_trades()
    assert caught.value.code == "INVALID_KEY" and "two words" not in str(caught.value)
    assert api == []


@pytest.mark.unit
def test_the_key_never_reaches_logs_errors_reprs_or_the_ledger(api, caplog, tmp_path):
    caplog.set_level(logging.DEBUG)
    leak = f"HTTPSConnectionPool: header Authorization: Bearer {KEY}"
    api.queue.extend([
        _error(401, "UNAUTHORIZED", f"Missing or invalid API key: {KEY}"),
        requests.ConnectionError(leak), requests.ConnectionError(leak), requests.ConnectionError(leak),
        _Response(200, {**OPENED, "note": f"echo {KEY}"}),
    ])
    client = AlphArenaClient()
    errors = []
    for _ in range(2):
        with pytest.raises(ArenaError) as caught:
            client.list_trades()
        errors.append(caught.value)
    ledger = ArenaLedger(tmp_path / "ledger.jsonl")
    results = send_plan(_plan("Buy"), client, ledger)

    assert [e.code for e in errors] == ["UNAUTHORIZED", "NETWORK_ERROR"]
    assert errors[0].message == "Missing or invalid API key: ***"
    assert results[0]["response"]["note"] == "echo ***"
    texts = [caplog.text, repr(client), repr(ledger), ledger.path.read_text(encoding="utf-8")]
    texts += [str(e) for e in errors] + [repr(e) for e in errors]
    assert all(KEY not in text for text in texts)
    # No exception chain: a requests error holds the request, and the request holds the key.
    assert all(e.__cause__ is None and e.__context__ is None for e in errors)


# --- the client: retries and answers -------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("transient", [
    _error(429, "RATE_LIMITED"), _Response(500), _error(502, "MARKET_PRICE_UNAVAILABLE"), _Response(503),
    requests.ConnectionError(), requests.Timeout(), requests.exceptions.ChunkedEncodingError(),
])
def test_rate_limits_server_errors_and_dropped_connections_are_retried(api, transient):
    api.queue.extend([transient, _Response(200, {"trades": []})])

    assert AlphArenaClient().list_trades() == {"trades": []}
    assert len(api) == 2


@pytest.mark.unit
def test_the_backoff_is_jittered_and_doubles(api):
    api.queue.extend([_Response(503), _Response(503), _Response(200, {"trades": []})])

    AlphArenaClient().list_trades()

    assert api.jitter == [(0.8, 1.2), (0.8, 1.2)]
    assert api.sleeps == [1.2, 2.4]     # the top of the jitter band, from a 1 s base


@pytest.mark.unit
@pytest.mark.parametrize("header, wait", [("7", 7.0), ("600", 30.0), ("0", 0.0), ("-5", 0.0), ("soon", 1.2)])
def test_retry_after_sets_the_wait_capped_at_30_seconds(api, header, wait):
    api.queue.extend([_error(429, "RATE_LIMITED", headers={"Retry-After": header}),
                      _Response(200, {"trades": []})])

    AlphArenaClient().list_trades()

    assert api.sleeps == [wait]


@pytest.mark.unit
def test_retry_after_may_be_an_http_date(api):
    when = format_datetime(datetime.now(UTC) + timedelta(seconds=20), usegmt=True)
    api.queue.extend([_Response(503, headers={"Retry-After": when}), _Response(200, {"trades": []})])

    AlphArenaClient().list_trades()

    assert api.sleeps == [pytest.approx(20, abs=2)]


@pytest.mark.unit
def test_retries_stop_after_three_attempts(api):
    api.queue.extend([_error(503, "SERVICE_UNAVAILABLE", "down")] * 3)

    with pytest.raises(ArenaError) as caught:
        AlphArenaClient().list_trades()

    assert (caught.value.code, caught.value.status) == ("SERVICE_UNAVAILABLE", 503)
    assert len(api) == 3 and len(api.sleeps) == 2


@pytest.mark.unit
@pytest.mark.parametrize("answer", [
    _error(400, "INSUFFICIENT_CASH", "Not enough cash for this position size"),
    _error(401, "UNAUTHORIZED", "Missing or invalid API key"),
    _error(403, "EMAIL_NOT_VERIFIED", "Live key requires email verification"),
    _error(404, "TRADE_NOT_FOUND", "Trade doesn't exist or not yours"),
    _error(429, "DAILY_LIMIT_REACHED", "Signal limit for your tier exceeded"),
])
def test_client_errors_and_a_spent_daily_limit_are_not_retried(api, answer):
    api.queue.append(answer)

    with pytest.raises(ArenaError) as caught:
        AlphArenaClient().submit_trade(_trade())

    assert caught.value.status == answer.status_code
    assert caught.value.code == json.loads(answer.body)["error"]["code"]
    assert len(api) == 1 and api.sleeps == []


@pytest.mark.unit
@pytest.mark.parametrize("error", [requests.exceptions.InvalidHeader(), requests.exceptions.InvalidURL(),
                                   requests.exceptions.TooManyRedirects()])
def test_other_request_errors_are_not_retried(api, error):
    api.queue.append(error)

    with pytest.raises(ArenaError) as caught:
        AlphArenaClient().list_trades()

    assert (caught.value.code, caught.value.message) == ("NETWORK_ERROR", type(error).__name__)
    assert len(api) == 1


@pytest.mark.unit
def test_a_redirect_is_not_followed(api):
    api.queue.append(_Response(302, headers={"Location": "https://elsewhere.example/api/v1/trades"}))

    with pytest.raises(ArenaError) as caught:
        AlphArenaClient().list_trades()

    assert caught.value.code == "HTTP_302"
    assert len(api) == 1 and api[0].allow_redirects is False


@pytest.mark.unit
def test_the_documented_error_envelope_becomes_an_arena_error(api):
    message = "Trades sized ≥ $2,000 must include a thesis (min 8 chars)."
    api.queue.append(_error(400, "THESIS_REQUIRED", message))

    with pytest.raises(ArenaError) as caught:
        AlphArenaClient().submit_trade(_trade())

    error = caught.value
    assert (error.code, error.message, error.status) == ("THESIS_REQUIRED", message, 400)
    assert str(error) == f"AlphArena THESIS_REQUIRED (HTTP 400): {message}"


@pytest.mark.unit
@pytest.mark.parametrize("answer", [
    _Response(400, body=b"<html>Bad Request</html>"), _Response(400, {"detail": "nope"}),
    _Response(400, {"error": "a string"}), _Response(400, {"error": {"code": "not a code!", "message": 5}}),
    _Response(400, body=b""),
])
def test_an_error_without_the_envelope_is_named_by_its_status(api, answer):
    api.queue.append(answer)

    with pytest.raises(ArenaError) as caught:
        AlphArenaClient().list_trades()

    assert (caught.value.code, caught.value.message, caught.value.status) == ("HTTP_400", "", 400)


@pytest.mark.unit
def test_error_messages_are_plain_text_cut_short(api):
    api.queue.append(_error(400, "INVALID_PRICE", "bad\x1b[31m price\u202e\n" + "x" * 1000))

    with pytest.raises(ArenaError) as caught:
        AlphArenaClient().list_trades()

    message = caught.value.message
    assert message == "bad [31m price..."
    assert len(_error(400, "X", "y " * 1000).body) > 300 and len(message) <= 300


@pytest.mark.unit
@pytest.mark.parametrize("answer", [
    _Response(200, body=b"not json"), _Response(200, [1, 2]), _Response(200, body=b'{"trades": NaN}'),
    _Response(200, {"trades": "none"}), _Response(200, {}),
])
def test_a_success_that_is_not_the_documented_shape_is_an_error(api, answer):
    api.queue.append(answer)

    with pytest.raises(ArenaError) as caught:
        AlphArenaClient().list_trades()

    assert caught.value.code == "MALFORMED_RESPONSE"
    assert len(api) == 1


@pytest.mark.unit
def test_an_oversized_answer_is_refused_unread(api, monkeypatch):
    monkeypatch.setattr(alpharena, "_MAX_BODY", 1024)
    answer = _Response(200, {"trades": [], "padding": "x" * 5000})
    api.queue.append(answer)

    with pytest.raises(ArenaError) as caught:
        AlphArenaClient().list_trades()

    assert caught.value.code == "MALFORMED_RESPONSE"
    assert answer.closed


@pytest.mark.unit
def test_strings_in_answers_are_plain_text(api):
    row = {"rank": 1, "agent_id": "edge", "name": "Evil\x1b]0;pwned\x07 Agent\u2066",
           "strategies": ["a\nb"], "stats": {"total_return_pct": 12.94}}
    api.queue.append(_Response(200, {"leaderboard": [row], "pagination": {"page": 1}}))

    (got,) = AlphArenaClient().leaderboard()["leaderboard"]

    assert got["name"] == "Evil ]0;pwned Agent"
    assert got["strategies"] == ["a b"]
    assert got["stats"] == {"total_return_pct": 12.94}


@pytest.mark.unit
@pytest.mark.parametrize("call", [
    lambda c: c.list_trades("pending"), lambda c: c.list_trades(limit=500), lambda c: c.list_trades(page=0),
    lambda c: c.leaderboard(period="1y"), lambda c: c.leaderboard(sort="pnl"),
    lambda c: c.leaderboard(limit=True), lambda c: c.agent_pnl("my-agent", "365d"),
    lambda c: c.get_trade("../agents/x"), lambda c: c.close_trade("abc/../../x"),
    lambda c: c.agent_profile("my agent"), lambda c: c.agent_portfolio(""),
])
def test_arguments_outside_the_documented_values_are_refused_before_any_request(api, call):
    with pytest.raises(ArenaError) as caught:
        call(AlphArenaClient())

    assert caught.value.code == "BAD_INPUT"
    assert api == []


@pytest.mark.unit
@pytest.mark.parametrize("payload, fragment", [
    (_trade(opened_at="2026-10-01T14:30:00Z"), "opened_at"),
    (_trade(asset_type="option", option={"legs": []}), "asset_type, option"),
    (_trade(ticker="RELIANCE.NS"), "RELIANCE.NS"),
    (_trade(ticker="BTC-USD"), "BTC-USD"),
    (_trade(direction="buy"), "direction"),
    (_trade(entry_price=0), "entry_price"),
    (_trade(entry_price=float("nan")), "entry_price"),
    (_trade(size_pct=10), "exactly one"),
    ({k: v for k, v in _trade().items() if k != "position_value_usd"}, "exactly one"),
])
def test_a_trade_outside_the_documented_stock_fields_is_refused_before_any_request(api, payload, fragment):
    with pytest.raises(ArenaError) as caught:
        AlphArenaClient().submit_trade(payload)

    assert caught.value.code == "BAD_INPUT" and fragment in caught.value.message
    assert api == []


# --- which runs may be sent ----------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("ticker", ["AAPL", "aapl", "SPY", "F"])
def test_a_swing_run_on_a_us_ticker_dated_today_in_new_york_is_eligible(ticker):
    assert refusal(ticker, TODAY, now=NOW) is None


@pytest.mark.unit
@pytest.mark.parametrize("ticker", ["RELIANCE.NS", "TCS.BO", "NSE:INFY", "BSE:TCS", "RELIANCE-EQ", "NIFTY", "^BSESN"])
def test_indian_listings_are_refused_with_the_sebi_caution(ticker):
    assert refusal(ticker, TODAY, now=NOW) == SEBI_REFUSAL
    assert refusal(ticker, TODAY, now=NOW) == (
        "AlphArena trades US stocks only; Indian listings stay in the local paper log (SEBI has "
        "cautioned against public paper-trading platforms built on Indian listed-stock prices)"
    )


@pytest.mark.unit
@pytest.mark.parametrize("ticker, kind", [
    ("BTC-USD", "a crypto pair"), ("ETHUSDT", "a crypto pair"), ("^GSPC", "an index"), ("SPX500", "an index"),
    ("VOD.L", "a listing on another exchange"), ("SHOP.TO", "a listing on another exchange"),
    ("0700.HK", "a listing on another exchange"), ("7203.T", "a listing on another exchange"),
    ("EURUSD", "a currency pair"), ("GC=F", "a futures contract"), ("BRK-B", "a share-class ticker"),
    ("GOOGLE", "not a plain ticker"),
])
def test_anything_but_a_plain_us_stock_or_etf_ticker_is_refused(ticker, kind):
    reason = refusal(ticker, TODAY, now=NOW)

    assert reason.startswith("AlphArena trades US stocks and ETFs only; ")
    assert kind in reason


@pytest.mark.unit
def test_a_past_dated_run_is_refused():
    assert refusal("AAPL", "2026-10-28", now=NOW) == (
        "AlphArena fills at today's market price, so only a decision dated today in New York "
        "(2026-10-29) is sent; this run is dated 2026-10-28"
    )


@pytest.mark.unit
def test_today_is_new_yorks_date_not_the_machines():
    after_midnight_in_india = datetime(2026, 10, 30, 0, 30, tzinfo=ZoneInfo("Asia/Kolkata"))  # 15:00 on the 29th in New York

    assert refusal("AAPL", "2026-10-30", now=after_midnight_in_india) is not None
    assert refusal("AAPL", "2026-10-29", now=after_midnight_in_india) is None


@pytest.mark.unit
def test_intraday_plans_backtests_and_other_assets_are_refused():
    assert refusal("AAPL", TODAY, horizon="intraday", now=NOW) == (
        "AlphArena paper trades are sent for swing decisions only; intraday plans stay in the local log"
    )
    assert refusal("AAPL", TODAY, backtest=True, now=NOW) == "Backtest decisions are never sent to AlphArena"
    assert "crypto" in refusal("AAPL", TODAY, asset_type="crypto", now=NOW)
    assert "not a date" in refusal("AAPL", "29/10/2026", now=NOW)


# --- what a decision becomes ---------------------------------------------------------------


@pytest.mark.unit
def test_buy_opens_a_long_below_the_thesis_threshold():
    plan = _plan("Buy", price_target=230.0, stop_loss=180.0, confidence="high")

    assert plan.closes == ()
    assert plan.opens == ({"ticker": "AAPL", "direction": "long", "entry_price": 195.51,
                           "position_value_usd": 1950.0,
                           "idempotency_key": idempotency_key("AAPL", TODAY, "Buy"),
                           "target": 230.0, "stop_loss": 180.0, "confidence": 0.8},)


@pytest.mark.unit
def test_overweight_opens_a_smaller_long():
    (payload,) = _plan("Overweight").opens

    assert (payload["direction"], payload["position_value_usd"]) == ("long", 1000.0)


@pytest.mark.unit
def test_sizes_come_from_the_settings():
    set_config({"arena_buy_position_usd": 1500, "arena_overweight_position_usd": 500})

    assert _plan("Buy").opens[0]["position_value_usd"] == 1500.0
    assert _plan("Overweight").opens[0]["position_value_usd"] == 500.0


@pytest.mark.unit
@pytest.mark.parametrize("rating", ["Hold", "REVIEW", ""])
def test_hold_and_an_unreadable_rating_send_nothing(rating):
    plan = _plan(rating, open_trades=[_open("long"), _open("short", "t-2")])

    assert plan.empty and plan.refused is None
    assert plan.describe().startswith("Nothing to send to AlphArena for AAPL")


@pytest.mark.unit
@pytest.mark.parametrize("rating", ["Underweight", "Sell"])
def test_underweight_and_sell_close_the_open_longs(rating):
    plan = _plan(rating, open_trades=[_open("long", "t-1"), _open("long", "t-2"), _open("short", "t-3")])

    assert plan.closes == ({"trade_id": "t-1", "direction": "long", "close_reason": f"{rating} rating"},
                           {"trade_id": "t-2", "direction": "long", "close_reason": f"{rating} rating"})
    assert plan.opens == ()


@pytest.mark.unit
def test_sell_opens_a_short_only_when_shorts_are_allowed():
    assert _plan("Sell").opens == ()

    plan = _plan("Sell", open_trades=[_open("long")], price_target=170.0, stop_loss=205.0,
                 config={"arena_allow_short": True})

    assert [close["trade_id"] for close in plan.closes] == ["t-1"]
    (payload,) = plan.opens
    assert (payload["direction"], payload["position_value_usd"], payload["target"], payload["stop_loss"]) == (
        "short", 1950.0, 170.0, 205.0)


@pytest.mark.unit
@pytest.mark.parametrize("rating, held", [("Buy", "long"), ("Overweight", "long"), ("Sell", "short")])
def test_nothing_is_added_beside_an_open_trade_in_the_same_direction(rating, held):
    plan = _plan(rating, open_trades=[_open(held, "t-9")], config={"arena_allow_short": True})

    assert plan.opens == ()
    assert any("already exists (trade t-9)" in note for note in plan.notes)


@pytest.mark.unit
def test_a_buy_closes_an_open_short_before_opening_its_long():
    plan = _plan("Buy", open_trades=[_open("short", "t-5")])

    assert plan.closes == ({"trade_id": "t-5", "direction": "short", "close_reason": "Buy rating"},)
    assert plan.opens[0]["direction"] == "long"


@pytest.mark.unit
@pytest.mark.parametrize("rating, target, stop", [
    ("Buy", 190.0, 200.0), ("Buy", 195.51, 195.51), ("Buy", -1.0, float("nan")), ("Sell", 200.0, 190.0),
])
def test_a_target_or_stop_on_the_wrong_side_of_the_entry_is_dropped(rating, target, stop):
    plan = _plan(rating, price_target=target, stop_loss=stop, config={"arena_allow_short": True})

    (payload,) = plan.opens
    assert "target" not in payload and "stop_loss" not in payload
    assert len([note for note in plan.notes if note.endswith("so it is not sent")]) == 2


@pytest.mark.unit
@pytest.mark.parametrize("word, score", [
    ("low", 0.4), ("medium", 0.6), ("high", 0.8), ("Medium", 0.6), (None, None), ("certain", None),
])
def test_confidence_words_become_alpharenas_score(word, score):
    assert _plan("Buy", confidence=word).opens[0].get("confidence") == score


@pytest.mark.unit
def test_the_idempotency_key_is_the_sha256_of_ticker_date_rating_and_horizon():
    expected = hashlib.sha256(b"AAPL|2026-10-29|Buy|swing").hexdigest()

    plan = plan_trades("aapl", TODAY, "buy", reference_price=195.51)

    assert plan.idempotency_key == expected == plan.opens[0]["idempotency_key"]
    assert _plan("Overweight").idempotency_key != expected


@pytest.mark.unit
def test_opened_at_is_never_sent():
    plans = [_plan("Buy"), _plan("Overweight"), _plan("Sell", config={"arena_allow_short": True})]

    assert all("opened_at" not in payload for plan in plans for payload in plan.opens)


SUMMARY = ("Enter on strength above the 50-day average.\x1b[2J  Size at a standard weight; "
           + "the main risk is the earnings date. " * 20)


@pytest.mark.unit
def test_the_thesis_is_off_by_default():
    assert "thesis" not in _plan("Buy", summary=SUMMARY).opens[0]


@pytest.mark.unit
def test_with_arena_send_thesis_the_thesis_is_the_summary_cut_to_300_characters():
    (payload,) = _plan("Buy", summary=SUMMARY, config={"arena_send_thesis": True}).opens

    assert len(payload["thesis"]) <= 300 and payload["thesis"].endswith("...")
    assert payload["thesis"].startswith("Enter on strength above the 50-day average. [2J Size at a standard weight;")


@pytest.mark.unit
def test_a_position_of_2000_or_more_is_not_opened_without_a_thesis():
    plan = _plan("Buy", config={"arena_buy_position_usd": 2500})
    assert plan.opens == ()
    assert any("requires a public thesis" in note for note in plan.notes)

    plan = _plan("Buy", summary=SUMMARY, config={"arena_buy_position_usd": 2500, "arena_send_thesis": True})
    assert plan.opens[0]["position_value_usd"] == 2500.0


@pytest.mark.unit
@pytest.mark.parametrize("price", [None, 0, -1.0, float("nan")])
def test_nothing_is_opened_without_a_reference_price(price):
    plan = _plan("Buy", reference_price=price, open_trades=[_open("short", "t-5")])

    assert plan.opens == ()
    assert [close["trade_id"] for close in plan.closes] == ["t-5"]
    assert "No reference price for AAPL, so nothing is opened" in plan.notes


@pytest.mark.unit
def test_open_trades_in_other_tickers_or_of_another_shape_are_ignored():
    rows = [_open("long", "t-1", ticker="MSFT"), _open("long", "../x"), {"ticker": "AAPL", "direction": "long"},
            {**_open("long", "t-4"), "status": "closed"}, "not a row", _open("sideways", "t-6"),
            _open("long", "t-7", ticker="aapl")]

    plan = _plan("Sell", open_trades=rows)

    assert [close["trade_id"] for close in plan.closes] == ["t-7"]


@pytest.mark.unit
def test_a_plan_for_an_indian_listing_is_refused():
    plan = plan_trades("RELIANCE.NS", TODAY, "Buy", reference_price=2900.0)

    assert plan.refused == SEBI_REFUSAL and plan.empty
    assert plan.describe() == f"Not sent to AlphArena: {SEBI_REFUSAL}"


@pytest.mark.unit
def test_a_plan_for_an_intraday_decision_is_refused():
    plan = plan_trades("AAPL", TODAY, "Buy", reference_price=195.51, horizon="intraday")

    assert plan.empty and "swing decisions only" in plan.refused


@pytest.mark.unit
def test_the_plan_reads_as_plain_lines_for_confirmation():
    plan = _plan("Buy", reference_date="2026-10-28", price_target=230.0, stop_loss=200.0,
                 confidence="medium", open_trades=[_open("short", "t-5")])

    assert plan.describe() == "\n".join([
        "Close the open short in AAPL (trade t-5)",
        "Open a long in AAPL: $1,950 at a reference price of 195.51 (close of 2026-10-28); "
        "target 230.0; confidence 0.6",
        "Note: Stop loss 200.0 is not below the reference price 195.51, so it is not sent",
    ])


# --- reading a finished run ----------------------------------------------------------------


@pytest.mark.unit
def test_decision_fields_are_read_from_the_rendered_decisions():
    from tradingagents.agents.schemas import (
        PortfolioDecision,
        TraderProposal,
        render_pm_decision,
        render_trader_proposal,
    )

    decision = PortfolioDecision(rating="Overweight", investment_thesis="Margins.", price_target=230.5,
                                 executive_summary="Build the position over two sessions.\nKeep it under 5%.",
                                 confidence="medium")
    proposal = TraderProposal(action="Buy", reasoning="Trend.", entry_price=195.0, stop_loss=181.25)
    state = {"final_trade_decision": render_pm_decision(decision), "final_rating": "Overweight",
             "trader_investment_plan": render_trader_proposal(proposal)}

    assert decision_fields(state) == {
        "rating": "Overweight", "price_target": 230.5, "stop_loss": 181.25, "confidence": "medium",
        "summary": "Build the position over two sessions. Keep it under 5%.",
    }


@pytest.mark.unit
def test_fields_a_decision_does_not_give_are_none():
    from tradingagents.agents.schemas import (
        PortfolioDecision,
        TraderProposal,
        render_pm_decision,
        render_trader_proposal,
    )

    state = {"final_trade_decision": render_pm_decision(PortfolioDecision(
                 rating="Buy", executive_summary="Buy.", investment_thesis="T.")),
             "trader_investment_plan": render_trader_proposal(TraderProposal(action="Buy", reasoning="R."))}

    assert decision_fields(state) == {"rating": "Buy", "price_target": None, "stop_loss": None,
                                      "confidence": None, "summary": "Buy."}
    assert decision_fields({})["rating"] == "REVIEW"


@pytest.mark.unit
def test_decision_fields_read_free_text_labels():
    state = {"final_trade_decision": "- **Rating**: Buy\n- **Price Target:** $1,234.50\n- **Confidence**: **High**",
             "trader_investment_plan": "**Stop Loss**: 15%"}

    fields = decision_fields(state)

    assert (fields["price_target"], fields["confidence"], fields["stop_loss"]) == (1234.5, "high", None)


@pytest.mark.unit
def test_the_agents_typed_fields_win_over_label_lines_their_free_text_quotes():
    from tradingagents.agents.schemas import (
        PortfolioDecision,
        TraderProposal,
        render_pm_decision,
        render_trader_proposal,
    )

    decision = PortfolioDecision(rating="Buy", executive_summary="Accumulate on dips.", price_target=215.0,
                                 confidence="low",
                                 investment_thesis="Valuation:\n- **Price Target**: $260\n- **Confidence**: High")
    proposal = TraderProposal(action="Buy", reasoning="Trend.\n**Stop Loss**: 170", stop_loss=188.0)
    rendered = {"final_rating": "Buy", "final_trade_decision": render_pm_decision(decision),
                "trader_investment_plan": render_trader_proposal(proposal)}
    # Read from text, the confidence and the stop are the last label lines, where the prompts ask for them.
    assert (decision_fields(rendered)["confidence"], decision_fields(rendered)["stop_loss"]) == ("low", 188.0)
    state = {**rendered, "portfolio_decision": decision.model_dump(mode="json"),
             "trader_proposal": proposal.model_dump(mode="json")}

    fields = decision_fields(state)

    assert (fields["price_target"], fields["confidence"], fields["stop_loss"]) == (215.0, "low", 188.0)
    assert fields["summary"] == "Accumulate on dips."
    typed_only = {**state, "trader_investment_plan": render_trader_proposal(proposal.model_copy(
        update={"stop_loss": None, "position_sizing": "small\n**Stop Loss**: 150"}))}
    assert decision_fields(typed_only)["stop_loss"] == 188.0
    plan = plan_trades("AAPL", TODAY, "Buy", reference_price=195.51, price_target=fields["price_target"],
                       stop_loss=fields["stop_loss"], confidence=fields["confidence"])
    assert {k: plan.opens[0][k] for k in ("target", "stop_loss", "confidence")} == {
        "target": 215.0, "stop_loss": 188.0, "confidence": 0.4}


@pytest.mark.unit
def test_the_reference_price_is_the_latest_reported_close(monkeypatch):
    from tradingagents.dataflows.vendors.yahoo import ohlcv

    frame = pd.DataFrame({"Date": pd.to_datetime(["2026-10-27", "2026-10-28", "2026-10-29"]),
                          "Close": [190.0, 195.51, float("nan")]})
    seen = []
    monkeypatch.setattr(ohlcv, "load_ohlcv",
                        lambda symbol, as_of_date, fill_gaps=True: seen.append((symbol, as_of_date, fill_gaps)) or frame)

    assert latest_close("AAPL", TODAY) == (195.51, "2026-10-28")
    assert seen == [("AAPL", TODAY, False)]     # as reported, never gap-filled


@pytest.mark.unit
def test_there_is_no_reference_price_without_a_close(monkeypatch):
    from tradingagents.dataflows.vendors.yahoo import ohlcv

    monkeypatch.setattr(ohlcv, "load_ohlcv", lambda *a, **k: pd.DataFrame(
        {"Date": pd.to_datetime([TODAY]), "Close": [float("nan")]}))

    with pytest.raises(NoMarketDataError):
        latest_close("AAPL", TODAY)


def _state(rating, ticker="AAPL", trade_date=TODAY):
    return {"company_of_interest": ticker, "trade_date": trade_date, "asset_type": "stock",
            "final_rating": rating,
            "final_trade_decision": f"**Rating**: {rating}\n\n**Executive Summary**: Act.\n\n"
                                    "**Price Target**: 230.0\n\n**Confidence**: high",
            "trader_investment_plan": "**Action**: Buy\n\n**Stop Loss**: 181.0"}


@pytest.mark.unit
def test_propose_plan_reads_the_open_trades_and_the_reference_price(api, monkeypatch):
    monkeypatch.setattr(mapping, "latest_close", lambda ticker, trade_date: (195.51, "2026-10-28"))
    api.queue.append(_Response(200, {"trades": [_open("short", "t-5")], "pagination": {}}))

    plan = propose_plan(_state("Buy"), AlphArenaClient(), now=NOW)

    assert [close["trade_id"] for close in plan.closes] == ["t-5"]
    assert plan.opens == ({"ticker": "AAPL", "direction": "long", "entry_price": 195.51,
                           "position_value_usd": 1950.0, "idempotency_key": idempotency_key("AAPL", TODAY, "Buy"),
                           "target": 230.0, "stop_loss": 181.0, "confidence": 0.8},)
    assert [(c.method, c.url) for c in api] == [("GET", f"{BASE_URL}/trades")]


@pytest.mark.unit
@pytest.mark.parametrize("failure", [
    VendorUnavailableError("Yahoo Finance rate limited after 3 retries"),
    NoMarketDataError("AAPL", "AAPL", "no closing price to use as a reference price"),
])
def test_without_a_reference_price_the_plan_still_closes(api, monkeypatch, failure):
    """A close needs no price: with shorts on, a Sell still closes the long, as it does with shorts off."""
    def unread(ticker, trade_date):
        raise failure

    monkeypatch.setattr(mapping, "latest_close", unread)
    api.queue.append(_Response(200, {"trades": [_open("long", "t-1")], "pagination": {}}))
    set_config({"arena_allow_short": True})

    plan = propose_plan(_state("Sell"), AlphArenaClient(), now=NOW)

    assert [close["trade_id"] for close in plan.closes] == ["t-1"] and plan.opens == ()
    assert plan.notes == ("No reference price for AAPL, so nothing is opened",
                          f"The reference price could not be read ({failure})")


@pytest.mark.unit
def test_propose_plan_makes_no_request_for_a_run_it_may_not_send(api, monkeypatch):
    monkeypatch.setattr(mapping, "latest_close", lambda *a: pytest.fail("no price is needed"))
    client = AlphArenaClient()

    assert propose_plan(_state("Buy", ticker="RELIANCE.NS"), client, now=NOW).refused == SEBI_REFUSAL
    assert "dated 2026-10-28" in propose_plan(_state("Buy", trade_date="2026-10-28"), client, now=NOW).refused
    assert propose_plan(_state("Buy"), client, backtest=True, now=NOW).refused
    assert propose_plan(_state("Hold"), client, now=NOW).empty
    # An intraday run is refused by its own state, whatever horizon the caller passes.
    intraday = {**_state("Buy"), "decision_horizon": "intraday"}
    for horizon in (None, "swing"):
        assert "intraday plans stay in the local log" in propose_plan(intraday, client, horizon=horizon,
                                                                      now=NOW).refused
    assert api == []


# --- the ledger ----------------------------------------------------------------------------


@pytest.mark.unit
def test_the_ledger_appends_one_json_line_per_entry(tmp_path):
    ledger = ArenaLedger(tmp_path / "arena" / "ledger.jsonl")

    first = ledger.append({"mode": "sandbox", "ticker": "AAPL", "trade_date": TODAY, "rating": "Buy",
                           "action": "open_long", "idempotency_key": "k1", "payload": _trade(),
                           "response": OPENED})
    ledger.append({"mode": "sandbox", "ticker": "AAPL", "trade_date": TODAY, "rating": "Sell",
                   "action": "close_long", "idempotency_key": "k2", "payload": {"trade_id": "t-1"},
                   "error": {"code": "TRADE_NOT_OPEN", "message": "Trade is already closed", "status": 400}})

    lines = ledger.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert [json.loads(line) for line in lines] == ledger.entries()
    assert first == ledger.entries()[0] and first["time"].endswith("+00:00")


@pytest.mark.unit
def test_the_ledger_lives_in_the_results_directory_unless_configured(tmp_path):
    assert ArenaLedger().path == Path(get_config()["results_dir"]) / "arena" / "ledger.jsonl"

    set_config({"arena_ledger_path": str(tmp_path / "mine.jsonl")})

    assert ArenaLedger().path == tmp_path / "mine.jsonl"


@pytest.mark.unit
def test_each_append_holds_the_ledger_lock(tmp_path, monkeypatch):
    held, real = [], ledger_module.locked

    @contextmanager
    def recording(path, wait=True):
        with real(path, wait) as acquired:
            held.append((Path(path), acquired))
            yield acquired

    monkeypatch.setattr(ledger_module, "locked", recording)
    ledger = ArenaLedger(tmp_path / "ledger.jsonl")

    ledger.append({"action": "close_long"})

    assert held == [(ledger.path, True)]
    assert (tmp_path / "ledger.jsonl.lock").exists()


@pytest.mark.unit
def test_parallel_appends_are_neither_lost_nor_interleaved(tmp_path):
    ledger = ArenaLedger(tmp_path / "ledger.jsonl")

    def write(writer):
        for i in range(25):
            ledger.append({"action": "close_long", "writer": writer, "i": i, "payload": {"pad": "x" * 4000}})

    threads = [threading.Thread(target=write, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    entries = ledger.entries()
    assert len(ledger.path.read_text(encoding="utf-8").splitlines()) == len(entries) == 200
    assert sorted((e["writer"], e["i"]) for e in entries) == [(n, i) for n in range(8) for i in range(25)]


@pytest.mark.unit
def test_a_line_cut_short_is_skipped_and_never_fused_with_the_next(tmp_path):
    ledger = ArenaLedger(tmp_path / "ledger.jsonl")
    ledger.append({"action": "open_long", "idempotency_key": "k1", "response": OPENED})
    with open(ledger.path, "a", encoding="utf-8") as handle:
        handle.write('{"action": "open_long", "idempotency_key": "k2", "respo')   # a crash mid-write

    ledger.append({"action": "open_long", "idempotency_key": "k3", "response": OPENED})

    assert [entry["idempotency_key"] for entry in ledger.entries()] == ["k1", "k3"]
    assert ArenaLedger(tmp_path / "missing.jsonl").entries() == []


@pytest.mark.unit
def test_a_line_cut_inside_a_character_or_in_another_encoding_is_skipped(tmp_path):
    """The ledger keeps AlphArena's text as UTF-8 (its THESIS_REQUIRED message holds "≥"),
    so a crash can cut a line inside a character."""
    ledger = ArenaLedger(tmp_path / "ledger.jsonl")
    ledger.append({"mode": "sandbox", "action": "open_long", "idempotency_key": "k1", "response": OPENED})
    line = json.dumps({"mode": "sandbox", "action": "open_long", "idempotency_key": "k2",
                       "error": {"code": "THESIS_REQUIRED", "message": "Trades sized ≥ $2,000 must include a thesis"}},
                      ensure_ascii=False).encode("utf-8")
    with open(ledger.path, "ab") as handle:
        handle.write(line[: line.index("≥".encode()) + 1])                  # a crash mid-character
    ledger.append({"mode": "sandbox", "action": "close_long", "idempotency_key": "k3", "response": OPENED})
    with open(ledger.path, "ab") as handle:
        handle.write('{"action": "close_long", "reason": "Vente à découvert"}\n'.encode("latin-1"))
    # A line separator in an answer is kept as written, and does not end the line.
    ledger.append({"mode": "sandbox", "action": "open_long", "idempotency_key": "k4",
                   "response": {**OPENED, "note": "filled\u2028at the mark\u0085"}})

    assert [entry["idempotency_key"] for entry in ledger.entries()] == ["k1", "k3", "k4"]
    assert ledger.already_submitted("k4", mode="sandbox") and not ledger.already_submitted("k2")


@pytest.mark.unit
def test_already_submitted_counts_only_an_open_alpharena_accepted(tmp_path):
    ledger = ArenaLedger(tmp_path / "ledger.jsonl")
    ledger.append({"mode": "sandbox", "action": "open_long", "idempotency_key": "failed",
                   "error": {"code": "NETWORK_ERROR"}})
    ledger.append({"mode": "sandbox", "action": "close_long", "idempotency_key": "closed",
                   "response": {"status": "closed"}})
    ledger.append({"mode": "sandbox", "action": "open_long", "idempotency_key": "opened", "response": OPENED})

    assert ledger.already_submitted("opened")
    assert ledger.already_submitted("opened", mode="sandbox")
    assert not ledger.already_submitted("opened", mode="live")     # a sandbox trade is not a live one
    assert not ledger.already_submitted("failed")
    assert not ledger.already_submitted("closed")
    assert not ledger.already_submitted("unknown")


@pytest.mark.unit
def test_the_ledger_never_holds_a_key(tmp_path, monkeypatch):
    monkeypatch.setenv(KEY_ENV, KEY)
    ledger = ArenaLedger(tmp_path / "ledger.jsonl")

    written = ledger.append({"action": "open_long", "payload": _trade(),
                             "response": {"api_key": "aa_live_other", "sandbox_api_key": "aa_test_x",
                                          "echo": f"key {KEY}"}})

    text = ledger.path.read_text(encoding="utf-8")
    assert KEY not in text and "aa_live_other" not in text and "aa_test_x" not in text
    assert written["response"] == {"api_key": "***", "sandbox_api_key": "***", "echo": "key ***"}


# --- sending a confirmed plan --------------------------------------------------------------


class _Arena:
    """A client double: records each call and answers, or fails as told."""

    mode = "sandbox"

    def __init__(self, close_error=None, open_error=None):
        self.calls, self.close_error, self.open_error = [], close_error, open_error

    def close_trade(self, trade_id, reason=None):
        self.calls.append(("close", trade_id, reason))
        if self.close_error:
            raise self.close_error
        return {"trade_id": trade_id, "status": "closed", "realized_pnl": 12.5}

    def submit_trade(self, payload):
        self.calls.append(("open", payload["direction"]))
        if self.open_error:
            raise self.open_error
        return dict(OPENED)


@pytest.mark.unit
def test_a_confirmed_plan_closes_then_opens_and_records_each_request(tmp_path):
    plan = _plan("Sell", open_trades=[_open("long", "t-1")], config={"arena_allow_short": True})
    arena, ledger = _Arena(), ArenaLedger(tmp_path / "ledger.jsonl")

    results = send_plan(plan, arena, ledger)

    assert arena.calls == [("close", "t-1", "Sell rating"), ("open", "short")]
    assert results == ledger.entries()
    assert [result["action"] for result in results] == ["close_long", "open_short"]
    opened = results[1]
    assert {key: opened[key] for key in ("mode", "ticker", "trade_date", "rating", "idempotency_key")} == {
        "mode": "sandbox", "ticker": "AAPL", "trade_date": TODAY, "rating": "Sell",
        "idempotency_key": idempotency_key("AAPL", TODAY, "Sell")}
    assert opened["payload"] == plan.opens[0] and opened["response"] == OPENED


@pytest.mark.unit
def test_a_failed_close_is_recorded_and_nothing_is_opened_after_it(tmp_path):
    plan = _plan("Sell", open_trades=[_open("long", "t-1")], config={"arena_allow_short": True})
    arena = _Arena(close_error=ArenaError("MARKET_PRICE_UNAVAILABLE", "Current market price is unavailable", 502))
    ledger = ArenaLedger(tmp_path / "ledger.jsonl")

    results = send_plan(plan, arena, ledger)

    assert arena.calls == [("close", "t-1", "Sell rating")]
    assert results[0]["error"] == {"code": "MARKET_PRICE_UNAVAILABLE",
                                   "message": "Current market price is unavailable", "status": 502}
    assert results[1]["skipped"] == "a close failed, so nothing is opened"
    assert len(ledger.entries()) == 1       # only what was sent is recorded


@pytest.mark.unit
def test_an_open_is_sent_once_per_decision_and_again_only_after_a_failure(tmp_path):
    ledger, plan = ArenaLedger(tmp_path / "ledger.jsonl"), _plan("Buy")
    send_plan(plan, _Arena(open_error=ArenaError("NETWORK_ERROR", "ConnectTimeout")), ledger)
    arena = _Arena()

    send_plan(plan, arena, ledger)              # the failed attempt is tried again
    again = send_plan(plan, arena, ledger)      # the accepted one is not

    assert arena.calls == [("open", "long")]
    assert again[0]["skipped"] == "already sent for this decision (see the ledger)"


@pytest.mark.unit
def test_a_refused_or_empty_plan_sends_nothing(tmp_path):
    arena, ledger = _Arena(), ArenaLedger(tmp_path / "ledger.jsonl")

    assert send_plan(plan_trades("TCS.BO", TODAY, "Buy", reference_price=4000.0), arena, ledger) == []
    assert send_plan(_plan("Hold"), arena, ledger) == []
    assert arena.calls == [] and not ledger.path.exists()


@pytest.mark.unit
def test_a_close_by_hand_closes_every_open_trade_in_the_ticker_and_is_recorded(tmp_path):
    held = [_open("long", "t-1"), _open("short", "t-2"), _open("long", "t-3", ticker="MSFT")]
    plan = close_plan("aapl", held, now=NOW)
    arena, ledger = _Arena(), ArenaLedger(tmp_path / "ledger.jsonl")

    send_plan(plan, arena, ledger)

    assert arena.calls == [("close", "t-1", "manual"), ("close", "t-2", "manual")]
    assert [(e["action"], e["trade_date"], e["rating"]) for e in ledger.entries()] == [
        ("close_long", TODAY, "manual"), ("close_short", TODAY, "manual")]
    assert close_plan("AAPL", [], now=NOW).describe() == (
        "Nothing to send to AlphArena for AAPL\nNote: No open trade in AAPL to close")
    assert close_plan("TCS.BO", held, now=NOW).refused == SEBI_REFUSAL


@pytest.mark.unit
def test_the_report_note_names_what_reached_alpharena(tmp_path):
    plan = _plan("Sell", open_trades=[_open("long", "t-1")], config={"arena_allow_short": True})

    results = send_plan(plan, _Arena(), ArenaLedger(tmp_path / "ledger.jsonl"))

    assert report_note(results) == (
        "Paper trade sent to AlphArena (simulated money, sandbox mode): closed the long in AAPL "
        "(trade t-1); opened a short in AAPL ($1,950, trade abc-123-def-456)."
    )
    failed = send_plan(_plan("Buy"), _Arena(open_error=ArenaError("INSUFFICIENT_CASH")),
                       ArenaLedger(tmp_path / "other.jsonl"))
    assert report_note(failed) is None and report_note([]) is None
