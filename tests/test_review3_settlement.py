"""Settlement, fourth review: lessons the release settled read on the clock of the benchmark it measured them
against, setup errors refused in one line, one Yahoo request for a CLI run's benchmark, a listed index checked
at a past run's date, and every volatility index refused."""

import re

import pandas as pd
import pytest
import typer
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from typer.testing import CliRunner

import cli.main as cli_main
import cli.run as cli_run
from cli.models import AnalystType
from tradingagents.dataflows.symbols import normalize_symbol
from tradingagents.default_config import DEFAULT_CONFIG, build_default_config
from tradingagents.graph import trading_graph
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.memory import TradingMemoryLog, settlement

NOW = pd.Timestamp("2026-10-30 08:40", tz="UTC")


def _ramp(start, step, tz, first="2025-01-01", last="2026-12-31"):
    days = pd.bdate_range(first, last)
    return pd.Series([start + step * i for i in range(len(days))], index=days.tz_localize(tz), dtype=float)


class _Asked(list):
    """The symbols Yahoo was asked for, in order; ``windows`` the (symbol, start, end) of each request;
    ``series`` what it answers from."""

    series: dict
    windows: list


@pytest.fixture
def yahoo(monkeypatch):
    """settlement.get_closes over fixed series, [start, end) by day; the clocks pinned to NOW. Rising closes
    for two NSE stocks, NIFTY 50, SPY and QQQ from 2025, and for NEWETF, an ETF listed 2026-09-01."""
    asked = _Asked()
    asked.windows = []
    asked.series = {"TCS.NS": _ramp(3000, 3, "Asia/Kolkata"), "HDFCBANK.NS": _ramp(1600, 1, "Asia/Kolkata"),
                    "^NSEI": _ramp(25000, 50, "Asia/Kolkata"), "SPY": _ramp(600, 0.6, "America/New_York"),
                    "QQQ": _ramp(500, 1, "America/New_York"),
                    "NEWETF": _ramp(50, 0.1, "America/New_York", first="2026-09-01")}

    def get_closes(symbol, start, end):
        asked.append(symbol)
        asked.windows.append((symbol, start, end))
        closes = asked.series.get(symbol, pd.Series(dtype=float))
        if closes.empty:
            return closes
        day = closes.index.tz_localize(None)
        return closes[(day >= pd.Timestamp(start)) & (day < pd.Timestamp(end))]

    monkeypatch.setattr(settlement, "get_closes", get_closes)
    monkeypatch.setattr(settlement, "_now", lambda: NOW)
    monkeypatch.setattr(trading_graph, "get_current_date", lambda: "2026-10-30")    # a run date's limit
    return asked


class _Client:
    def get_llm(self):
        return FakeListChatModel(responses=["never asked"])


class _RunStarted(Exception):
    """The run got past its checks to building its state (the instrument's identity)."""


def _started(self, ticker, *args, **kwargs):
    raise _RunStarted(ticker)


def _log(tmp_path):
    return TradingMemoryLog({"memory_log_path": str(tmp_path / "memory.md")})


# --- R10: a lesson the release settled is read on the clock of the benchmark it measured it against -------

def _settled_by_the_release(log, ticker, lesson, plan=None):
    """An entry recorded and settled without its benchmark, as the release (27e6744) wrote every one."""
    if plan is None:
        log.store_decision(ticker, "2026-10-19", f"**Rating**: Buy\n\n{ticker} decision")
        log.update_with_outcome(ticker, "2026-10-19", 0.0, -0.05, 5, lesson, resolution_date="2026-10-27")
    else:
        log.store_decision(ticker, "2026-10-19", f"**Rating**: Buy\n\n{ticker} plan", intraday_plan=plan)
        log.update_with_intraday_outcome(ticker, "2026-10-19", {"scored": False}, lesson,
                                         resolution_date="2026-10-27")
    assert log.load_entries()[-1].get("benchmark") is None


# An index the release measured against SPY (no listing suffix), and a listing of its exchange a backfill runs.
INDEX_AND_LISTING = [("^N225", "7203.T"), ("^HSI", "0005.HK"), ("^FTSE", "BP.L"), ("^GDAXI", "SAP.DE"),
                     ("^AXJO", "BHP.AX"), ("^KS11", "005930.KS"), ("^TWII", "2330.TW"), ("^NSEI", "TCS.NS"),
                     ("^BSESN", "RELIANCE.BO"), ("^CNXIT", "INFY.NS"), ("^NSEBANK", "HDFCBANK.NS")]


@pytest.mark.unit
@pytest.mark.parametrize(("index", "listing"), INDEX_AND_LISTING)
def test_an_index_lesson_the_release_measured_against_spy_reaches_its_exchange_only_the_next_day(tmp_path, index,
                                                                                                listing):
    """A backfill dated the lesson's resolution day read it: its alpha includes SPY's close of that day, final
    3.5 to 14.8 hours after the backfill's own close."""
    log = _log(tmp_path)
    _settled_by_the_release(log, index, f"<<{index}>>")

    assert f"<<{index}>>" not in log.get_past_context(listing, n_cross=10, as_of="2026-10-27")
    assert f"<<{index}>>" in log.get_past_context(listing, n_cross=10, as_of="2026-10-28")


@pytest.mark.unit
@pytest.mark.parametrize(("settled", "run"), [("7203.T", "6758.T"), ("TCS.NS", "INFY.NS"), ("TCS.NS", "RELIANCE.BO"),
                                              ("BP.L", "HSBA.L"), ("NVDA", "AAPL")])
def test_a_listing_lesson_the_release_measured_against_its_exchanges_index_is_still_read_that_day(tmp_path, settled,
                                                                                                  run):
    log = _log(tmp_path)
    _settled_by_the_release(log, settled, f"<<{settled}>>")
    assert f"<<{settled}>>" in log.get_past_context(run, n_cross=10, as_of="2026-10-27")


@pytest.mark.unit
def test_an_intraday_lesson_has_no_benchmark_and_is_read_on_its_listings_clock(tmp_path):
    """A plan is scored on its session's bars alone, final at its listing's close."""
    log = _log(tmp_path)
    _settled_by_the_release(log, "^NSEI", "<<plan>>", plan={})
    assert "<<plan>>" in log.get_past_context("TCS.NS", n_cross=10, as_of="2026-10-27")


# --- R12: a setting the graph refuses ends a CLI run in one line ------------------------------------------

@pytest.mark.unit
def test_a_mistyped_cost_profile_variable_is_refused_at_startup_by_name(monkeypatch):
    """TRADINGAGENTS_COST_PROFILE=zerodha passed startup, then ended every analysis, US and crypto ones too,
    in a traceback once the setup questions were answered, without naming the variable."""
    monkeypatch.setenv("TRADINGAGENTS_COST_PROFILE", "zerodha")
    with pytest.raises(ValueError, match=r"^Invalid value for TRADINGAGENTS_COST_PROFILE: expected a profile .*"
                                         r"got 'zerodha'$"):
        build_default_config()
    monkeypatch.setenv("TRADINGAGENTS_COST_PROFILE", " India_Equity_Delivery ")
    assert build_default_config()["cost_profile"] == "india_equity_delivery"
    monkeypatch.setenv("TRADINGAGENTS_COST_PROFILE", "none")
    assert build_default_config()["cost_profile"] == "none"


def _selections(flags):
    """The setup questions' answers for an unattended swing run on ``flags``."""
    return {"ticker": flags.get("ticker") or "TCS.NS", "analysis_date": flags.get("date") or "2026-10-29",
            "analysts": [AnalystType.MARKET], "asset_type": "stock", "research_depth": 1,
            "quick_think_llm": "gpt-x", "deep_think_llm": "gpt-x", "backend_url": None, "llm_provider": "openai",
            "output_language": "English", "data_vendors": {}, "horizon": "swing",
            "benchmark_ticker": normalize_symbol(flags["benchmark"]) if flags.get("benchmark") else None}


class _Buffer:
    """The live view's message buffer, as a run drives it."""

    def __init__(self):
        self.messages, self.tool_calls, self.report_sections, self.agent_status = [], [], {}, {}
        self.selected_analysts, self._processed_message_ids = [], set()

    def init_for_analysis(self, analysts):
        self.selected_analysts = list(analysts)

    def add_message(self, kind, content):
        self.messages.append((0.0, kind, content))

    def add_tool_call(self, name, args):
        self.tool_calls.append((0.0, name, args))

    def update_report_section(self, *args):
        pass

    def update_agent_status(self, agent, status):
        self.agent_status[agent] = status


@pytest.fixture
def cli_run_parts(monkeypatch):
    """run_analysis on the real graph with fake model clients (never called), its answers from the flags, the
    live view's display stubbed. Returns the events of the live view, and what was printed, in order."""
    events = []

    class Live:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            events.append("live view opens")
            return self

        def stop(self):
            events.append("live view stops")

        def __exit__(self, *args):
            events.append("live view closes")
            return False

    monkeypatch.setattr(trading_graph, "create_tier_client", lambda config, tier, **kw: _Client())
    monkeypatch.setattr(cli_run, "unattended_gaps", lambda flags: [])     # CliRunner's stdin is no terminal
    monkeypatch.setattr(cli_run, "get_user_selections", _selections)
    monkeypatch.setattr(cli_run, "Live", Live)
    monkeypatch.setattr(cli_run, "create_layout", lambda: None)
    monkeypatch.setattr(cli_run, "update_display", lambda *a, **k: None)
    monkeypatch.setattr(cli_run, "message_buffer", _Buffer())
    monkeypatch.setattr(cli_run.console, "print", lambda *a, **k: events.append(("printed", " ".join(map(str, a)))))
    return events


@pytest.mark.unit
@pytest.mark.parametrize(("setting", "said"), [
    ({"cost_profile": "zerodha"}, "unknown cost profile 'zerodha'"),
    ({"capital_gains_overrides": {"long_term_after_months": 12.5}}, "long_term_after_months must be a whole"),
])
def test_a_setting_the_graph_refuses_ends_a_cli_run_in_one_line_not_a_traceback(monkeypatch, cli_run_parts,
                                                                                 setting, said):
    for key, value in setting.items():
        monkeypatch.setitem(DEFAULT_CONFIG, key, value)

    with pytest.raises(typer.Exit) as stopped:
        cli_run.run_analysis(flags={"ticker": "NVDA", "date": "2026-10-29", "save": False, "show": False})

    assert stopped.value.exit_code == 1
    ((_, line),) = [event for event in cli_run_parts if event[0] == "printed"]
    assert line.startswith("[red]") and said in line
    assert "live view opens" not in cli_run_parts


# --- R14: a past-dated CLI run is refused a benchmark unpriced at its date, in one line ----------------------

@pytest.mark.unit
def test_a_cli_run_given_a_past_date_checks_its_benchmark_there_before_the_questions(monkeypatch, yahoo):
    """--date 2026-06-01 --benchmark NEWETF passed the check of the last 30 days, went through the setup
    questions, then ended in a traceback from create_run_state."""
    monkeypatch.setattr(cli_main, "run_analysis", lambda **kw: pytest.fail("the run must not start"))
    result = CliRunner().invoke(cli_main.app, ["--ticker", "TCS.NS", "--date", "2026-06-01",
                                               "--benchmark", "NEWETF"])
    out = " ".join(result.output.split())
    assert result.exit_code == 1, out
    assert out.startswith("--benchmark NEWETF: Yahoo Finance has no prices for 'NEWETF' in the 30 days to "
                          "2026-06-01, so no decision")
    assert yahoo.windows == [("NEWETF", "2026-05-02", "2026-10-31")]


@pytest.mark.unit
def test_a_date_typed_at_the_prompt_its_benchmark_has_no_prices_for_ends_the_run_in_one_line(monkeypatch, yahoo,
                                                                                          cli_run_parts):
    """The date asked for interactively is checked inside the live view: the refusal came as a traceback."""
    monkeypatch.setattr(TradingAgentsGraph, "resolve_instrument_context",
                        lambda *a, **k: pytest.fail("the run must be refused before it starts"))
    with pytest.raises(typer.Exit) as stopped:
        cli_run.run_analysis(flags={"ticker": "TCS.NS", "date": "2026-06-01", "benchmark": "NEWETF",
                                    "save": False, "show": False})

    assert stopped.value.exit_code == 1
    # The screen is given back first: printed on the live view's, the line would go with it.
    assert [event if isinstance(event, str) else event[0] for event in cli_run_parts] == [
        "live view opens", "live view stops", "printed", "live view closes"]
    assert cli_run_parts[2][1].startswith("[red]Yahoo Finance has no prices for 'NEWETF' in the 30 days to "
                                          "2026-06-01, so no decision could be settled against it as a benchmark.")


# --- R15: a CLI run asks Yahoo about its benchmark once -----------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize(("day", "windows"), [
    ("2026-10-29", [("QQQ", "2026-09-29", "2026-10-31")]),     # from the run's date, through today
    ("2026-06-01", [("QQQ", "2026-05-02", "2026-10-31")]),
])
def test_a_cli_run_asks_yahoo_about_its_benchmark_once(monkeypatch, yahoo, cli_run_parts, day, windows):
    """The CLI's check, the graph's and the run date's each asked: twice for a run dated lately, three times
    for one dated more than 30 days back."""
    monkeypatch.setattr(TradingAgentsGraph, "resolve_instrument_context", _started)
    result = CliRunner().invoke(cli_main.app, ["--ticker", "TCS.NS", "--date", day, "--benchmark", "QQQ",
                                               "--analysts", "market", "--no-save", "--no-show"])

    assert isinstance(result.exception, _RunStarted), cli_run_parts     # past every check, to the run
    assert yahoo.windows == windows


# --- R13: a listed index is checked at a past run's own date ------------------------------------------------

@pytest.mark.unit
def test_a_past_run_before_a_listed_indexs_yahoo_history_starts_is_refused_on_one_request(tmp_path, monkeypatch,
                                                                                        yahoo):
    """Yahoo's NIFTY Financial Services history starts 2011-09-07: a 2010 backtest cell against it passed the
    check, paid for its model calls and stored a decision that could never settle."""
    yahoo.series["NIFTY_FIN_SERVICE.NS"] = _ramp(5000, 2, "Asia/Kolkata", first="2011-09-07")
    monkeypatch.setattr(trading_graph, "create_tier_client", lambda config, tier, **kw: _Client())
    monkeypatch.setattr(TradingAgentsGraph, "resolve_instrument_context", _started)
    config = {**DEFAULT_CONFIG, "results_dir": str(tmp_path / "results"), "data_cache_dir": str(tmp_path / "cache"),
              "memory_log_path": str(tmp_path / "memory.md"), "benchmark_ticker": "NIFTY FIN SERVICE"}
    graph = TradingAgentsGraph(["market"], config=config)
    assert list(yahoo) == []                        # a listed index is priced lately: no request for that

    with pytest.raises(ValueError, match="no prices for 'NIFTY_FIN_SERVICE.NS' in the 30 days to 2010-03-01"):
        graph.create_run_state("HDFCBANK.NS", "2010-03-01")
    for day in ("2012-03-01", "2026-10-29"):          # priced by then; and lately, as before
        with pytest.raises(_RunStarted):
            graph.create_run_state("HDFCBANK.NS", day)
    assert list(yahoo) == ["NIFTY_FIN_SERVICE.NS"]     # one answer for every cell after it


@pytest.mark.unit
def test_the_cli_refuses_such_a_date_before_its_questions(monkeypatch, yahoo):
    yahoo.series["NIFTY_FIN_SERVICE.NS"] = _ramp(5000, 2, "Asia/Kolkata", first="2011-09-07")
    monkeypatch.setattr(cli_main, "run_analysis", lambda **kw: pytest.fail("the run must not start"))
    result = CliRunner().invoke(cli_main.app, ["--ticker", "HDFCBANK.NS", "--date", "2010-03-01",
                                               "--benchmark", "NIFTY FIN SERVICE"])
    out = " ".join(result.output.split())
    assert result.exit_code == 1 and out.startswith(
        "--benchmark NIFTY FIN SERVICE: Yahoo Finance has no prices for 'NIFTY_FIN_SERVICE.NS' in the 30 days "
        "to 2010-03-01")


@pytest.mark.unit
@pytest.mark.parametrize("benchmark", ["NIFTY IT", "SPY", "^N225"])
def test_a_listed_index_or_a_mapped_benchmark_is_still_taken_lately_without_asking(yahoo, benchmark):
    assert settlement.check_benchmark(benchmark, DEFAULT_CONFIG, as_of="2026-10-01")
    assert settlement.check_benchmark(benchmark, DEFAULT_CONFIG)
    assert list(yahoo) == []


# --- R16: every volatility index is refused as a benchmark -------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("benchmark", ["^VXN", "^VIX3M", "^VVIX", "^VIX9D", "^VXD", "^RVX", "^GVZ", "^OVX", "^EVZ",
                                       "^VHSI", "^VIX1D", "^VIX6M", "^VIX1Y", "^VXEEM", "^AXVI", "^MOVE", "V2TX.DE"])
def test_every_volatility_index_yahoo_prices_is_refused_without_asking(yahoo, benchmark):
    """Only India VIX and VIX were: an explicit --benchmark ^VXN stored alphas that were a return less a swing
    in volatility."""
    yahoo.series[benchmark] = _ramp(20, 0.01, "America/New_York")
    with pytest.raises(ValueError, match="^" + re.escape(f"{benchmark} is a volatility index, which has no return")):
        settlement.check_benchmark(benchmark, DEFAULT_CONFIG)
    assert list(yahoo) == []


# --- from R8: --benchmark on an intraday run ---------------------------------------------------------------

@pytest.mark.unit
def test_an_intraday_run_says_its_benchmark_flag_does_not_apply(monkeypatch, yahoo):
    seen = {}
    monkeypatch.setattr(cli_main, "run_analysis", lambda **kw: seen.update(kw))
    result = CliRunner().invoke(cli_main.app, ["--ticker", "RELIANCE.NS", "--horizon", "intraday",
                                               "--benchmark", "NIFTY IT"])
    assert result.exit_code == 0, result.output
    assert result.output == "--benchmark applies to swing decisions; this intraday run does not use it.\n"
    assert seen["flags"]["horizon"] == "intraday" and list(yahoo) == []

    result = CliRunner().invoke(cli_main.app, ["--ticker", "RELIANCE.NS", "--horizon", "intraday"])
    assert result.exit_code == 0 and result.output == ""
