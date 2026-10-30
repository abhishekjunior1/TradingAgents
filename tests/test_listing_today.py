"""A run's "today" follows the listing's clock where the machine's date has moved past it.

On a machine in India the date turns at midnight while the US session that opened
the evening before still trades, so until New York's own midnight a US run dated
New York's day is a current run, not a past one: it is served live profiles and
upcoming events, reads undated news and live pages, and has no lessons filtered.
Today is never later than the machine's date, so nothing newer can be let in.

Every test that reads a date pins its clock: the machine's date
(``get_current_date``) and the instant the listings' dates are read at.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from tradingagents.agents.context import build_instrument_context
from tradingagents.arena import refusal
from tradingagents.dataflows import date_window, symbols
from tradingagents.dataflows.config import get_config, run_config
from tradingagents.dataflows.vendors.agent_reach import client, media
from tradingagents.dataflows.vendors.yahoo import corporate_actions, ohlcv
from tradingagents.graph.trading_graph import TradingAgentsGraph

IST = ZoneInfo("Asia/Kolkata")
# 01:00 on Friday 30 Oct 2026 in India: 15:30 on Thursday in New York, whose session trades until 16:00.
NIGHT = datetime(2026, 10, 30, 1, 0, tzinfo=IST)


def _clock(monkeypatch, machine_date: str, at: datetime):
    """Pin the machine's date, and the instant every listing's own date is read at."""
    real = date_window.listing_date
    monkeypatch.setattr(date_window, "get_current_date", lambda: machine_date)
    monkeypatch.setattr(date_window, "listing_date", lambda symbol, now=None: real(symbol, now or at))


@pytest.fixture
def night(monkeypatch):
    """A machine in India at 01:00 on Friday: its date is the 30th, New York's still the 29th."""
    _clock(monkeypatch, "2026-10-30", NIGHT)


# --- whose clock --------------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("symbol, zone", [
    ("AAPL", "America/New_York"), ("aapl", "America/New_York"), ("BRK-B", "America/New_York"),
    ("RELIANCE.NS", "Asia/Kolkata"), ("NSE:RELIANCE", "Asia/Kolkata"), ("TCS.BO", "Asia/Kolkata"),
    ("^NSEI", "Asia/Kolkata"), ("NIFTY 50", "Asia/Kolkata"),
    # The machine's own date: other markets' listings and indices, crypto, forex, futures.
    ("VOD.L", None), ("^GSPC", None), ("BTC-USD", None), ("EURUSD=X", None), ("GC=F", None),
    ("", None), (None, None),
])
def test_a_listing_s_day_is_its_exchange_s_or_new_york_s(symbol, zone):
    found = symbols.listing_zone(symbol)
    assert (found.key if found else None) == zone
    assert ohlcv.listing_zone is symbols.listing_zone      # the price cache judges its day the same way


@pytest.mark.unit
def test_a_listing_s_date_is_read_on_its_own_clock():
    assert date_window.listing_date("AAPL", NIGHT) == "2026-10-29"
    assert date_window.listing_date("RELIANCE.NS", NIGHT) == "2026-10-30"
    assert date_window.listing_date("VOD.L", NIGHT) is None


@pytest.mark.unit
def test_a_us_run_dated_new_york_s_day_stays_current_after_midnight_in_india(night):
    assert date_window.today_for("AAPL") == "2026-10-29"
    assert not date_window.is_historical("2026-10-29", "AAPL")
    assert date_window.is_historical("2026-10-28", "AAPL")
    # India's date is the machine's, and a listing with no known zone keeps the machine's.
    assert date_window.is_historical("2026-10-29", "RELIANCE.NS")
    assert date_window.is_historical("2026-10-29", "VOD.L")
    assert date_window.is_historical("2026-10-29")
    assert date_window.get_current_date() == "2026-10-30"


@pytest.mark.unit
def test_without_a_symbol_the_run_s_listing_counts(night):
    with run_config({"run_symbol": "AAPL"}):
        assert not date_window.is_historical("2026-10-29")
        assert date_window.is_historical("2026-10-29", "RELIANCE.NS")     # a symbol given wins
    assert date_window.is_historical("2026-10-29")


@pytest.mark.unit
def test_today_is_never_later_than_the_machine_s_date(monkeypatch):
    # 23:30 on Thursday on a machine in New York: India is already on Friday.
    _clock(monkeypatch, "2026-10-29", datetime(2026, 10, 29, 23, 30, tzinfo=ZoneInfo("America/New_York")))
    assert date_window.listing_date("RELIANCE.NS") == "2026-10-30"
    assert date_window.today_for("RELIANCE.NS") == "2026-10-29"
    assert not date_window.is_historical("2026-10-29", "RELIANCE.NS")


# --- the run's listing reaches its tools ----------------------------------------------------------

def _graph():
    graph = object.__new__(TradingAgentsGraph)
    graph.config = {"max_debate_rounds": 1, "max_risk_discuss_rounds": 1}
    graph.selected_analysts = ("market",)
    graph._checkpointer_ctx = None
    return graph


class _Stream:
    """A compiled graph whose one step reports the listing its tools would read."""

    def stream(self, graph_input, **kwargs):
        yield (), "values", {"messages": [], "run_symbol": get_config().get("run_symbol")}


@pytest.mark.unit
def test_propagate_serves_the_run_s_listing_without_changing_its_signature_or_settings():
    graph = _graph()
    signature, settings = graph._run_signature("stock"), graph.run_settings()
    seen = []
    graph._run_graph = lambda *a, **k: seen.append(get_config().get("run_symbol")) or ({}, "Hold")

    graph.propagate("AAPL", "2026-09-01")

    assert seen == ["AAPL"] and "run_symbol" not in graph.config
    assert graph._run_signature("stock") == signature and graph.run_settings() == settings


@pytest.mark.unit
def test_a_streamed_run_serves_its_listing_fresh_and_resumed():
    graph = _graph()
    graph.graph = _Stream()
    fresh = [state["run_symbol"] for _, state in graph.stream_run({"company_of_interest": "AAPL", "messages": []})]
    assert fresh == ["AAPL"]

    # The CLI resumes a checkpoint by streaming None, after begin_checkpoint is given the ticker.
    graph.begin_checkpoint("RELIANCE.NS", "2026-10-29", "stock")
    resumed = [state["run_symbol"] for _, state in graph.stream_run(None)]
    assert resumed == ["RELIANCE.NS"] and "run_symbol" not in graph.config


# --- the rules that read it -------------------------------------------------------------------

@pytest.mark.unit
def test_the_identity_and_the_lessons_follow_the_listing(night):
    identity = {"company_name": "Apple Inc.", "sector": "Technology", "industry": "Consumer Electronics"}
    current = build_instrument_context("AAPL", "stock", identity, trade_date="2026-10-29")
    assert "Business classification: Technology / Consumer Electronics" in current
    past = build_instrument_context("VOD.L", "stock", identity, trade_date="2026-10-29")
    assert "Business classification" not in past and "its current name" in past

    graph = _graph()
    assert graph._memory_as_of("2026-10-29", "AAPL") is None           # no filter: a current run
    assert graph._memory_as_of("2026-10-29", "RELIANCE.NS") == "2026-10-29"


@pytest.mark.unit
def test_an_indian_run_dated_india_s_day_is_told_the_session_now(monkeypatch):
    # 02:00 on Friday on a machine in Tokyo: India is still on Thursday, 22:30.
    tokyo = datetime(2026, 10, 30, 2, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
    _clock(monkeypatch, "2026-10-30", tokyo)
    current = build_instrument_context("RELIANCE.NS", "stock", {}, trade_date="2026-10-29", now=tokyo)
    assert "NSE is closed" in current and "was an NSE trading day" not in current
    past = build_instrument_context("RELIANCE.NS", "stock", {}, trade_date="2026-10-28", now=tokyo)
    assert "Wed 28 Oct 2026 was an NSE trading day." in past


@pytest.mark.unit
def test_live_only_data_is_served_to_a_run_dated_the_listing_s_day(night, monkeypatch):
    assert date_window.withhold_live_profile("2026-10-29", "AAPL") is None
    assert date_window.withhold_undisclosed_trades("2026-10-29", "AAPL") is None
    assert date_window.withhold_live_profile("2026-10-29", "VOD.L") is not None

    calendar = {"Earnings Date": [pd.Timestamp("2027-01-28").date()]}
    monkeypatch.setattr(corporate_actions.yf, "Ticker", lambda symbol: SimpleNamespace(
        dividends=pd.Series(dtype=float), splits=pd.Series(dtype=float), calendar=calendar))
    assert "Next results date" in corporate_actions.get_corporate_actions("AAPL", "2026-10-29")
    assert "Upcoming events: withheld" in corporate_actions.get_corporate_actions("VOD.L", "2026-10-29")


@pytest.mark.unit
def test_agent_reach_reads_a_live_page_for_a_run_dated_the_listing_s_day(night, monkeypatch):
    assert client.cutoff("2026-10-29", "AAPL") == ("2026-10-29", datetime(2026, 10, 30), False)
    assert client.cutoff("2026-10-29", "VOD.L") == ("2026-10-29", datetime(2026, 10, 29), True)

    read = []
    monkeypatch.setattr(media, "run", lambda *args, **kwargs: read.append(args) or {"items": []})
    with run_config({"run_symbol": "AAPL"}):
        out = media.read_web_page("https://example.com/a", "2026-10-29")
    assert out.startswith("No page found") and len(read) == 1
    assert media.read_web_page("https://example.com/a", "2026-10-29").startswith("read_web_page is not available")


class _UtcEvening(datetime):
    """19:30 UTC on Thursday 29 Oct 2026, which is 01:00 on Friday in India."""

    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 10, 29, 19, 30, tzinfo=UTC).astimezone(tz)


@pytest.mark.unit
def test_a_window_ending_on_the_machine_s_date_is_not_in_the_future(monkeypatch):
    # From midnight to 05:30 in India the machine's date is a day ahead of UTC's.
    monkeypatch.setattr(date_window, "datetime", _UtcEvening)
    monkeypatch.setattr(date_window, "get_current_date", lambda: "2026-10-30")
    dates = [datetime(2026, 10, 1, tzinfo=UTC)]
    assert date_window.coverage_gap(dates, "2026-10-23", "2026-10-30", "Feed", "news") is None
    later = date_window.coverage_gap(dates, "2026-10-24", "2026-10-31", "Feed", "news")
    assert later is not None and "the window extends past today" in later


# --- the CLI's default date -----------------------------------------------------------------------

UNATTENDED_ENV = {
    "TRADINGAGENTS_OUTPUT_LANGUAGE": "English",
    "TRADINGAGENTS_MAX_DEBATE_ROUNDS": "1",
    "TRADINGAGENTS_MAX_RISK_ROUNDS": "1",
    "TRADINGAGENTS_LLM_PROVIDER": "openai",
    "TRADINGAGENTS_QUICK_THINK_LLM": "gpt-6-luna",
}


@pytest.mark.unit
def test_the_cli_offers_new_york_s_date_for_a_us_ticker_after_midnight_in_india(night, monkeypatch):
    from cli import selections

    for name, value in UNATTENDED_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(selections.updates, "enabled", lambda: False)
    offered = []
    monkeypatch.setattr(selections, "get_analysis_date", lambda default=None: offered.append(default) or default)
    flags = {"analysts": "market", "save": True, "show": False}

    us = selections._prompt_selections({}, {**flags, "ticker": "AAPL"})
    india = selections._prompt_selections({}, {**flags, "ticker": "RELIANCE.NS"})

    assert offered == ["2026-10-29", "2026-10-30"]
    assert us["analysis_date"] == "2026-10-29" and india["analysis_date"] == "2026-10-30"
    # ... which AlphArena takes as dated today in New York; the machine's date it would not.
    assert refusal("AAPL", us["analysis_date"], now=NIGHT) is None
    assert refusal("AAPL", date_window.get_current_date(), now=NIGHT)


@pytest.mark.unit
def test_the_date_question_offers_the_default_it_is_given(monkeypatch):
    from cli import selections

    asked = []
    monkeypatch.setattr(selections.typer, "prompt", lambda text, default=None: asked.append(default) or default)
    assert selections.get_analysis_date("2024-01-05") == "2024-01-05" and asked == ["2024-01-05"]
