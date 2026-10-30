"""News review fixes: a page date of exactly midnight (how Exa writes a page that
states only a date) or with no time zone is not counted, or shown, on the evening
before in New York; an unset page date (0001-01-01, 9999-12-31) is no date and costs
no page; and Polymarket pauses only when it could not be connected to at all, with a
connect timeout that leaves a slow TLS handshake time to finish.

No test reaches the network: Agent-Reach and the clock are faked, and Polymarket
goes through the real requests stack with the connections urllib3 opens faked.
"""

import json
import socket
import threading
from datetime import UTC, date, datetime

import pytest
import urllib3.util.connection

from tradingagents.agents import tools
from tradingagents.dataflows import date_window
from tradingagents.dataflows.config import get_config, run_config
from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.vendors import polymarket
from tradingagents.dataflows.vendors.agent_reach import client, media, news as ar_news

PAST = ("2025-05-02", "2025-05-09")    # a window well before today: the run is dated 2025-05-09
WATCH = "https://www.youtube.com/watch?v=jNQXAC9IVRw"


def _item(title, url, published_at=None, text="body", author=None):
    """An item as Agent-Reach's client returns it."""
    return {"title": title, "url": url, "author": author, "published_at": published_at,
            "text": text, "duration": None, "lang": None}


@pytest.fixture
def web(monkeypatch):
    """Agent-Reach answers a web search from ``answers["*"]``, a page read from
    ``answers["page"]`` and a YouTube call from ``answers[(channel, action)]``."""
    answers = {}

    def run(channel, action, positional, params=None, timeout=None):
        if (channel, action) == ("web", "search"):
            return {"items": list(answers.get("*", []))}
        if (channel, action) == ("web", "read"):
            return {"items": [answers["page"]]}
        return {"items": list(answers.get((channel, action), []))}

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


# ── a page date of midnight, or with no zone, is not the evening before ────

@pytest.mark.unit
@pytest.mark.parametrize(("text", "expected"), [
    ("2026-10-29T00:00:00.000Z", date(2026, 10, 29)),                  # Exa, for a page dated 29 Oct
    ("Thu, 29 Oct 2026 00:00:00 GMT", date(2026, 10, 29)),
    ("2026-10-29T00:00:00+05:30", date(2026, 10, 29)),                 # midnight in the page's own zone
    ("2026-10-28T20:00:00-04:00", datetime(2026, 10, 29, tzinfo=UTC)),  # a time of day, though midnight UTC
    ("2025-05-10T01:00:00", datetime(2025, 5, 10, 1)),                 # no zone: the page's wall time
])
def test_a_page_date_at_midnight_is_a_date_and_one_with_no_zone_keeps_its_wall_time(text, expected):
    published = client.published_date(text)
    assert published == expected and type(published) is type(expected)


@pytest.mark.unit
def test_a_past_run_reads_no_page_dated_after_it_by_a_midnight_or_zoneless_stamp(web):
    web["*"] = [
        _item("Page dated 10 May", "https://a.test/after", "2025-05-10T00:00:00.000Z"),
        _item("Zone-less 01:00 on 10 May", "https://a.test/naive", "2025-05-10T01:00:00"),
        _item("Inside", "https://a.test/inside", "2025-05-05T10:00:00.000Z"),
    ]
    for symbol in ("AAPL", "RELIANCE.NS"):
        out = ar_news.get_news_agent_reach(symbol, *PAST)
        assert "### Inside (source: a.test, published 2025-05-05)" in out
        assert "Page dated 10 May" not in out and "Zone-less" not in out


@pytest.mark.unit
def test_a_past_us_run_reads_no_fed_statement_dated_the_next_day(web, today):
    web["*"] = [
        _item("Federal Reserve issues FOMC statement",
              "https://www.federalreserve.gov/newsevents/pressreleases/monetary20261029a.htm",
              "2026-10-29T00:00:00.000Z", "The Committee decided to lower the target range."),
        _item("Treasury yields edge up before Fed decision", "https://news.example.com/yields",
              "2026-10-27T15:00:00.000Z", "Yields rose."),
    ]
    base = get_config()
    with run_config({**base, "run_symbol": "AAPL", "global_news_queries": ["Federal Reserve interest rates"],
                     "data_vendors": {**base["data_vendors"], "news_data": "agent_reach,yfinance"}}):
        out = tools.get_global_news.func(curr_date="2026-10-28", trade_date="2026-10-28", symbol="AAPL")
    assert "### Treasury yields edge up before Fed decision (source: news.example.com, published 2026-10-27)" in out
    assert "FOMC" not in out and "lower the target range" not in out


@pytest.mark.unit
def test_a_current_us_run_shows_todays_date_only_page_as_today(web, today):
    web["*"] = [_item("Apple reports fourth quarter results", "https://www.apple.com/newsroom/x",
                      "2026-10-30T00:00:00.000Z")]
    out = ar_news.get_news_agent_reach("AAPL", "2026-10-23", "2026-10-30")
    assert "### Apple reports fourth quarter results (source: apple.com, published 2026-10-30)" in out


@pytest.mark.unit
def test_a_time_with_no_zone_counts_no_earlier_than_its_date_or_its_utc_reading(web):
    web["*"] = [_item("Board meeting ends", "https://in.example/board", "2025-05-09T22:00:00")]
    # Read as UTC, 22:00 is 03:30 on 10 May in India: a past NSE run dated the 9th does not read it.
    with pytest.raises(NoMarketDataError):
        ar_news.get_news_agent_reach("RELIANCE.NS", *PAST)
    # Nor is it counted later than either reading: in New York it is the 9th it states.
    out = ar_news.get_news_agent_reach("AAPL", *PAST)
    assert "### Board meeting ends (source: in.example, published 2025-05-09)" in out


@pytest.mark.unit
def test_a_page_dated_at_midnight_or_with_no_zone_is_shown_as_the_page_states_it(web):
    web["page"] = _item("FOMC statement", "https://www.federalreserve.gov/x", "2026-10-29T00:00:00.000Z", "Body")
    for symbol in ("AAPL", "RELIANCE.NS", None):
        with run_config({**get_config(), "run_symbol": symbol}):
            out = media.read_web_page("https://www.federalreserve.gov/x", "")
        assert "Published: 2026-10-29 (as the page states)" in out

    web["page"] = _item("Stocks in news", "https://news.example.com/a", "2026-10-30T01:00:00", "Body")
    for symbol in ("AAPL", "RELIANCE.NS"):
        with run_config({**get_config(), "run_symbol": symbol}):
            out = media.read_web_page("https://news.example.com/a", "")
        assert "Published: 2026-10-30 01:00 (as the page states, with no time zone)" in out


# ── an unset page date is no date, and costs no page ───────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("stamp", [
    "0001-01-01T00:00:00Z", "0001-01-01T00:00:00+00:00", "0001-01-01T00:00:00", "0001-01-01",
    "0001-01-01T10:00:00Z", "9999-12-31T23:00:00Z", "9999-12-31",
])
def test_an_unset_page_date_is_no_date(stamp):
    assert client.published_date(stamp) is None


@pytest.mark.unit
@pytest.mark.parametrize("stamp", [
    "0001-01-01T00:00:00Z", "0001-01-01T00:00:00+00:00", "0001-01-01T00:00:00", "0001-01-01T10:00:00Z",
    "9999-12-31T23:00:00Z",
])
def test_a_page_with_an_unset_date_is_still_read(web, stamp):
    # Hugo writes 0001-01-01T00:00:00+00:00 (Go's zero time) for a page with no date.
    web["page"] = _item("Apple suppliers", "https://news.example.com/a", stamp, "Body text of the article.")
    for symbol in ("AAPL", "RELIANCE.NS", None):
        with run_config({**get_config(), "run_symbol": symbol}):
            out = media.read_web_page("https://news.example.com/a", "")
        assert "> Body text of the article." in out and "Published" not in out


@pytest.mark.unit
def test_a_story_or_video_with_an_unset_date_is_undated_in_a_current_run(web, today):
    web["*"] = [_item("Unset date", "https://b.test/unset", "0001-01-01T00:00:00Z"),
                _item("Good story", "https://a.test/good", "2026-10-29T15:00:00Z")]
    out = ar_news.get_news_agent_reach("AAPL", "2026-10-23", "2026-10-30")
    assert "### Unset date (source: b.test) (date not given)" in out
    assert "### Good story (source: a.test, published 2026-10-29)" in out

    web[("youtube", "read")] = [_item("Apple Q4", WATCH, "0001-01-01T10:00:00Z", author="CNBC")]
    web[("youtube", "subtitle")] = [_item("Apple Q4", WATCH, None, "captions")]
    with run_config({**get_config(), "run_symbol": "AAPL"}):
        out = media.get_video_transcript(WATCH, "2026-10-30")
    assert "channel: CNBC; upload date not given" in out and "> captions" in out


# ── Polymarket pauses only when it could not connect at all ────────────────

_BODY = json.dumps({"events": []}).encode()
ANSWER = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s" % (len(_BODY), _BODY)
DROP = b""                                       # the server closes the connection without answering
NOT_TLS = b"HTTP/1.1 400 Bad Request\r\n\r\n"    # an https request answered in plain text: the handshake fails


@pytest.fixture
def gamma(monkeypatch, tmp_path):
    """Polymarket through the real requests stack, with each connection urllib3 opens
    faked. ``plan`` says what each does in turn: an error raised while connecting, or
    else the bytes the server sends (ANSWER, DROP, NOT_TLS); ``opened`` records each
    one's address and connect timeout. Only this test's thread is served: a connection
    another thread opens meanwhile is refused, so it can neither take a planned answer
    nor count as opened."""
    plan, opened, servers = [], [], []
    owner = threading.get_ident()

    def create_connection(address, timeout=None, *args, **kwargs):
        if threading.get_ident() != owner:
            raise OSError(f"test tried to reach the network: {address}")
        if not plan:
            raise AssertionError(f"unplanned connection to {address}; opened before it: {opened}")
        opened.append((address, timeout))
        outcome = plan.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        ours, theirs = socket.socketpair()
        ours.settimeout(timeout)
        theirs.sendall(outcome)
        servers.append(theirs)
        if outcome == DROP:
            theirs.close()
        return ours

    monkeypatch.setattr(urllib3.util.connection, "create_connection", create_connection)
    monkeypatch.setattr(polymarket, "GAMMA_BASE", "http://gamma.test")
    monkeypatch.setattr(polymarket, "_connect_failure", None)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "*")                        # nor the system's proxy settings
    monkeypatch.setenv("NETRC", str(tmp_path / "netrc"))     # nor the user's ~/.netrc
    yield plan, opened
    for server in servers:
        server.close()


@pytest.mark.unit
def test_polymarket_is_asked_again_after_a_server_it_reached_dropped_the_connection(gamma):
    plan, opened = gamma
    plan += [DROP, ANSWER]
    assert "unavailable" in polymarket.get_prediction_markets("RBI rate cut")
    second = polymarket.get_prediction_markets("Fed rate cut December")
    assert "No open prediction markets matched 'Fed rate cut December'" in second
    assert len(opened) == 2


@pytest.mark.unit
def test_polymarket_is_asked_again_after_a_failed_tls_handshake(gamma, monkeypatch):
    # It failed at once, so a pause would save no waiting.
    plan, opened = gamma
    monkeypatch.setattr(polymarket, "GAMMA_BASE", "https://gamma.test")
    plan += [NOT_TLS, NOT_TLS]
    for topic in ("RBI rate cut", "Fed rate cut December"):
        assert "unavailable" in polymarket.get_prediction_markets(topic)
    assert len(opened) == 2


@pytest.mark.unit
@pytest.mark.parametrize("failure", [
    ConnectionRefusedError(61, "Connection refused"),
    socket.gaierror(8, "nodename nor servname provided, or not known"),
    TimeoutError("timed out"),
], ids=["refused", "name-not-found", "connect-timeout"])
def test_polymarket_pauses_after_it_could_not_connect(gamma, failure):
    plan, opened = gamma
    plan += [failure, ANSWER]
    assert "unavailable" in polymarket.get_prediction_markets("RBI rate cut")
    assert "not tried again" in polymarket.get_prediction_markets("Fed rate cut December")
    assert len(opened) == 1


@pytest.mark.unit
def test_polymarket_pauses_when_its_proxy_cannot_be_reached_but_not_when_it_drops(gamma, monkeypatch):
    plan, opened = gamma
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name)
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.test:3128")
    plan += [DROP, ConnectionRefusedError(61, "Connection refused"), ANSWER]
    polymarket.get_prediction_markets("RBI rate cut")              # the proxy took the request, then dropped it
    polymarket.get_prediction_markets("Fed rate cut December")     # the proxy refused the connection
    assert "not tried again" in polymarket.get_prediction_markets("India recession")
    assert [address for address, _ in opened] == [("proxy.test", 3128)] * 2


@pytest.mark.unit
def test_polymarket_leaves_a_slow_tls_handshake_time_to_finish(gamma):
    # urllib3 runs the TLS handshake (and a proxy's CONNECT) under the connect timeout:
    # at 5 s, a working Polymarket that took 7 s to shake hands was never read.
    plan, opened = gamma
    plan.append(ANSWER)
    assert "No open prediction markets matched" in polymarket.get_prediction_markets("Fed rate cut December")
    [(_, connect_timeout)] = opened
    assert connect_timeout >= 10
