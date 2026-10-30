"""News review fixes, round 2: Polymarket pauses after a timeout before it answers,
including a TLS handshake or a proxy's CONNECT that stalls (urllib3 reports either
as a read timeout); and a web-search story whose time gives no zone is shown on the
date it states, as read_web_page shows it, while still counted no earlier than its
UTC reading.

No test reaches the network: Agent-Reach and the clock are faked, and Polymarket
goes through the real requests stack with the connections urllib3 opens faked.
"""

import socket
import threading
from datetime import UTC, datetime

import pytest
import urllib3.util.connection

from tradingagents.dataflows import date_window
from tradingagents.dataflows.config import get_config, run_config
from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.vendors import polymarket
from tradingagents.dataflows.vendors.agent_reach import media, news as ar_news

# ── Polymarket pauses after a timeout, whichever step it stalled in ────────

STALL = None    # the far end takes the connection and never sends a byte
_BODY = b'{"events": []}'
ANSWER = (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s"
          % (len(_BODY), _BODY))


@pytest.fixture
def gamma(monkeypatch, tmp_path):
    """Polymarket at https://gamma.test through the real requests stack, each connection
    urllib3 opens faked by a socket pair whose far end sends ``plan``'s next bytes, or
    nothing (STALL), and keeps the connection open; ``opened`` records each one's
    address. The connect timeout is cut to 0.2 s, so a stalled TLS handshake or
    CONNECT ends quickly. Only this test's thread is served: a connection another
    thread opens meanwhile (one an earlier test left running) is refused, so it can
    neither take a planned answer nor count as opened."""
    plan, opened, servers = [], [], []
    owner = threading.get_ident()

    def create_connection(address, timeout=None, *args, **kwargs):
        if threading.get_ident() != owner:
            raise OSError(f"test tried to reach the network: {address}")
        if not plan:
            raise AssertionError(f"unplanned connection to {address}; opened before it: {opened}")
        opened.append(address)
        outcome = plan.pop(0)
        ours, theirs = socket.socketpair()
        ours.settimeout(timeout)
        servers.append(theirs)
        if outcome is not STALL:
            theirs.sendall(outcome)
        return ours

    monkeypatch.setattr(urllib3.util.connection, "create_connection", create_connection)
    monkeypatch.setattr(polymarket, "GAMMA_BASE", "https://gamma.test")
    monkeypatch.setattr(polymarket, "CONNECT_TIMEOUT", 0.2)
    monkeypatch.setattr(polymarket, "_connect_failure", None)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "*")                        # nor the system's proxy settings
    monkeypatch.setenv("NETRC", str(tmp_path / "netrc"))     # nor the user's ~/.netrc
    yield plan, opened
    for server in servers:
        server.close()


def _two_topics() -> str:
    """Ask about two topics in turn; the first must fail, and the second's answer is returned."""
    first = polymarket.get_prediction_markets("RBI rate cut")
    assert "Polymarket data is currently unavailable" in first, first
    return polymarket.get_prediction_markets("Fed rate cut December")


@pytest.mark.unit
def test_polymarket_pauses_after_a_tls_handshake_that_stalls(gamma):
    # TCP connects but the handshake gets no answer (a middlebox that drops the ClientHello).
    plan, opened = gamma
    plan += [STALL, STALL]
    second = _two_topics()
    assert "failed (ReadTimeout)" in second and "not tried again" in second, second
    assert "'Fed rate cut December'" in second and "RBI+rate+cut" not in second, second
    assert len(opened) == 1, opened


@pytest.mark.unit
def test_polymarket_pauses_after_its_proxy_stalls_the_connect(gamma, monkeypatch):
    # The proxy takes the connection but never answers the CONNECT (it is still
    # waiting on its own connection to a Polymarket it cannot reach).
    plan, opened = gamma
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.test:3128")
    plan += [STALL, STALL]
    second = _two_topics()
    assert "failed (ReadTimeout)" in second and "not tried again" in second
    assert opened == [("proxy.test", 3128)]


@pytest.mark.unit
def test_polymarket_pauses_after_it_took_the_request_but_did_not_answer_in_time(gamma, monkeypatch):
    # Waiting for an answer that does not come costs the whole read timeout each time.
    plan, opened = gamma
    monkeypatch.setattr(polymarket, "GAMMA_BASE", "http://gamma.test")
    monkeypatch.setattr(polymarket, "READ_TIMEOUT", 0.2)
    plan += [STALL, STALL]
    second = _two_topics()
    assert "failed (ReadTimeout)" in second and "not tried again" in second
    assert len(opened) == 1


@pytest.mark.unit
def test_polymarket_is_asked_again_after_an_answer_that_stopped_part_way(gamma, monkeypatch):
    # It answered, so it is asked again: the rest of the body never came (requests
    # reports that as a ConnectionError, not a timeout).
    plan, opened = gamma
    monkeypatch.setattr(polymarket, "GAMMA_BASE", "http://gamma.test")
    monkeypatch.setattr(polymarket, "READ_TIMEOUT", 0.2)
    plan += [ANSWER[:-5], ANSWER]
    second = _two_topics()
    assert "No open prediction markets matched 'Fed rate cut December'" in second
    assert len(opened) == 2


# ── a web-search time with no zone is shown on the date it states ──────────

def _item(title, url, published_at=None, text="body", author=None):
    """An item as Agent-Reach's client returns it."""
    return {"title": title, "url": url, "author": author, "published_at": published_at,
            "text": text, "duration": None, "lang": None}


@pytest.fixture
def web(monkeypatch):
    """Agent-Reach answers a web search from ``answers["*"]`` and a page read from ``answers["page"]``."""
    answers = {}

    def run(channel, action, positional, params=None, timeout=None):
        if (channel, action) == ("web", "search"):
            return {"items": list(answers.get("*", []))}
        if (channel, action) == ("web", "read"):
            return {"items": [answers["page"]]}
        return {"items": []}

    monkeypatch.setattr(ar_news, "run", run)
    monkeypatch.setattr(media, "run", run)
    monkeypatch.setattr(ar_news, "company_name", lambda symbol: None)
    return answers


@pytest.fixture
def today(monkeypatch):
    """The machine's date is 30 Oct 2026, at 10:00 in New York (19:30 in India)."""
    now = datetime(2026, 10, 30, 14, 0, tzinfo=UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)

    monkeypatch.setattr(date_window, "datetime", Clock)
    monkeypatch.setattr(date_window, "get_current_date", lambda: "2026-10-30")


EVENING = "2026-10-29T21:00:00"     # 29 Oct 21:00, no zone: 30 Oct 02:30 in India if it was UTC


@pytest.mark.unit
@pytest.mark.parametrize("symbol", ["RELIANCE.NS", "AAPL", "BTC-USD"])
def test_a_current_run_shows_a_time_with_no_zone_on_the_date_it_states(web, today, symbol):
    web["*"] = [_item("Reliance board approves bonus issue", "https://news.example.in/a", EVENING)]
    web["page"] = web["*"][0]
    out = ar_news.get_news_agent_reach(symbol, "2026-10-23", "2026-10-30")
    assert "### Reliance board approves bonus issue (source: news.example.in, published 2026-10-29)" in out
    # The same date as the page itself, read through read_web_page.
    with run_config({**get_config(), "run_symbol": symbol}):
        page = media.read_web_page("https://news.example.in/a", "")
    assert "Published: 2026-10-29 21:00 (as the page states, with no time zone)" in page


@pytest.mark.unit
def test_a_current_nse_runs_global_news_shows_a_time_with_no_zone_on_the_date_it_states(web, today):
    web["*"] = [_item("Rupee ends lower against the dollar", "https://news.example.in/rupee", EVENING)]
    base = get_config()
    with run_config({**base, "run_symbol": "RELIANCE.NS", "global_news_groups": True,
                     "agent_reach_india_queries": ["rupee dollar exchange rate"], "global_news_queries": []}):
        out = ar_news.get_global_news_agent_reach("2026-10-30", 7, 5)
    assert "#### Rupee ends lower against the dollar (source: news.example.in, published 2026-10-29)" in out


@pytest.mark.unit
def test_a_past_nse_run_still_does_not_read_a_time_with_no_zone_that_may_be_after_it(web, today):
    # Shown on the date it states, but counted no earlier than its UTC reading: 29 Oct in India.
    # Dated two days before the machine's date, the run is past even where the listing's date
    # is a day behind the machine's.
    web["*"] = [_item("Reliance board approves bonus issue", "https://news.example.in/a", "2026-10-28T21:00:00")]
    with pytest.raises(NoMarketDataError):
        ar_news.get_news_agent_reach("RELIANCE.NS", "2026-10-21", "2026-10-28")
    out = ar_news.get_news_agent_reach("AAPL", "2026-10-21", "2026-10-28")
    assert "(source: news.example.in, published 2026-10-28)" in out
