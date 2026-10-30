"""News fixes: a window's days are the listing's, a web page is read past its
menus, a section Yahoo cannot reach says so, dates are shown in the listing's zone,
the sentiment analyst names its news source, a misconfigured vendor says why, the
India section does not hang on Yahoo's India searches, and Polymarket is not asked
again right after it failed to connect.

No test reaches the network: Yahoo, Agent-Reach, the social feeds, Polymarket and
the clock are faked.
"""

import json
import logging
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

import pytest
import requests

from tradingagents.agents import tools
from tradingagents.agents.analysts import sentiment_analyst
from tradingagents.dataflows import date_window, router
from tradingagents.dataflows.config import get_config, run_config, set_config
from tradingagents.dataflows.errors import VendorNotConfiguredError
from tradingagents.dataflows.vendors import polymarket, reddit, stocktwits
from tradingagents.dataflows.vendors.agent_reach import client, media, news as ar_news
from tradingagents.dataflows.vendors.yahoo import news as ynews

PAST = ("2025-05-02", "2025-05-09")    # a window well before today: the run is dated 2025-05-09
WATCH = "https://www.youtube.com/watch?v=jNQXAC9IVRw"


def _item(title, url, published_at=None, text="body", author=None):
    """An item as Agent-Reach's client returns it."""
    return {"title": title, "url": url, "author": author, "published_at": published_at,
            "text": text, "duration": None, "lang": None}


@pytest.fixture
def web(monkeypatch):
    """Agent-Reach answers a search for ``query`` from ``answers[query]`` (else
    ``answers["*"]``), a page read from ``answers["page"]`` and a YouTube call from
    ``answers[(channel, action)]``; ``searched`` records the searches."""
    answers, searched = {}, []

    def run(channel, action, positional, params=None, timeout=None):
        if (channel, action) == ("web", "search"):
            searched.append(positional[0])
            return {"items": list(answers.get(positional[0], answers.get("*", [])))}
        if (channel, action) == ("web", "read"):
            return {"items": [answers["page"]]}
        return {"items": list(answers.get((channel, action), []))}

    monkeypatch.setattr(ar_news, "run", run)
    monkeypatch.setattr(media, "run", run)
    monkeypatch.setattr(ar_news, "company_name", lambda symbol: None)
    return answers, searched


def _yahoo(title, pub_date):
    """A Yahoo news article in the nested ``content`` format."""
    slug = title.lower().replace(" ", "-")
    return {"content": {"title": title, "summary": "", "pubDate": pub_date,
                        "provider": {"displayName": "Wire"},
                        "canonicalUrl": {"url": f"https://finance.yahoo.com/news/{slug}"}}}


def _yahoo_feed(monkeypatch, feed):
    monkeypatch.setattr(ynews.yf, "Ticker", lambda symbol: SimpleNamespace(get_news=lambda count=20: feed))


class _JsonResp:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, *a):
        return self._body


def _epoch(iso):
    return datetime.fromisoformat(iso).timestamp()


# ── a window's days are the listing's ──────────────────────────────────────

@pytest.mark.unit
def test_a_past_nse_run_reads_no_web_story_india_dated_after_the_run(web):
    answers, _ = web
    answers["*"] = [
        _item("Late on the 9th in India", "https://in.example/late", "2025-05-09T18:00:00.000Z"),  # 23:30 IST
        _item("GIFT Nifty signals gap-up", "https://in.example/gift", "2025-05-09T19:45:00.000Z"),  # 01:15 IST, 10 May
        _item("Board approves NCDs", "https://in.example/ncd", "2025-05-09T23:50:00.000Z"),        # 05:20 IST, 10 May
    ]
    out = ar_news.get_news_agent_reach("RELIANCE.NS", *PAST)
    assert "Late on the 9th in India" in out
    assert "GIFT Nifty" not in out and "Board approves" not in out


@pytest.mark.unit
def test_yahoo_news_for_a_past_nse_run_ends_at_midnight_in_india(monkeypatch):
    _yahoo_feed(monkeypatch, [_yahoo("Evening in India", "2025-05-09T15:00:00Z"),            # 20:30 IST
                              _yahoo("GIFT Nifty signals gap-up", "2025-05-09T19:45:00Z")])  # 01:15 IST, 10 May
    out = tools.get_news.func(ticker="RELIANCE.NS", start_date=PAST[0], end_date=PAST[1], trade_date=PAST[1])
    assert "Evening in India" in out and "GIFT Nifty" not in out


@pytest.mark.unit
def test_a_past_us_run_keeps_its_new_york_evening_and_not_the_next_new_york_day(monkeypatch):
    _yahoo_feed(monkeypatch, [_yahoo("After-hours guidance cut", "2025-05-10T01:00:00Z"),   # 21:00 in New York, 9 May
                              _yahoo("Next morning in New York", "2025-05-10T04:30:00Z")])  # 00:30 there, 10 May
    out = ynews.get_news_yfinance("AAPL", *PAST)
    assert "After-hours guidance cut" in out and "Next morning" not in out


@pytest.mark.unit
def test_global_news_for_a_past_nse_run_ends_at_midnight_in_india(monkeypatch):
    articles = [_yahoo("Fed holds rates", "2025-05-09T12:00:00Z"),
                _yahoo("US stocks close higher", "2025-05-09T20:15:00Z")]   # 01:45 IST, 10 May
    monkeypatch.setattr(ynews.yf, "Search", lambda *a, **k: SimpleNamespace(news=articles, quotes=[]))
    with run_config({**get_config(), "run_symbol": "RELIANCE.NS", "global_news_queries": ["markets"]}):
        out = tools.get_global_news.func(curr_date=PAST[1], trade_date=PAST[1], symbol="RELIANCE.NS")
    assert "Fed holds rates" in out and "US stocks close higher" not in out


@pytest.mark.unit
def test_social_posts_for_a_past_nse_run_end_at_midnight_in_india(monkeypatch):
    messages = [{"created_at": "2025-05-09T10:00:00Z", "body": "in the window", "user": {"username": "a"}},
                {"created_at": "2025-05-09T19:45:00Z", "body": "the next day in India", "user": {"username": "b"}}]
    monkeypatch.setattr(stocktwits, "urlopen", lambda *a, **k: _JsonResp({"messages": messages}))
    out = stocktwits.fetch_stocktwits_messages("RELIANCE.NS", start_date=PAST[0], end_date=PAST[1])
    assert "in the window" in out and "the next day in India" not in out

    # Reddit searches the bare symbol ("RELIANCE"); the window is still India's.
    posts = [{"title": "In the window", "created_utc": _epoch("2025-05-09T10:00:00+00:00"),
              "subreddit": "IndianStockMarket"},
             {"title": "The next day in India", "created_utc": _epoch("2025-05-09T19:45:00+00:00"),
              "subreddit": "IndianStockMarket"}]
    monkeypatch.setattr(reddit, "_fetch_subreddit_rss", lambda *a, **k: posts)
    out = reddit.fetch_reddit_posts("RELIANCE.NS", start_date=PAST[0], end_date=PAST[1])
    assert "In the window" in out and "The next day in India" not in out


@pytest.mark.unit
def test_an_undated_item_is_kept_only_until_the_listings_own_midnight(monkeypatch):
    now = datetime(2026, 10, 30, 19, 0, tzinfo=UTC)     # 00:30 on the 31st in India, 15:00 on the 30th in New York

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)

    monkeypatch.setattr(date_window, "datetime", Clock)
    start, end = datetime(2026, 10, 23), datetime(2026, 10, 30)
    assert date_window.in_window(None, start, end, ZoneInfo("Asia/Kolkata")) is False
    assert date_window.in_window(None, start, end, ZoneInfo("America/New_York")) is True
    assert date_window.in_window(None, start, end) is True     # UTC's 30th is not over either


@pytest.mark.unit
def test_the_window_zone_is_the_symbols_listing_else_the_runs():
    assert date_window.window_zone("RELIANCE.NS") == ZoneInfo("Asia/Kolkata")
    assert date_window.window_zone("AAPL") == ZoneInfo("America/New_York")
    assert date_window.window_zone("BTC-USD") is None
    with run_config({**get_config(), "run_symbol": "TCS.BO"}):
        assert date_window.window_zone() == ZoneInfo("Asia/Kolkata")


@pytest.mark.unit
def test_a_page_date_without_a_time_is_a_date_not_midnight_utc():
    assert client.published_date("2026-01-02") == date(2026, 1, 2)
    assert not isinstance(client.published_date("January 2, 2026"), datetime)
    # Nor is exactly midnight UTC: it is how Exa writes a page that states only a date.
    assert client.published_date("2026-01-02T00:00:00Z") == date(2026, 1, 2)


@pytest.mark.unit
def test_a_video_uploaded_the_day_after_a_past_us_run_is_still_refused(web):
    # YouTube gives an upload date with no time; read as midnight UTC it would be
    # 20:00 the evening before in New York and pass a run dated that day.
    answers, _ = web
    answers[("youtube", "read")] = [_item("Apple Q2", WATCH, "2025-05-10", author="CNBC")]
    answers[("youtube", "subtitle")] = [_item("Apple Q2", WATCH, None, "captions")]
    with run_config({**get_config(), "run_symbol": "AAPL"}):
        assert media.get_video_transcript(WATCH, PAST[1]) == f"Refused: {WATCH} was uploaded after {PAST[1]}."
        answers[("youtube", "search")] = [_item("Apple Q2", WATCH)]
        assert "jNQXAC9IVRw" not in media.get_video_coverage("Apple results", PAST[1])


# ── dates are shown in the listing's zone ──────────────────────────────────

@pytest.mark.unit
def test_a_web_story_is_dated_by_the_listings_day_not_utcs(web):
    answers, _ = web
    answers["*"] = [_item("GIFT Nifty signals gap-up", "https://in.example/gift", "2025-05-08T19:45:00.000Z")]
    out = ar_news.get_news_agent_reach("RELIANCE.NS", *PAST)       # 01:15 IST on 9 May
    assert "(source: in.example, published 2025-05-09)" in out


@pytest.mark.unit
def test_a_page_date_is_converted_to_the_listings_zone_and_labelled_so(web):
    answers, _ = web
    answers["page"] = _item("Stocks in news", "https://news.example.com/a", "2026-10-30T02:00:00+05:30", "Body")
    with run_config({**get_config(), "run_symbol": "RELIANCE.NS"}):
        out = media.read_web_page("https://news.example.com/a", "")
    assert "Published: 2026-10-30 02:00 IST" in out and "as the page states" not in out

    out = media.read_web_page("https://news.example.com/a", "")     # no listing: UTC
    assert "Published: 2026-10-29 20:30 UTC" in out

    answers["page"] = _item("Stocks in news", "https://news.example.com/a", "2026-10-30", "Body")
    assert "Published: 2026-10-30 (as the page states)" in media.read_web_page("https://news.example.com/a", "")


# ── a web page is read past its menus ──────────────────────────────────────

@pytest.mark.unit
def test_a_web_page_is_read_past_its_images_ads_and_menu_links(web):
    answers, _ = web
    chrome = "\n\n".join(
        [f"![Image {i}](https://cdn.example.com/img/{i}.jpg)" for i in range(30)]
        + [f"[](https://adclick.example.net/pcs/click?xai={'x' * 150}&adurl=https://ads.example/{i})"
           for i in range(30)]
        + [f'*   [Menu item {i}](https://news.example.com/section/{i} "Menu item {i}")' for i in range(30)]
        + ["[![Logo](https://cdn.example.com/logo.png)](https://news.example.com/)",
           "[Nifty (live)](https://news.example.com/markets/indices/nifty-50(1))"])
    article = "# Stocks in news: RIL, Jio\n\nRIL and Jio are likely to remain in focus today after their results."
    answers["page"] = _item("Stocks in news: RIL, Jio", "https://news.example.com/a", None, f"{chrome}\n\n{article}")
    out = media.read_web_page("https://news.example.com/a", "")
    assert "likely to remain in focus" in out
    assert "adclick" not in out and "cdn.example.com" not in out and "](" not in out
    assert "> *   Menu item 3" in out and "> Nifty (live)" in out       # a link keeps its text


# ── a section Yahoo cannot reach says so ───────────────────────────────────

@pytest.mark.unit
def test_a_global_section_yahoo_cannot_reach_is_unavailable_not_empty(monkeypatch):
    india_story = _yahoo("India's exports rise in April", "2025-05-05T08:00:00Z")
    recent = [_yahoo("Fed signals pause", "2026-10-29T14:00:00Z")]
    monkeypatch.setattr(ynews.yf, "Search", lambda query=None, **k: SimpleNamespace(
        news=([india_story] if query == "India" else []) + recent, quotes=[]))
    out = tools.get_global_news.func(curr_date=PAST[1], trade_date=PAST[1], symbol="RELIANCE.NS")
    india_part, global_part = out.split("### India\n", 1)[1].split("### Global\n", 1)
    assert "India's exports" in india_part
    assert "unavailable" in global_part and "not an absence" in global_part
    assert "returned no Global stories in this window." not in global_part


# ── the sentiment analyst names its news source ────────────────────────────

def _news_heading(news_block):
    message = sentiment_analyst._build_system_message(
        ticker="RELIANCE.NS", start_date="2026-10-23", end_date="2026-10-30",
        news_block=news_block, stocktwits_block="", reddit_block="")
    return message.split("<start_of_news>", 1)[0].rsplit("### ", 1)[1]


@pytest.mark.unit
def test_web_search_results_are_not_labelled_institutional_yahoo_headlines():
    web_block = (f'## RELIANCE.NS News, from 2026-10-23 to 2026-10-30 (Agent-Reach web search (Exa): "x"):\n'
                 f"{client.UNTRUSTED_NOTE}\n\n### Some blog says RIL to double (source: randomblog.example)")
    heading = _news_heading(web_block)
    assert "Yahoo Finance" not in heading and "Institutional" not in heading
    assert "Agent-Reach" in heading and "unverified" in heading

    heading = _news_heading("## RELIANCE.NS News, from 2026-10-23 to 2026-10-30:\n\n### RIL Q2 (source: Reuters)")
    assert "Yahoo Finance" in heading and "Institutional framing" in heading
    set_config({"data_vendors": {"news_data": "alpha_vantage"}})
    assert "Alpha Vantage" in _news_heading('{"feed": []}')


# ── a misconfigured vendor says why ────────────────────────────────────────

@pytest.mark.unit
def test_a_vendor_that_is_not_configured_is_logged_with_its_reason(caplog):
    def misconfigured(*a, **k):
        raise VendorNotConfiguredError("agent_reach_max_results must be a positive whole number")

    set_config({"data_vendors": {"news_data": "agent_reach,yfinance"}})
    with mock.patch.dict(router.VENDOR_METHODS, {"get_news": {"agent_reach": misconfigured,
                                                               "yfinance": lambda *a: "YAHOO NEWS"}}), \
            caplog.at_level(logging.WARNING, logger=router.__name__):
        assert router.route_to_vendor("get_news", "RELIANCE.NS", "2026-10-23", "2026-10-30") == "YAHOO NEWS"
    assert "agent_reach_max_results must be a positive whole number" in caplog.text


# ── the India section does not hang on Yahoo's India searches ──────────────

@pytest.mark.unit
def test_agent_reach_keeps_its_india_section_when_yahoos_india_searches_are_emptied(web):
    answers, searched = web
    answers["RBI monetary policy repo rate"] = [_item("RBI holds repo rate", "https://in.example/rbi",
                                                      "2025-05-08T08:00:00Z")]
    answers["Federal Reserve interest rates inflation"] = [_item("Fed holds rates", "https://us.example/fed",
                                                                 "2025-05-08T18:00:00Z")]
    set_config({"data_vendors": {"news_data": "agent_reach,yfinance"}, "global_news_queries_india": [],
                "agent_reach_india_queries": ["RBI monetary policy repo rate"],
                "global_news_queries": ["Federal Reserve interest rates inflation"]})
    out = tools.get_global_news.func(curr_date=PAST[1], trade_date=PAST[1], symbol="RELIANCE.NS")
    assert searched == ["RBI monetary policy repo rate", "Federal Reserve interest rates inflation"]
    assert "#### RBI holds repo rate" in out.split("### India\n", 1)[1].split("### Global\n", 1)[0]


@pytest.mark.unit
def test_yahoo_says_india_is_not_covered_when_its_india_searches_are_emptied(monkeypatch):
    monkeypatch.setattr(ynews.yf, "Search", lambda *a, **k: SimpleNamespace(
        news=[_yahoo("Fed holds rates", "2025-05-08T12:00:00Z")], quotes=[]))
    set_config({"global_news_queries_india": []})
    out = tools.get_global_news.func(curr_date=PAST[1], trade_date=PAST[1], symbol="RELIANCE.NS")
    india_part, global_part = out.split("### India\n", 1)[1].split("### Global\n", 1)
    assert "(Yahoo news searches: none configured)" in india_part
    assert "report them as unavailable" in india_part and "Fed holds rates" in global_part


# ── Polymarket is not asked again right after it failed to connect ─────────

@pytest.fixture
def gamma(monkeypatch):
    """A clock for polymarket and a fake requests.get; ``calls`` records each request's timeout."""
    clock, calls, answers = [1000.0], [], []
    monkeypatch.setattr(polymarket, "monotonic", lambda: clock[0])
    monkeypatch.setattr(polymarket, "_connect_failure", None)

    def get(url, params=None, timeout=None):
        calls.append(timeout)
        answer = answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(polymarket.requests, "get", get)
    return clock, calls, answers


def _timed_out():
    return requests.exceptions.ConnectTimeout(
        "HTTPSConnectionPool(host='gamma-api.polymarket.com', port=443): Max retries exceeded with url: "
        "/public-search?q=RBI+rate+cut (Caused by ConnectTimeoutError(..., 'Connection timed out. (connect timeout=5)'))")


@pytest.mark.unit
def test_polymarket_is_not_asked_again_soon_after_it_failed_to_connect(gamma):
    clock, calls, answers = gamma
    answers += [_timed_out(), _timed_out()]
    first = polymarket.get_prediction_markets("RBI rate cut")
    second = polymarket.get_prediction_markets("Fed rate cut December")
    assert len(calls) == 1
    assert "unavailable" in first and "unavailable" in second and "'Fed rate cut December'" in second
    assert "RBI+rate+cut" not in second     # the first request's address is not repeated for another topic
    connect, read = calls[0]
    assert connect <= 10 and read >= connect

    clock[0] += polymarket.RETRY_AFTER       # once the pause is over it is asked again
    polymarket.get_prediction_markets("India recession")
    assert len(calls) == 2


@pytest.mark.unit
def test_polymarket_answering_with_an_error_is_asked_again(gamma):
    _, calls, answers = gamma

    def refused():
        raise requests.HTTPError("503 Server Error")

    answers += [SimpleNamespace(raise_for_status=refused), SimpleNamespace(raise_for_status=refused)]
    for topic in ("RBI rate cut", "Fed rate cut December"):
        assert "unavailable" in polymarket.get_prediction_markets(topic)
    assert len(calls) == 2
