"""Benchmarks a decision can be settled against, reports that name them, and the square-off wording.

A run's benchmark Yahoo has no prices for is refused before the run and never stored with a
decision; the report header names the run's own benchmark; an instrument measured against
itself (NIFTY 50, SENSEX) is not scored as a missed call.
"""

import logging

import pandas as pd
import pytest

from tradingagents.backtest import summarize
from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph import trading_graph
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.memory import TradingMemoryLog, settlement
from tradingagents.reporting import horizon_line, write_report_tree

NOW = pd.Timestamp("2026-10-30 08:40", tz="UTC")


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


def _ramp(start, step, tz, first="2026-08-03", last="2026-12-31"):
    days = pd.bdate_range(first, last)
    return pd.Series([start + step * i for i in range(len(days))], index=days.tz_localize(tz), dtype=float)


class _Asked(list):
    """The symbols Yahoo was asked for, in order, and the series it answers from (``series``)."""

    series: dict


@pytest.fixture
def yahoo(monkeypatch):
    """settlement.get_closes over fixed series, [start, end) by day; an unknown symbol has none.
    The clock is pinned to NOW."""
    asked = _Asked()
    asked.series = {}

    def get_closes(symbol, start, end):
        asked.append(symbol)
        closes = asked.series.get(symbol, pd.Series(dtype=float))
        if closes.empty:
            return closes
        day = closes.index.tz_localize(None)
        return closes[(day >= pd.Timestamp(start)) & (day < pd.Timestamp(end))]

    monkeypatch.setattr(settlement, "get_closes", get_closes)
    monkeypatch.setattr(settlement, "_now", lambda: NOW)
    return asked


@pytest.fixture
def prices(yahoo):
    """Rising closes for an NSE stock, NIFTY 50 and QQQ; NIFTY50X and the history-less indices have none."""
    yahoo.series.update({"TCS.NS": _ramp(3000, 3, "Asia/Kolkata"), "^NSEI": _ramp(25000, 10, "Asia/Kolkata"),
                         "INFY.NS": _ramp(1500, 1, "Asia/Kolkata"), "QQQ": _ramp(500, 1, "America/New_York")})
    return yahoo


# --- R16: a benchmark with no prices is refused before the run, and never stored -----------

class _LLMReached(Exception):
    """The graph got past its config checks to building its model clients."""


def _build(tmp_path, monkeypatch, **config):
    def reached(*args, **kwargs):
        raise _LLMReached

    monkeypatch.setattr(trading_graph, "create_tier_client", reached)
    cfg = {**DEFAULT_CONFIG, "results_dir": str(tmp_path / "results"), "data_cache_dir": str(tmp_path / "cache"),
           "memory_log_path": str(tmp_path / "memory.md"), **config}
    return TradingAgentsGraph(config=cfg)


@pytest.mark.unit
@pytest.mark.parametrize(("benchmark", "named"), [
    ("NIFTY50X", "'NIFTY50X'"),                                  # a typo: Yahoo has no prices for it
    ("S&P 500", "'S&P 500'"),                                    # no such Yahoo symbol
    ("NIFTY MIDCAP 100", "NIFTY Midcap 100 (NIFTY_MIDCAP_100.NS)"),   # an index Yahoo keeps no history for
])
def test_the_graph_refuses_a_benchmark_with_no_prices_before_any_model_is_built(tmp_path, monkeypatch, prices,
                                                                                 benchmark, named):
    """A run with such a --benchmark stored it with its decision, which then never settled and
    was fetched again, and reported as unsettled, by every later run."""
    with pytest.raises(ValueError, match="benchmark") as refused:
        _build(tmp_path, monkeypatch, benchmark_ticker=benchmark)
    message = str(refused.value)
    assert named in message and "NIFTY IT" in message and "NIFTY 500" in message    # the choices
    assert "India VIX" not in message                     # a volatility index is no return benchmark
    assert not (tmp_path / "results").exists()            # refused before the run set anything up


@pytest.mark.unit
def test_an_index_yahoo_keeps_no_history_for_is_refused_without_asking_yahoo(prices):
    with pytest.raises(ValueError, match="no daily history"):
        settlement.check_benchmark("NIFTY MIDCAP 100", DEFAULT_CONFIG)
    assert list(prices) == []


@pytest.mark.unit
@pytest.mark.parametrize(("benchmark", "symbol"), [
    ("NIFTY IT", "^CNXIT"), ("nifty it", "^CNXIT"), ("BANKNIFTY", "^NSEBANK"), ("SENSEX", "^BSESN"),
    ("SPY", "SPY"), ("^N225", "^N225"),
])
def test_an_index_with_a_history_or_a_mapped_benchmark_is_taken_without_asking_yahoo(prices, benchmark, symbol):
    assert settlement.check_benchmark(benchmark, DEFAULT_CONFIG) == symbol
    assert list(prices) == []


@pytest.mark.unit
def test_another_symbol_is_taken_once_yahoo_has_prices_for_it(tmp_path, monkeypatch, prices):
    assert settlement.check_benchmark("qqq", DEFAULT_CONFIG) == "QQQ"
    assert list(prices) == ["QQQ"]
    with pytest.raises(_LLMReached):                       # past the check, on to the run
        _build(tmp_path, monkeypatch, benchmark_ticker="QQQ")
    with pytest.raises(_LLMReached):
        _build(tmp_path, monkeypatch, benchmark_ticker="NIFTY IT")


@pytest.mark.unit
def test_a_benchmark_yahoo_could_not_be_asked_about_is_not_taken_on_trust(tmp_path, monkeypatch, prices):
    def down(symbol, start, end):
        raise VendorUnavailableError("Yahoo Finance request failed: ConnectionError")

    monkeypatch.setattr(settlement, "get_closes", down)
    with pytest.raises(VendorUnavailableError, match="benchmark QQQ could not be checked"):
        _build(tmp_path, monkeypatch, benchmark_ticker="QQQ")


@pytest.mark.unit
@pytest.mark.parametrize("benchmark", ["NIFTY50X", "NIFTY MIDCAP 100"])
def test_a_decision_is_never_stored_with_a_benchmark_that_has_no_prices(tmp_path, prices, caplog, benchmark):
    """However the graph was built, the decision keeps a benchmark it can be settled against:
    its listing's index, with a warning, instead of one no run could ever price."""
    from tradingagents.dataflows.symbols import normalize_symbol

    bad = _graph(tmp_path, benchmark_ticker=normalize_symbol(benchmark))
    with caplog.at_level(logging.WARNING):
        bad.record_decision("TCS.NS", "2026-10-12", {"final_trade_decision": "**Rating**: Buy"})
    (entry,) = bad.memory_log.load_entries()
    assert entry["benchmark"] == "^NSEI"
    assert "measured against ^NSEI, its listing's index, instead of" in caplog.text

    # A later plain run settles it, and has nothing to report.
    prices.clear()
    note = _graph(tmp_path)._memory_step({"company_of_interest": "INFY.NS", "trade_date": "2026-10-30"})
    assert note["memory_note"] == ""
    assert sorted(set(prices)) == ["TCS.NS", "^NSEI"]
    (entry,) = bad.memory_log.load_entries()
    assert not entry["pending"] and entry["reflection"].endswith("vs ^NSEI")


@pytest.mark.unit
@pytest.mark.parametrize(("benchmark", "stored", "asked"), [
    ("^CNXIT", "^CNXIT", []),            # a known index: no request
    ("QQQ", "QQQ", ["QQQ"]),             # another symbol Yahoo prices
])
def test_a_runs_own_benchmark_is_still_stored_with_its_decision(tmp_path, prices, benchmark, stored, asked):
    graph = _graph(tmp_path, benchmark_ticker=benchmark)
    graph.record_decision("TCS.NS", "2026-10-12", {"final_trade_decision": "**Rating**: Buy"})
    assert graph.memory_log.load_entries()[0]["benchmark"] == stored
    assert list(prices) == asked


@pytest.mark.unit
def test_a_benchmark_yahoo_cannot_be_asked_about_at_the_end_of_a_run_is_kept(tmp_path, monkeypatch, prices):
    """The run's benchmark passed its check before the run; an outage at the end does not replace it."""
    def down(symbol, start, end):
        raise VendorUnavailableError("Yahoo Finance request failed: ConnectionError")

    monkeypatch.setattr(settlement, "get_closes", down)
    graph = _graph(tmp_path, benchmark_ticker="QQQ")
    graph.record_decision("TCS.NS", "2026-10-12", {"final_trade_decision": "**Rating**: Buy"})
    assert graph.memory_log.load_entries()[0]["benchmark"] == "QQQ"


# --- R19: the report header names the run's own benchmark ----------------------------------

def _header(tmp_path, ticker, settings, **state):
    final_state = {"trade_date": "2026-10-29", "final_trade_decision": "**Rating**: Buy", "final_rating": "Buy",
                   "market_report": "Market report.", **state}
    md = write_report_tree(final_state, ticker, tmp_path / "report", settings=settings, html=False)
    return md.read_text(encoding="utf-8").split("\n\n## ")[0]


@pytest.mark.unit
def test_the_report_header_names_the_benchmark_the_run_set(tmp_path):
    """A --benchmark "NIFTY IT" run's report said its decision was measured against NIFTY 50."""
    header = _header(tmp_path, "TCS.NS", {"benchmark_ticker": "^CNXIT"})
    assert ("- Market: National Stock Exchange of India (NSE); prices in INR; exchange time IST (Asia/Kolkata); "
            "benchmark NIFTY IT (^CNXIT), set for this run") in header
    assert "NIFTY 50" not in header
    # A value as the environment may give it is named as the run measures it.
    assert "benchmark NIFTY IT (^CNXIT), set for this run" in _header(tmp_path, "TCS.NS",
                                                                       {"benchmark_ticker": "nifty it"})


@pytest.mark.unit
def test_a_listing_on_no_known_exchange_gets_a_benchmark_line_when_the_run_set_one(tmp_path):
    assert "- Benchmark: QQQ, set for this run" in _header(tmp_path, "NVDA", {"benchmark_ticker": "QQQ"})
    plain = _header(tmp_path, "NVDA", {"benchmark_ticker": None})
    assert "Benchmark" not in plain and "- Market:" not in plain


@pytest.mark.unit
def test_without_a_benchmark_of_its_own_the_header_names_the_listings(tmp_path):
    header = _header(tmp_path, "RELIANCE.BO", {"benchmark_ticker": None})
    assert header.count("benchmark") == 1 and "; benchmark BSE SENSEX" in header
    assert "; benchmark NIFTY 50" in _header(tmp_path, "TCS.NS", None)


@pytest.mark.unit
def test_an_intraday_report_says_its_plan_has_no_benchmark(tmp_path):
    intraday = {"decision_horizon": "intraday", "benchmark_ticker": "^CNXIT"}
    header = _header(tmp_path, "TCS.NS", intraday, decision_horizon="intraday")
    assert "; benchmark none (an intraday plan is scored on its session's bars)" in header
    assert "NIFTY" not in header
    us = _header(tmp_path, "NVDA", {**intraday, "benchmark_ticker": "QQQ"}, decision_horizon="intraday")
    assert "- Benchmark: none (an intraday plan is scored on its session's bars)" in us


@pytest.mark.unit
def test_a_saved_report_and_the_memory_log_name_the_same_benchmark(tmp_path, prices):
    graph = _graph(tmp_path, benchmark_ticker="^CNXIT")
    state = {"trade_date": "2026-10-29", "final_trade_decision": "**Rating**: Buy", "final_rating": "Buy",
             "market_report": "Market report."}
    report = graph.save_reports(state, "TCS.NS", save_path=tmp_path / "report", html=False)
    graph.record_decision("TCS.NS", "2026-10-29", state)
    assert "benchmark NIFTY IT (^CNXIT), set for this run" in report.read_text(encoding="utf-8")
    assert graph.memory_log.load_entries()[0]["benchmark"] == "^CNXIT"


# --- R11: the square-off in the header is the configured one -------------------------------

CONTEXT = {"symbol": "RELIANCE.NS", "session": "2026-11-02", "opens": "09:15", "exit_by": "15:10",
           "why": "the next trading day after the analysis date Fri 30 Oct 2026", "timezone": "Asia/Kolkata",
           "tz_label": "IST"}


@pytest.mark.unit
def test_the_header_calls_the_square_off_the_configured_one_not_the_brokers(tmp_path):
    """15:10 is intraday_square_off's default; brokers square off between about 15:05 and 15:25."""
    state = {"trade_date": "2026-10-30", "decision_horizon": "intraday", "intraday_context": CONTEXT,
             "intraday_plan": {"exit_by": "12:30"}, "final_trade_decision": "**Rating**: Buy", "final_rating": "Buy"}
    assert horizon_line(state).endswith("; exit by 12:30 IST (the configured square-off is 15:10 IST)")
    report = write_report_tree(state, "RELIANCE.NS", tmp_path, settings={"decision_horizon": "intraday"})
    page = (tmp_path / "complete_report.html").read_text(encoding="utf-8")
    for text in (report.read_text(encoding="utf-8"), page):
        assert "exit by 12:30 IST (the configured square-off is 15:10 IST)" in text
        assert "broker square-off" not in text


# --- R20: an instrument measured against itself is not scored as a missed call -------------

def _settle_buys(tmp_path, prices, ticker):
    prices.series.update({"^NSEI": _ramp(25000, 50, "Asia/Kolkata"), "^NSEBANK": _ramp(55000, 50, "Asia/Kolkata"),
                          "SPY": _ramp(600, 0.6, "America/New_York")})
    graph = _graph(tmp_path)
    for day in ("2026-09-01", "2026-09-15", "2026-10-01"):
        graph.record_decision(ticker, day, {"final_trade_decision": "**Rating**: Buy", "final_rating": "Buy"})
    done = graph.settle_pending(ticker)
    assert len(done.settled) == 3 and not done.failed
    return graph


@pytest.mark.unit
@pytest.mark.parametrize("ticker", ["^NSEI", "^BSESN", "SPY"])
def test_an_instrument_that_is_its_own_benchmark_is_not_scored_as_a_missed_call(tmp_path, prices, ticker):
    """Three Buys of NIFTY 50, each followed by a rise, were reported as 'called the direction 0%'."""
    prices.series["^BSESN"] = _ramp(80000, 160, "Asia/Kolkata")
    graph = _settle_buys(tmp_path, prices, ticker)

    summary = summarize(tmp_path / "memory.md")
    assert (summary.resolved, summary.pending, summary.unscored) == (0, 0, 3)
    assert "Buy" not in summary.by_rating
    text = summary.render()
    assert "called the direction" not in text
    assert f"Cells on {ticker} are not scored: each was measured against itself" in text
    # The reflection is told that alpha against itself is zero by construction.
    for raw, alpha, label in graph.reflector.told:
        assert raw > 0 and alpha == 0 and label.startswith(f"{ticker} (the instrument itself")


@pytest.mark.unit
def test_an_index_measured_against_another_is_still_scored(tmp_path, prices):
    graph = _settle_buys(tmp_path, prices, "^NSEBANK")
    summary = summarize(tmp_path / "memory.md")
    assert summary.resolved == 3 and summary.unscored == 0 and summary.by_rating["Buy"].count == 3
    assert all(label == "^NSEI" for _, _, label in graph.reflector.told)


@pytest.mark.unit
def test_a_decision_logged_without_its_benchmark_is_scored_as_before(tmp_path):
    """Before benchmarks were recorded, an index was measured against SPY: its alpha is real."""
    log = TradingMemoryLog({"memory_log_path": str(tmp_path / "memory.md")})
    log.store_decision("^NSEI", "2026-09-01", "**Rating**: Buy")
    log.update_with_outcome("^NSEI", "2026-09-01", 0.01, -0.01, 5, "Lagged SPY.", "2026-09-08")
    summary = summarize(tmp_path / "memory.md")
    assert summary.resolved == 1 and summary.by_rating["Buy"].hit_rate == 0.0 and summary.unscored == 0


# --- found after the fix round: the CLI, the HTML header ------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize(("args", "said"), [
    (["--benchmark", "NIFTY50X"], "Yahoo Finance has no prices for 'NIFTY50X'"),
    (["--benchmark", "NIFTY MIDCAP 100"], "keeps no daily history for NIFTY Midcap 100"),
])
def test_the_cli_refuses_such_a_benchmark_before_asking_anything(monkeypatch, prices, args, said):
    """The graph's refusal came as a traceback, after the setup questions had been answered."""
    from typer.testing import CliRunner

    import cli.main as cli_main

    monkeypatch.setattr(cli_main, "run_analysis", lambda **kw: pytest.fail("the run must not start"))
    result = CliRunner().invoke(cli_main.app, ["--ticker", "RELIANCE.NS", *args])
    assert result.exit_code == 1 and said in " ".join(result.output.split())


@pytest.mark.unit
def test_the_cli_checks_a_benchmark_set_in_the_environment_and_says_when_yahoo_cannot_be_asked(monkeypatch, prices):
    from typer.testing import CliRunner

    import cli.main as cli_main

    def down(symbol, start, end):
        raise VendorUnavailableError("Yahoo Finance request failed: ConnectionError")

    monkeypatch.setattr(settlement, "get_closes", down)
    monkeypatch.setitem(DEFAULT_CONFIG, "benchmark_ticker", "QQQ")
    monkeypatch.setattr(cli_main, "run_analysis", lambda **kw: pytest.fail("the run must not start"))
    result = CliRunner().invoke(cli_main.app, ["--ticker", "AAPL"])
    assert result.exit_code == 1 and "the benchmark QQQ could not be checked" in " ".join(result.output.split())


@pytest.mark.unit
def test_the_cli_lets_a_priced_benchmark_through(monkeypatch, prices):
    from typer.testing import CliRunner

    import cli.main as cli_main

    seen = {}
    monkeypatch.setattr(cli_main, "run_analysis", lambda **kw: seen.update(kw))
    result = CliRunner().invoke(cli_main.app, ["--ticker", "AAPL", "--benchmark", "QQQ"])
    assert result.exit_code == 0, result.output
    assert seen["flags"]["benchmark"] == "QQQ" and list(prices) == ["QQQ"]


@pytest.mark.unit
def test_the_html_header_names_the_runs_own_benchmark_as_the_markdown_one_does():
    """complete_report.md said NIFTY IT for a --benchmark "NIFTY IT" run; complete_report.html said NIFTY 50."""
    from tradingagents.report_html import _run_details

    state = {"trade_date": "2026-10-29", "final_trade_decision": "Rating: Hold"}
    details = dict(_run_details(state, {"benchmark_ticker": "^CNXIT"}, "TCS.NS"))
    assert details["Market"].endswith("benchmark NIFTY IT (^CNXIT), set for this run")
    assert dict(_run_details(state, {}, "TCS.NS"))["Market"].endswith("benchmark NIFTY 50")
    elsewhere = dict(_run_details(state, {"benchmark_ticker": "QQQ"}, "BTC-USD"))
    assert "Market" not in elsewhere and elsewhere["Benchmark"] == "QQQ, set for this run"
    intraday = dict(_run_details({**state, "decision_horizon": "intraday"}, {"benchmark_ticker": "^CNXIT"}, "TCS.NS"))
    assert intraday["Market"].endswith("benchmark none (an intraday plan is scored on its session&#x27;s bars)")


@pytest.mark.unit
def test_the_agents_are_told_the_benchmark_their_decision_is_measured_against(monkeypatch):
    """They were told 'benchmark NIFTY 50' on a --benchmark "NIFTY IT" run, as the report header was."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from tradingagents.agents.context import build_instrument_context
    from tradingagents.dataflows import config as dataflows_config

    def told(**config):
        monkeypatch.setattr(dataflows_config, "get_config", lambda: {**DEFAULT_CONFIG, **config})
        return build_instrument_context("TCS.NS", "stock", {"company_name": "Tata Consultancy Services"},
                                        now=datetime(2031, 10, 30, 11, 5, tzinfo=ZoneInfo("Asia/Kolkata")))

    assert ("Market: National Stock Exchange of India (NSE), benchmark NIFTY 50 (`^NSEI`); this run's decision "
            "is measured against NIFTY IT (`^CNXIT`).") in told(benchmark_ticker="nifty it")
    assert "this run's decision is measured against QQQ (`QQQ`)." in told(benchmark_ticker="QQQ")
    for plain in (told(), told(benchmark_ticker="NIFTY IT", decision_horizon="intraday")):
        assert "benchmark NIFTY 50 (`^NSEI`)." in plain and "measured against" not in plain


@pytest.mark.unit
def test_the_cli_names_the_variable_a_refused_benchmark_came_from(monkeypatch, prices):
    from typer.testing import CliRunner

    import cli.main as cli_main

    monkeypatch.setitem(DEFAULT_CONFIG, "benchmark_ticker", "NIFTY50X")
    monkeypatch.setattr(cli_main, "run_analysis", lambda **kw: pytest.fail("the run must not start"))
    result = CliRunner().invoke(cli_main.app, ["--ticker", "RELIANCE.NS"])
    out = " ".join(result.output.split())
    assert result.exit_code == 1 and out.startswith("TRADINGAGENTS_BENCHMARK_TICKER NIFTY50X: Yahoo Finance has no")
    result = CliRunner().invoke(cli_main.app, ["--ticker", "RELIANCE.NS", "--benchmark", "INDIA VIX"])
    assert result.exit_code == 1 and " ".join(result.output.split()).startswith("--benchmark INDIA VIX: ")


@pytest.mark.unit
def test_an_intraday_run_checks_no_benchmark(tmp_path, monkeypatch, prices):
    """An intraday plan has no benchmark, yet a bad TRADINGAGENTS_BENCHMARK_TICKER stopped
    /day-trader runs, in the CLI and when the graph was built."""
    from typer.testing import CliRunner

    import cli.main as cli_main

    monkeypatch.setitem(DEFAULT_CONFIG, "benchmark_ticker", "NIFTY50X")
    seen = {}
    monkeypatch.setattr(cli_main, "run_analysis", lambda **kw: seen.update(kw))
    result = CliRunner().invoke(cli_main.app, ["--ticker", "RELIANCE.NS", "--horizon", "intraday"])
    assert result.exit_code == 0, result.output
    assert seen["flags"]["horizon"] == "intraday" and list(prices) == []
    with pytest.raises(_LLMReached):                       # past the checks, on to the models
        _build(tmp_path, monkeypatch, benchmark_ticker="NIFTY50X", decision_horizon="intraday")
    with pytest.raises(ValueError, match="NIFTY50X"):
        _build(tmp_path, monkeypatch, benchmark_ticker="NIFTY50X")
