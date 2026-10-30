"""Settlement, third review: entries the released fork left pending, benchmarks checked at the run's own
date and once per graph, volatility indices refused, same-day lessons on their benchmark's clock, and the
charges and tax rules checked before the run."""

import logging

import pandas as pd
import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from tradingagents.backtest import summarize
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph import trading_graph
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.memory import TradingMemoryLog, settlement
from tradingagents.memory.log import _market_clock

NOW = pd.Timestamp("2026-10-30 08:40", tz="UTC")
SEP = TradingMemoryLog._SEPARATOR
BUY = {"final_trade_decision": "**Rating**: Buy", "final_rating": "Buy"}


class _Reflector:
    def __init__(self):
        self.told = []

    def reflect_on_final_decision(self, final_decision, raw_return, alpha_return, benchmark_name, holding_days):
        self.told.append((raw_return, alpha_return, benchmark_name))
        return f"alpha {alpha_return:+.1%} vs {benchmark_name}"


def _graph(tmp_path, **config):
    """A graph without __init__ (no LLM clients) on a temp log, with a stub reflector."""
    graph = object.__new__(TradingAgentsGraph)
    graph.config = {**DEFAULT_CONFIG, "memory_log_path": str(tmp_path / "memory.md"), **config}
    graph.memory_log = TradingMemoryLog(graph.config)
    graph.reflector = _Reflector()
    graph.selected_analysts = ("market",)
    graph._log_state = lambda *a: None
    return graph


class _Client:
    def get_llm(self):
        return FakeListChatModel(responses=["never asked"])


def _built(tmp_path, monkeypatch, **config):
    """A graph built through __init__, as the CLI and a backtest build it; its model is never called."""
    monkeypatch.setattr(trading_graph, "create_tier_client", lambda config, tier, **kw: _Client())
    cfg = {**DEFAULT_CONFIG, "results_dir": str(tmp_path / "results"), "data_cache_dir": str(tmp_path / "cache"),
           "memory_log_path": str(tmp_path / "memory.md"), **config}
    graph = TradingAgentsGraph(["market"], config=cfg)
    graph.started = []
    graph.resolve_instrument_context = lambda ticker, *a, **k: graph.started.append(ticker) or ""
    graph._log_state = lambda *a: None
    return graph


class _LLMReached(Exception):
    """The graph got past its config checks to building its model clients."""


def _build_until_models(tmp_path, monkeypatch, **config):
    def reached(*args, **kwargs):
        raise _LLMReached

    monkeypatch.setattr(trading_graph, "create_tier_client", reached)
    cfg = {**DEFAULT_CONFIG, "results_dir": str(tmp_path / "results"), "data_cache_dir": str(tmp_path / "cache"),
           "memory_log_path": str(tmp_path / "memory.md"), **config}
    return TradingAgentsGraph(config=cfg)


def _ramp(start, step, tz, first="2025-01-01", last="2026-12-31"):
    days = pd.bdate_range(first, last)
    return pd.Series([start + step * i for i in range(len(days))], index=days.tz_localize(tz), dtype=float)


class _Asked(list):
    """The symbols Yahoo was asked for, in order; ``windows`` the (symbol, start, end) of each request;
    ``series`` what it answers from; ``empty`` makes every answer empty."""

    series: dict
    windows: list
    empty: bool


@pytest.fixture
def yahoo(monkeypatch):
    """settlement.get_closes over fixed series, [start, end) by day; the clocks pinned to NOW. Rising closes
    for two NSE stocks, NIFTY 50, SENSEX, SPY and QQQ from 2025, and for NEWETF, an ETF listed 2026-09-01."""
    asked = _Asked()
    asked.windows, asked.empty = [], False
    asked.series = {"TCS.NS": _ramp(3000, 3, "Asia/Kolkata"), "INFY.NS": _ramp(1500, 1, "Asia/Kolkata"),
                    "^NSEI": _ramp(25000, 50, "Asia/Kolkata"), "^BSESN": _ramp(80000, 160, "Asia/Kolkata"),
                    "SPY": _ramp(600, 0.6, "America/New_York"), "QQQ": _ramp(500, 1, "America/New_York"),
                    "NEWETF": _ramp(50, 0.1, "America/New_York", first="2026-09-01")}

    def get_closes(symbol, start, end):
        asked.append(symbol)
        asked.windows.append((symbol, start, end))
        closes = asked.series.get(symbol, pd.Series(dtype=float))
        if closes.empty or asked.empty:
            return pd.Series(dtype=float)
        day = closes.index.tz_localize(None)
        return closes[(day >= pd.Timestamp(start)) & (day < pd.Timestamp(end))]

    monkeypatch.setattr(settlement, "get_closes", get_closes)
    monkeypatch.setattr(settlement, "_now", lambda: NOW)
    monkeypatch.setattr(trading_graph, "get_current_date", lambda: "2026-10-30")    # a run date's limit
    return asked


def _blocks(path):
    return [block for block in path.read_text(encoding="utf-8").split(SEP) if block.strip()]


# --- R8: a decision the released fork left pending is settled with the benchmark it was measured against ---

def _released_pending(path, ticker, day, decision="**Rating**: Buy\n\nBreakout.", head="", rating="Buy"):
    """A pending entry byte for byte as the released fork wrote it: no BENCHMARK line, the decision unescaped."""
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"[{day} | {ticker} | {rating} | pending]\n\n{head}DECISION:\n{decision}{SEP}")


@pytest.mark.unit
@pytest.mark.parametrize("ticker", ["^NSEI", "^BSESN"])
def test_a_headline_index_left_pending_by_the_release_is_not_scored_as_a_missed_call(tmp_path, yahoo, ticker):
    """Settled against itself after the upgrade, with nothing written to say so: three Buys of NIFTY 50, each
    followed by a rise, read 'called the direction 0%' and their lessons '+0.0%' against no benchmark."""
    path = tmp_path / "memory.md"
    for day in ("2026-09-01", "2026-09-15", "2026-10-01"):
        _released_pending(path, ticker, day)
    graph = _graph(tmp_path)

    done = graph.settle_pending(ticker)

    assert len(done.settled) == 3 and not done.failed
    assert {e["benchmark"] for e in graph.memory_log.load_entries()} == {ticker}
    summary = summarize(path)
    assert (summary.resolved, summary.pending, summary.unscored) == (0, 0, 3)
    text = summary.render()
    assert "called the direction" not in text
    assert f"Cells on {ticker} are not scored: each was measured against itself" in text
    assert f"| +0.0% vs {ticker} | 5d]" in graph.memory_log.get_past_context(ticker)


@pytest.mark.unit
def test_an_entry_gets_a_benchmark_line_only_when_it_has_none(tmp_path, yahoo):
    path = tmp_path / "memory.md"
    _released_pending(path, "TCS.NS", "2026-09-01")
    graph = _graph(tmp_path, benchmark_ticker="^CNXIT")
    yahoo.series["^CNXIT"] = _ramp(35000, 20, "Asia/Kolkata")
    graph.record_decision("TCS.NS", "2026-09-15", BUY)      # recorded with this run's own benchmark

    assert len(graph.settle_pending("TCS.NS").settled) == 2

    legacy, own = graph.memory_log.load_entries()
    assert (legacy["benchmark"], own["benchmark"]) == ("^NSEI", "^CNXIT")
    assert all(block.count("BENCHMARK: ") == 1 for block in _blocks(path))
    assert legacy["reflection"].endswith("vs ^NSEI") and own["reflection"].endswith("vs ^CNXIT")
    # The writer leaves a recorded benchmark alone whatever it is handed.
    log = TradingMemoryLog({"memory_log_path": str(tmp_path / "other.md")})
    log.store_decision("INFY.NS", "2026-09-01", "**Rating**: Buy", benchmark="^CNXIT")
    log.update_with_outcome("INFY.NS", "2026-09-01", 0.01, 0.0, 5, "ok", "2026-09-08", benchmark="^NSEI")
    assert log.load_entries()[0]["benchmark"] == "^CNXIT"


# --- R9: a run's benchmark must be priced at the run's own date --------------------------------------------

@pytest.mark.unit
def test_a_past_dated_run_is_refused_a_benchmark_listed_after_its_date(tmp_path, monkeypatch, yahoo):
    """NEWETF had prices lately, so it passed the check, but none at a backtest cell's date: the cell's
    decision then stayed pending for good, and the backtest told the user to re-run."""
    graph = _built(tmp_path, monkeypatch, benchmark_ticker="NEWETF")     # priced in the last 30 days

    with pytest.raises(ValueError, match=r"no prices for 'NEWETF' in the 30 days to 2026-06-01, so no decision"):
        graph.create_run_state("TCS.NS", "2026-06-01")
    assert graph.started == [] and not (tmp_path / "memory.md").exists()     # refused before the run started

    graph.create_run_state("TCS.NS", "2026-10-29")
    assert graph.started == ["TCS.NS"]


@pytest.mark.unit
def test_a_backtest_cell_dated_before_its_benchmark_was_listed_fails_with_the_reason(tmp_path, monkeypatch, yahoo):
    from tradingagents.backtest import run_backtest

    monkeypatch.setattr(trading_graph, "create_tier_client", lambda config, tier, **kw: _Client())
    monkeypatch.setattr(TradingAgentsGraph, "resolve_instrument_context",
                        lambda *a, **k: pytest.fail("the cell must be refused before it runs"))
    config = {**DEFAULT_CONFIG, "results_dir": str(tmp_path / "results"),
              "data_cache_dir": str(tmp_path / "cache"), "benchmark_ticker": "NEWETF"}

    result = run_backtest(["TCS.NS"], ["2026-06-01"], config, selected_analysts=("market",))

    ((ticker, date, reason),) = result.failures
    assert (ticker, date) == ("TCS.NS", "2026-06-01")
    assert "Yahoo Finance has no prices for 'NEWETF' in the 30 days to 2026-06-01" in reason


@pytest.mark.unit
def test_a_graph_not_built_that_way_stores_the_listings_index_for_such_a_date(tmp_path, yahoo, caplog):
    graph = _graph(tmp_path, benchmark_ticker="NEWETF")
    with caplog.at_level(logging.WARNING):
        graph.record_decision("TCS.NS", "2026-06-01", BUY)
    assert graph.memory_log.load_entries()[0]["benchmark"] == "^NSEI"
    assert "in the 30 days to 2026-06-01" in caplog.text

    assert graph.settle_pending("TCS.NS").settled == [("TCS.NS", "2026-06-01")]


@pytest.mark.unit
def test_a_decision_already_stored_with_a_benchmark_listed_since_is_reported_not_left_pending(tmp_path, yahoo):
    """Its benchmark's first close came after the entry, which read as 'not traded yet' on every run, for good:
    no failure, no note, and four Yahoo requests each time."""
    log = TradingMemoryLog({"memory_log_path": str(tmp_path / "memory.md")})
    log.store_decision("TCS.NS", "2026-06-01", "**Rating**: Buy", benchmark="NEWETF")    # as stored before

    done = _graph(tmp_path).settle_pending("TCS.NS")

    assert done.failed == [("TCS.NS", "2026-06-01", "prices unavailable: no close for the benchmark NEWETF "
                                                    "on or before the entry session 2026-06-02")]
    note = _graph(tmp_path)._memory_step({"company_of_interest": "INFY.NS", "trade_date": "2026-10-30"})
    assert note["memory_note"] == "1 past decision(s) could not be settled this run and stay pending."


# --- R10: a volatility index is no benchmark -------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize(("benchmark", "named"), [
    ("INDIA VIX", "India VIX (^INDIAVIX)"), ("^INDIAVIX", "India VIX (^INDIAVIX)"), ("^VIX", "^VIX"),
])
def test_a_volatility_index_is_refused_as_a_benchmark_without_asking_yahoo(tmp_path, monkeypatch, yahoo,
                                                                          benchmark, named):
    """India VIX was taken: a stock's alpha became its return less the swing in volatility (+14.6% for a
    +0.5% move), stored, reflected on and counted in backtests."""
    with pytest.raises(ValueError, match="volatility index") as refused:
        settlement.check_benchmark(benchmark, DEFAULT_CONFIG)
    message = str(refused.value)
    assert message.startswith(f"{named} is a volatility index")
    choices = message.split("with a daily history (")[1]
    assert "NIFTY 50" in choices and "India VIX" not in choices
    assert list(yahoo) == []
    with pytest.raises(ValueError, match="volatility index"):
        _build_until_models(tmp_path, monkeypatch, benchmark_ticker=benchmark)


@pytest.mark.unit
def test_a_decision_is_never_stored_against_a_volatility_index(tmp_path, yahoo, caplog):
    graph = _graph(tmp_path, benchmark_ticker="^INDIAVIX")
    with caplog.at_level(logging.WARNING):
        graph.record_decision("TCS.NS", "2026-10-12", BUY)
    assert graph.memory_log.load_entries()[0]["benchmark"] == "^NSEI"
    assert "is a volatility index" in caplog.text


# --- R11: one check of a run's benchmark, and no swap at the end of the run ------------------------------

@pytest.mark.unit
def test_a_run_asks_yahoo_about_its_benchmark_once(tmp_path, monkeypatch, yahoo):
    """The graph checked it, then checked it again when it recorded the decision."""
    graph = _built(tmp_path, monkeypatch, benchmark_ticker="QQQ")
    assert list(yahoo) == ["QQQ"]

    graph.create_run_state("TCS.NS", "2026-10-29")
    graph.record_decision("TCS.NS", "2026-10-29", BUY)

    assert list(yahoo) == ["QQQ"]
    assert graph.memory_log.load_entries()[0]["benchmark"] == "QQQ"


@pytest.mark.unit
def test_a_sweep_of_past_cells_asks_yahoo_about_its_benchmark_twice_in_all(tmp_path, monkeypatch, yahoo):
    """One request per cell, 100 for a 100-cell sweep: once for the graph, once from the first cell's date."""
    graph = _built(tmp_path, monkeypatch, benchmark_ticker="QQQ")
    days = ("2026-03-02", "2026-04-06", "2026-05-04", "2026-06-01", "2026-07-06")
    for ticker in ("TCS.NS", "INFY.NS"):
        for day in days:
            graph.create_run_state(ticker, day)
            graph.record_decision(ticker, day, BUY)

    assert yahoo.windows == [("QQQ", "2026-09-30", "2026-10-31"), ("QQQ", "2026-01-31", "2026-10-31")]
    assert [e["benchmark"] for e in graph.memory_log.load_entries()] == ["QQQ"] * 10


@pytest.mark.unit
def test_cells_before_a_listing_are_refused_on_one_answer_and_an_empty_answer_is_asked_again(tmp_path, monkeypatch,
                                                                                           yahoo):
    graph = _built(tmp_path / "a", monkeypatch, benchmark_ticker="NEWETF")
    for day in ("2026-05-04", "2026-06-01", "2026-07-06"):
        with pytest.raises(ValueError, match=f"in the 30 days to {day}"):
            graph.create_run_state("TCS.NS", day)
    graph.create_run_state("TCS.NS", "2026-09-15")             # priced by then
    assert list(yahoo) == ["NEWETF", "NEWETF"]                # the graph's check, then the first cell's

    yahoo.clear()
    graph = _built(tmp_path / "b", monkeypatch, benchmark_ticker="QQQ")
    yahoo.empty = True                                        # a moment when Yahoo answers nothing
    with pytest.raises(ValueError, match="in the 30 days to 2026-03-02"):
        graph.create_run_state("TCS.NS", "2026-03-02")
    yahoo.empty = False
    graph.create_run_state("TCS.NS", "2026-03-02")            # asked again, not refused on that answer
    assert list(yahoo) == ["QQQ", "QQQ", "QQQ"]


@pytest.mark.unit
def test_an_empty_answer_at_the_end_of_a_run_no_longer_swaps_its_benchmark(tmp_path, monkeypatch, yahoo):
    """The saved report named QQQ, the run's own, while the memory log stored NIFTY 50."""
    graph = _built(tmp_path, monkeypatch, benchmark_ticker="QQQ")
    graph.create_run_state("TCS.NS", "2026-10-29")
    yahoo.empty = True                                       # Yahoo answers nothing from here on

    state = {**BUY, "trade_date": "2026-10-29", "market_report": "Market report."}
    graph.record_decision("TCS.NS", "2026-10-29", state)
    report = graph.save_reports(state, "TCS.NS", save_path=tmp_path / "report", html=False)

    assert graph.memory_log.load_entries()[0]["benchmark"] == "QQQ"
    assert "benchmark QQQ, set for this run" in report.read_text(encoding="utf-8")
    assert list(yahoo) == ["QQQ"]


# --- R12: an entry the released fork recorded keeps its reflection when this code settles it ------------

ENDINGS = ["\n\n<!-- ENTRY_END --> ", "\n\n<!-- ENTRY_END -->\t", "\nREFLECTION: "]


@pytest.mark.unit
@pytest.mark.parametrize("writer", ["update_with_outcome", "batch_update_with_outcomes",
                                    "update_with_intraday_outcome"])
@pytest.mark.parametrize("ending", ENDINGS, ids=["separator, space", "separator, tab", "label, space"])
def test_a_pending_entry_from_the_release_keeps_its_reflection_when_it_settles(tmp_path, writer, ending):
    """The release stored model text unescaped. Settling strips the space after a separator ending the
    decision, which made it the real separator (the reflection fell into a block no reader parses), or the
    space after a label line, which made it the real label (the reflection read back with 'REFLECTION:')."""
    path = tmp_path / "memory.md"
    head = 'HORIZON: intraday\nINTRADAY PLAN: {}\n\n' if writer == "update_with_intraday_outcome" else ""
    _released_pending(path, "INFY.NS", "2026-10-12", f"Rating: Buy\n\nInvestment Thesis: Deal wins.{ending}", head)
    _released_pending(path, "TCS.NS", "2026-10-12", "**Rating**: Hold", rating="Hold")
    log = TradingMemoryLog({"memory_log_path": str(path)})

    if writer == "update_with_outcome":
        assert log.update_with_outcome("INFY.NS", "2026-10-12", 0.016, 0.014, 5, "The Buy worked.", "2026-10-19")
    elif writer == "batch_update_with_outcomes":
        log.batch_update_with_outcomes([{"ticker": "INFY.NS", "trade_date": "2026-10-12", "raw_return": 0.016,
                                         "alpha_return": 0.014, "holding_days": 5, "reflection": "The Buy worked.",
                                         "resolution_date": "2026-10-19"}])
    else:
        assert log.update_with_intraday_outcome("INFY.NS", "2026-10-12", {"scored": False}, "The Buy worked.",
                                                resolution_date="2026-10-12")

    assert len(_blocks(path)) == 2
    infy, tcs = log.load_entries()
    assert (infy["pending"], tcs["ticker"], tcs["pending"]) == (False, "TCS.NS", True)
    assert infy["reflection"] == "The Buy worked."
    assert infy["decision"].startswith("Rating: Buy\n\nInvestment Thesis: Deal wins.\n")
    assert infy["decision"].rstrip().endswith(("\\<!-- ENTRY_END -->", "\\REFLECTION:"))   # kept, as text


@pytest.mark.unit
def test_the_next_run_after_the_upgrade_reads_the_lesson_of_such_an_entry(tmp_path, yahoo):
    path = tmp_path / "memory.md"
    _released_pending(path, "INFY.NS", "2026-10-12",
                      "Rating: Buy\n\nInvestment Thesis: Deal wins.\n\n<!-- ENTRY_END --> ")
    _released_pending(path, "TCS.NS", "2026-10-29", "**Rating**: Hold", rating="Hold")
    graph = _graph(tmp_path)

    assert graph.settle_pending("INFY.NS").settled == [("INFY.NS", "2026-10-12")]

    assert len(_blocks(path)) == 2
    lesson = graph.memory_log.get_past_context("INFY.NS")
    assert "Investment Thesis: Deal wins." in lesson and "REFLECTION:\nalpha " in lesson and "vs ^NSEI" in lesson


# --- R13: a same-day lesson only when its benchmark also closes with the run's market ---------------------

# (ticker, benchmark) of each lesson resolved Tue 27 Oct 2026, as record_decision stores them.
LESSONS = [("^N225", "SPY"), ("BBCA.JK", "SPY"), ("RELIANCE.NS", "SPY"), ("TCS.NS", "^NSEI"),
           ("6758.T", "^N225"), ("NVDA", "SPY"), ("HSBA.L", "^FTSE")]
# A backfill run dated that day, and the same-day lessons known by its own close.
SAME_DAY = {
    "^N225": {"6758.T"},                # not its own, measured against SPY's close, 13.5 h after Tokyo's
    "BMRI.JK": set(),                   # not BBCA.JK's, against SPY too, 11 h after Jakarta's
    "INFY.NS": {"TCS.NS"},              # not RELIANCE.NS, measured against SPY (--benchmark SPY)
    "7203.T": {"6758.T"},               # measured against Tokyo's own index
    "AAPL": {"NVDA"},
    "BP.L": {"HSBA.L"},
}


@pytest.mark.unit
@pytest.mark.parametrize("run", list(SAME_DAY))
def test_a_backfill_reads_no_lesson_of_its_day_whose_benchmark_closed_after_it(tmp_path, run):
    """Only the listing's close was counted: a NIKKEI backfill dated the 27th read an alpha against SPY's
    close of that day, final 13.5 hours after its own."""
    log = TradingMemoryLog({"memory_log_path": str(tmp_path / "memory.md")})
    for ticker, benchmark in LESSONS:
        log.store_decision(ticker, "2026-10-19", f"**Rating**: Buy\n\n{ticker} decision", benchmark=benchmark)
        log.update_with_outcome(ticker, "2026-10-19", 0.05, 0.02, 5, f"<<{ticker}>>", resolution_date="2026-10-27")

    def seen(as_of):
        context = log.get_past_context(run, n_same=20, n_cross=20, as_of=as_of)
        return {ticker for ticker, _ in LESSONS if f"<<{ticker}>>" in context}

    assert seen("2026-10-27") == SAME_DAY[run]
    assert seen("2026-10-28") == {ticker for ticker, _ in LESSONS}       # every lesson from the next day


@pytest.mark.unit
def test_an_exchanges_index_closes_with_its_listings():
    assert _market_clock("^N225") == _market_clock("7203.T")
    assert _market_clock("^FTSE") == _market_clock("HSBA.L")
    assert _market_clock("^TWII") == _market_clock("2330.TW") != _market_clock("6488.TWO")
    assert _market_clock("^GSPC") == ("instrument", "^GSPC")        # not in benchmark_map: a key of its own


# --- R18: the charges and the capital-gains rules are checked before the run -------------------------------

@pytest.mark.unit
@pytest.mark.parametrize(("config", "named"), [
    ({"capital_gains_overrides": {"long_term_after_months": 12.5}}, "long_term_after_months must be a whole"),
    ({"capital_gains_overrides": {"long_term_months": 12}}, "long_term_months"),
    ({"capital_gains_rules": "india_2024"}, "unknown capital-gains rules 'india_2024'"),
    ({"cost_profile": "zerodha"}, "unknown cost profile 'zerodha'"),
    ({"cost_overrides": {"brokerage": 0.0003}}, "unknown CostProfile field"),
])
def test_bad_charges_or_tax_rules_are_refused_before_any_model_is_built(tmp_path, monkeypatch, config, named):
    """12.5 months removed every risk figure from every run on a book holding NSE/BSE listings, mid-run,
    with one log line to say why."""
    with pytest.raises(ValueError, match=named):
        _build_until_models(tmp_path, monkeypatch, **config)
    assert not (tmp_path / "results").exists()


@pytest.mark.unit
def test_whole_months_given_as_a_float_and_the_shipped_rules_pass(tmp_path, monkeypatch):
    with pytest.raises(_LLMReached):
        _build_until_models(tmp_path, monkeypatch, capital_gains_overrides={"long_term_after_months": 12.0})
    with pytest.raises(_LLMReached):
        _build_until_models(tmp_path, monkeypatch, cost_profile="none", capital_gains_rules=None)
