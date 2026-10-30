"""NSE/BSE listings: global news in an India and a Global group, a note on insider
prices that do not fit the stock's closes, and the quarterly cash-flow gap.

yf.Search, yf.Ticker and load_ohlcv are stubbed; no test reaches Yahoo.
"""

from datetime import UTC, datetime
from unittest import mock

import pandas as pd
import pytest

import tradingagents.dataflows.vendors.yahoo.news as ynews
from tradingagents.agents import tools
from tradingagents.dataflows.date_window import get_current_date
from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.router import no_data_available
from tradingagents.dataflows.vendors.yahoo import fundamentals
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.markets import india

AS_OF = "2025-05-09"


def _article(title, day="2025-05-08", related=(), summary=""):
    epoch = int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC).timestamp())
    return {"title": title, "publisher": "P", "link": "l", "summary": summary,
            "providerPublishTime": epoch, "relatedTickers": list(related)}


@pytest.fixture
def search(monkeypatch):
    """Answer each Yahoo search from ``pages[query]``, any other query from ``pages["*"]``;
    ``calls`` records each (query, fuzzy)."""
    pages, calls = {}, []

    class FakeSearch:
        def __init__(self, query, news_count=10, enable_fuzzy_query=True, **k):
            calls.append((query, enable_fuzzy_query))
            self.news = list(pages.get(query, pages.get("*", [])))

    monkeypatch.setattr(ynews.yf, "Search", FakeSearch)
    return pages, calls


def _global_news(symbol, limit=10, as_of=AS_OF):
    with tools._news_searches_for(symbol):
        return ynews.get_global_news_yfinance(as_of, 7, limit)


def _section(out, label):
    """The text of one ``### label`` section."""
    return out.split(f"### {label}\n", 1)[1].split("\n### ", 1)[0]


# --- global news -----------------------------------------------------------------

@pytest.mark.unit
def test_the_india_group_keeps_india_stories_and_drops_unrelated_ones(search):
    pages, _ = search
    pages["India"] = [
        _article("Tata Communications to Build India's AI Super Highway"),
        _article("USA Rare Earth soars on Pentagon deal", related=["USAR"]),
        _article("Jio Platforms breaks IPO record", related=["RELIANCE.NS"]),
        _article("Refiners squeezed by crude", summary="The Indian rupee fell to a record low."),
        _article("RBI holds rates as the rupee firms"),            # two weak terms
        _article("SEBIRA Holdings and BSEL Corp rally"),          # no whole-word SEBI or BSE
        _article("SEBI tightens F&O rules"),
    ]
    pages["*"] = [_article("Fed holds rates")]
    india_part = _section(_global_news("RELIANCE.NS"), "India")

    for kept in ("Tata Communications", "Jio Platforms", "Refiners squeezed", "RBI holds rates", "SEBI tightens"):
        assert kept in india_part
    assert "USA Rare Earth" not in india_part and "SEBIRA" not in india_part


@pytest.mark.unit
@pytest.mark.parametrize("title, related", [
    ("Pakistani rupee slips to record low against dollar", ()),     # another country's rupee
    ("Austria's RBI to wind down Russian unit", ("RBI.VI",)),       # Raiffeisen Bank International
    ("RBI Asia Trailblazer Awards open entries", ()),               # Retail Banker International
    ("Indiana Michigan Power to build gas plant", ()),
    ("Eli Lilly, the Indianapolis drugmaker, beats estimates", ()),
    ("Apple's nifty new AirPods feature", ()),
])
def test_a_term_that_also_names_something_outside_india_is_not_enough(search, title, related):
    pages, _ = search
    pages["rupee"] = [_article(title, related=related)]
    pages["*"] = [_article("Fed holds rates")]
    india_part = _section(_global_news("RELIANCE.NS"), "India")
    assert title not in india_part and india.NEWS_GAP in india_part


@pytest.mark.unit
@pytest.mark.parametrize("tag", ["^NSEI", "^BSESN", "^NSEBANK", "^INDIAVIX", "INR=X", "USDINR=X"])
def test_a_story_tagged_with_an_indian_index_or_the_rupee_is_kept(search, tag):
    pages, _ = search
    pages["rupee"] = [_article("Bond yields ease", related=[tag])]
    assert "Bond yields ease" in _section(_global_news("RELIANCE.NS"), "India")


@pytest.mark.unit
@pytest.mark.parametrize("title", ["INDIA GDP BEATS", "indian equities slip", "sebi widens F&O curbs"])
def test_india_terms_match_in_any_case(search, title):
    pages, _ = search
    pages["India"] = [_article(title)]
    assert title in _section(_global_news("RELIANCE.NS"), "India")


@pytest.mark.unit
def test_each_group_is_searched_with_its_own_fuzzy_setting(search):
    _, calls = search
    _global_news("TCS.BO")
    india_calls = [c for c in calls if c[0] in ("rupee", "RBI", "India")]
    assert india_calls == [("rupee", False), ("RBI", False), ("India", False)]
    assert [c for c in calls if c not in india_calls] == [
        (q, True) for q in DEFAULT_CONFIG["global_news_queries"]]


@pytest.mark.unit
def test_the_india_fuzzy_setting_is_configurable(search):
    _, calls = search
    from tradingagents.dataflows.config import get_config, run_config

    with run_config({**get_config(), "global_news_india_fuzzy": True}):
        _global_news("RELIANCE.NS")
    assert ("rupee", True) in calls


@pytest.mark.unit
@pytest.mark.parametrize("n_india, n_world, limit, shown", [
    (5, 5, 5, (3, 2)),      # odd limit: India takes the larger half
    (5, 5, 4, (2, 2)),
    (1, 10, 5, (1, 4)),     # India's unused share goes to Global
    (10, 1, 5, (4, 1)),     # and Global's to India
    (10, 0, 5, (5, 0)),
])
def test_the_article_limit_is_split_between_the_groups(search, n_india, n_world, limit, shown):
    pages, _ = search
    pages["rupee"] = [_article(f"India story {i}") for i in range(n_india)]
    pages["*"] = [_article(f"World story {i}") for i in range(n_world)]
    out = _global_news("RELIANCE.NS", limit=limit)
    assert _section(out, "India").count("#### India story") == shown[0]
    assert _section(out, "Global").count("#### World story") == shown[1]


@pytest.mark.unit
def test_a_story_found_by_both_groups_is_shown_once(search):
    pages, _ = search
    pages["India"] = [_article("Oil prices hit India's refiners")]
    pages["*"] = [_article("Oil prices hit India's refiners"), _article("Fed holds rates")]
    out = _global_news("RELIANCE.NS")
    assert out.count("Oil prices hit India's refiners") == 1
    assert "Oil prices" in _section(out, "India") and "Fed holds rates" in _section(out, "Global")


@pytest.mark.unit
def test_a_group_whose_only_stories_an_earlier_group_found_says_so(search):
    pages, _ = search
    pages["India"] = [_article("Oil prices hit India's refiners")]
    pages["*"] = [_article("Oil prices hit India's refiners")]
    global_part = _section(_global_news("RELIANCE.NS"), "Global")
    assert "other than ones the searches above had already found" in global_part
    assert "returned no Global stories in this window." not in global_part


@pytest.mark.unit
def test_a_group_the_limit_leaves_no_room_does_not_claim_it_found_nothing(search):
    pages, _ = search
    pages["rupee"] = [_article("Rupee slides", related=["INR=X"])]
    pages["*"] = [_article("Fed holds rates"), _article("Oil slips")]
    out = _global_news("RELIANCE.NS", limit=1)
    assert "Rupee slides" in _section(out, "India")
    global_part = _section(out, "Global")
    assert "2 Global stories found in this window; none shown within the article limit of 1." in global_part
    assert "returned no Global stories" not in global_part


@pytest.mark.unit
def test_out_of_window_india_stories_do_not_spend_the_budget(search):
    # #1356 and #1007 hold per group: old and future stories neither count nor show.
    pages, _ = search
    pages["rupee"] = [_article(f"India old {i}", day="2025-01-01") for i in range(5)]
    pages["RBI"] = [_article("India future", day="2025-06-01")]
    pages["India"] = [_article("India in window")]
    pages["*"] = []
    out = _global_news("RELIANCE.NS", limit=2)
    assert "India in window" in out and "India old" not in out and "India future" not in out


@pytest.mark.unit
def test_an_empty_india_group_says_what_is_not_covered(search):
    pages, _ = search
    pages["India"] = [_article("Micron beats estimates", related=["MU"])]
    pages["*"] = [_article("Fed holds rates")]
    out = _global_news("RELIANCE.NS")
    assert out.startswith("## Global Market News, from 2025-05-02 to 2025-05-09:")
    assert india.NEWS_GAP in _section(out, "India")
    assert "Micron" not in out and "Fed holds rates" in _section(out, "Global")


@pytest.mark.unit
def test_no_story_in_either_group_is_a_coverage_gap(search):
    pages, _ = search
    pages["*"] = [_article("Old", day="2025-01-01")]
    out = _global_news("RELIANCE.NS")
    assert "###" not in out and "unavailable" in out and "not an absence" in out


@pytest.mark.unit
def test_a_us_listing_is_searched_and_reported_as_before(search):
    pages, calls = search
    pages["*"] = [_article("Fed holds rates", related=["RELIANCE.NS"]), _article("Oil slips")]
    out = _global_news("AAPL")
    assert calls == [(q, True) for q in DEFAULT_CONFIG["global_news_queries"]]
    assert out == ("## Global Market News, from 2025-05-02 to 2025-05-09:\n\n"
                   "### Fed holds rates (source: P)\nLink: l\n\n"
                   "### Oil slips (source: P)\nLink: l\n\n")


# --- insider transactions ----------------------------------------------------------

def _insider(texts, days_ago=(20, 10)):
    today = pd.Timestamp(get_current_date())
    return pd.DataFrame({
        "Shares": [835479] * len(texts),
        "Value": [13104571] * len(texts),
        "Text": texts,
        "Start Date": [today - pd.Timedelta(days=d) for d in days_ago[:len(texts)]],
    })


def _closes(low=1359.7, high=1419.6):
    dates = pd.bdate_range(end=get_current_date(), periods=60)
    closes = [low + (high - low) * i / 59 for i in range(60)]
    return pd.DataFrame({"Date": dates, "Close": closes})


def _insider_report(ticker, frame, load):
    with mock.patch.object(fundamentals.yf, "Ticker", return_value=mock.Mock(insider_transactions=frame)), \
            mock.patch.object(fundamentals, "load_ohlcv", side_effect=load) as loaded:
        return fundamentals.get_insider_transactions(ticker, get_current_date()), loaded


@pytest.mark.unit
def test_insider_prices_far_from_the_inr_closes_get_a_note():
    frame = _insider(["Sale at price 15.69 per share.", "Sale at price 14.04 per share."])
    out, _ = _insider_report("RELIANCE.NS", frame, lambda *a, **k: _closes())
    assert out.startswith("# Insider Transactions data for RELIANCE.NS\n")
    assert "Yahoo does not state the currency" in out
    bars = _closes()
    span = bars[(bars["Date"] >= frame["Start Date"].min() - pd.Timedelta(days=7))
                & (bars["Date"] <= frame["Start Date"].max())]["Close"]
    assert "prices (14.04-15.69)" in out
    assert f"INR closes over the same period ({span.min():.2f}-{span.max():.2f})" in out
    assert "not rupee amounts" in out and "SEBI's insider-trading (PIT) regulations" in out
    assert "USD" not in out and "dollar" not in out


@pytest.mark.unit
def test_insider_prices_that_fit_the_closes_get_no_note():
    frame = _insider(["Sale at price 1,402.50 per share.", "Purchase at price 1380 per share."])
    out, _ = _insider_report("RELIANCE.NS", frame, lambda *a, **k: _closes())
    assert "Note:" not in out


@pytest.mark.unit
def test_a_price_written_with_commas_is_read_whole():
    frame = _insider(["Sale at price 1,523.40 per share."])
    out, _ = _insider_report("MRF.NS", frame, lambda *a, **k: _closes(129000, 131000))
    assert "prices (1523.40-1523.40)" in out and "not rupee amounts" in out


@pytest.mark.unit
@pytest.mark.parametrize("price, noted", [(600, False), (400, True), (1900, False), (2100, True)])
def test_insider_prices_are_held_to_half_the_lowest_and_twice_the_highest_close(price, noted):
    frame = _insider([f"Sale at price {price} per share."])
    out, _ = _insider_report("RELIANCE.NS", frame, lambda *a, **k: _closes(1000, 1000))
    assert ("Note:" in out) is noted


@pytest.mark.unit
def test_insider_rows_without_a_price_get_no_note_and_load_no_closes():
    frame = _insider(["Stock Gift", "Sale at price 0.00 per share."])
    out, loaded = _insider_report("RELIANCE.NS", frame, lambda *a, **k: _closes())
    assert "Note:" not in out and not loaded.called


@pytest.mark.unit
def test_insider_rows_keep_the_currency_caveat_when_closes_cannot_be_loaded():
    """Without closes the prices cannot be checked, which the note says instead of
    dropping the caveat (a BSE listing with no daily history lost it this way)."""
    frame = _insider(["Sale at price 15.69 per share."])

    def broken(*a, **k):
        raise NoMarketDataError("RELIANCE.NS", detail="no price rows")

    out, loaded = _insider_report("RELIANCE.NS", frame, broken)
    assert loaded.called
    assert out.startswith("# Insider Transactions data for RELIANCE.NS\n" + frame.to_csv() + "\nNote: ")
    assert "Yahoo does not state the currency" in out and "not assume they are rupee amounts" in out
    assert "do not match" not in out


@pytest.mark.unit
def test_a_us_listing_gets_no_insider_check():
    frame = _insider(["Sale at price 15.69 per share."])
    out, loaded = _insider_report("AAPL", frame, lambda *a, **k: _closes())
    assert out == "# Insider Transactions data for AAPL\n" + frame.to_csv() and not loaded.called


@pytest.mark.unit
def test_closes_are_read_no_later_than_the_run_date():
    frame = _insider(["Sale at price 15.69 per share."])
    _, loaded = _insider_report("RELIANCE.NS", frame, lambda *a, **k: _closes())
    symbol, as_of = loaded.call_args.args[:2]
    # As of the last trade, not the run date itself: a history that stops between the
    # two is not refused as stale (tests/test_review2_fundamentals.py).
    assert symbol == "RELIANCE.NS" and as_of <= get_current_date()


# --- quarterly cash flow -------------------------------------------------------------

def _cashflow(ticker, freq, routed):
    with mock.patch.object(tools, "route_to_vendor", return_value=routed):
        return tools.get_cashflow.func(ticker=ticker, freq=freq, curr_date=None, trade_date="")


def _no_data(ticker):
    return no_data_available(NoMarketDataError(ticker, detail="no cash flow data"))


@pytest.mark.unit
def test_no_quarterly_cash_flow_for_an_indian_listing_is_explained():
    out = _cashflow("RELIANCE.NS", "quarterly", _no_data("RELIANCE.NS"))
    assert out.startswith("NO_DATA_AVAILABLE: ")
    assert "'RELIANCE.NS'" in out and "half-yearly" in out and "SEBI LODR" in out
    assert 'freq="annual"' in out and "may be invalid" not in out


@pytest.mark.unit
@pytest.mark.parametrize("ticker, freq", [("AAPL", "quarterly"), ("RELIANCE.NS", "annual"),
                                          ("^NSEI", "quarterly"), ("^BSESN", "quarterly"),
                                          ("NIFTY_FIN_SERVICE.NS", "quarterly")])
def test_other_cash_flow_gaps_keep_the_usual_message(ticker, freq):
    routed = _no_data(ticker)
    assert _cashflow(ticker, freq, routed) == routed


@pytest.mark.unit
@pytest.mark.parametrize("routed", ["# Cash Flow data for RELIANCE.NS (quarterly)\n,2026-09-30\n",
                                    "DATA_UNAVAILABLE: no configured vendor could serve get_cashflow"])
def test_quarterly_cash_flow_data_or_an_outage_is_passed_through(routed):
    assert _cashflow("RELIANCE.NS", "quarterly", routed) == routed
