"""CLI fixes: the market command's next session, date, exchange and tables; broker symbols with a
series or suffix; --vendor and the tier models checked before a paid run; backtest watchlist names;
the /day-trader script's interpreter; error text shown as text; --arena-yes without the arena.

Vendors, AlphArena, the exchanges' clock and the CLI's stand-in are faked: nothing here reaches the
network, runs a model or starts TradingAgents.
"""

import datetime as dt
import io
import os
import shutil
from datetime import timedelta, timezone
from pathlib import Path

import pytest
import typer
from rich.console import Console
from typer.testing import CliRunner

import cli.arena as arena_cli
import cli.main as cli_main
import cli.market as cli_market
from cli import prompts, run, selections
from cli.models import AssetType
from tests.test_day_trader_skill import INTRADAY_FLAGS, Kit, Skill, needs_bash
from tradingagents.dataflows.symbols import normalize_symbol, resolve_symbol
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.markets import exchanges
from tradingagents.markets.watchlists import watchlist_symbols

IST = timezone(timedelta(hours=5, minutes=30))


def _clock(monkeypatch, moment: dt.datetime) -> None:
    """The exchanges' clock reads ``moment``."""

    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)

    monkeypatch.setattr(exchanges, "datetime", Clock)


def _market(monkeypatch, *args, answer="table"):
    """``tradingagents market ARGS`` with the vendor answering ``answer``; returns the result and its calls."""
    calls = []
    monkeypatch.setattr(cli_market, "route_to_vendor", lambda method, *a: calls.append((method, a)) or answer)
    return CliRunner().invoke(cli_main.app, ["market", *args]), calls


def _flat(text: str) -> str:
    return " ".join(text.split())   # rich wraps long lines


# --- market: the next session (#9) -----------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("moment", [
    dt.datetime(2026, 11, 6, 17, 0, tzinfo=IST),    # Friday, after the close
    dt.datetime(2026, 11, 7, 12, 0, tzinfo=IST),    # Saturday
    dt.datetime(2026, 11, 8, 10, 0, tzinfo=IST),    # the Muhurat Sunday, before its 14:05 pre-open
])
def test_before_muhurat_trading_the_next_session_is_the_special_one_with_its_hours(monkeypatch, moment):
    """It read 'Next regular session: Sun 08 Nov 2026, 09:15-15:30 IST', against the calendar."""
    _clock(monkeypatch, moment)
    result, _ = _market(monkeypatch)
    out = _flat(result.output)
    assert result.exit_code == 0, result.output
    assert ("Next session: Sun 08 Nov 2026, Muhurat trading (Diwali), 14:20-15:30 IST (pre-open from 14:05)."
            in out)
    assert "Next regular session: Mon 09 Nov 2026, 09:15-15:30 IST." in out
    assert "Sun 08 Nov 2026, 09:15-15:30" not in out


@pytest.mark.unit
def test_before_the_budget_sunday_the_next_session_is_named_as_special(monkeypatch):
    _clock(monkeypatch, dt.datetime(2026, 1, 31, 12, 0, tzinfo=IST))
    out = _flat(_market(monkeypatch)[0].output)
    assert ("Next session: Sun 01 Feb 2026, Union Budget special session, 09:15-15:30 IST (pre-open from 09:00)."
            in out)
    assert "Next regular session: Mon 02 Feb 2026, 09:15-15:30 IST." in out


@pytest.mark.unit
@pytest.mark.parametrize("moment, expected", [
    (dt.datetime(2026, 10, 30, 14, 40, tzinfo=IST), "Next regular session: Mon 02 Nov 2026, 09:15-15:30 IST."),
    (dt.datetime(2026, 11, 8, 14, 30, tzinfo=IST), "Next regular session: Mon 09 Nov 2026, 09:15-15:30 IST."),
])
def test_a_regular_next_session_reads_as_before(monkeypatch, moment, expected):
    _clock(monkeypatch, moment)
    out = _flat(_market(monkeypatch)[0].output)
    assert expected in out and "Next session:" not in out


# --- market: a blank exchange (#29) and the date (#30) -----------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("value", ["", "   "])
def test_a_blank_exchange_is_refused_like_an_unknown_one(monkeypatch, value):
    """A script passing --exchange "$EXCHANGE" with the variable unset got an AttributeError traceback."""
    result, calls = _market(monkeypatch, "--exchange", value)
    assert result.exit_code == 1 and isinstance(result.exception, SystemExit), result.output
    assert "--exchange takes nse or bse" in result.output and calls == []


@pytest.mark.unit
@pytest.mark.parametrize("value", ["2026-W44-5", "20261030", "2026-13-01", "30-10-2026", ""])
def test_the_market_date_is_yyyy_mm_dd_and_checked_before_anything_is_printed(monkeypatch, value):
    """ISO week and basic forms reached the vendor; 2026-13-01 printed the session, then 'month must be in 1..12'."""
    _clock(monkeypatch, dt.datetime(2026, 10, 30, 14, 40, tzinfo=IST))
    result, calls = _market(monkeypatch, "--date", value)
    assert result.exit_code == 1 and calls == []
    assert "YYYY-MM-DD" in result.output
    assert "National Stock Exchange" not in result.output


@pytest.mark.unit
def test_a_future_market_date_is_refused_on_the_exchange_s_clock(monkeypatch):
    # 20 Oct 2026 on NSE's clock: the 25th is still to come there, whatever the machine's date.
    _clock(monkeypatch, dt.datetime(2026, 10, 20, 11, 0, tzinfo=IST))
    result, calls = _market(monkeypatch, "--date", "2026-10-25")
    assert result.exit_code == 1 and calls == [] and "in the future" in result.output
    result, calls = _market(monkeypatch, "--date", "2026-10-20")
    assert result.exit_code == 0 and calls == [("get_market_context", ("^NSEI", "2026-10-20"))]
    # 2 Nov on NSE's clock: the 1st has passed there, even on a machine whose date is behind it.
    _clock(monkeypatch, dt.datetime(2026, 11, 2, 11, 0, tzinfo=IST))
    assert _market(monkeypatch, "--date", "2026-11-01")[0].exit_code == 0


# --- market and arena tables at 80 columns (#26) -------------------------------------------------

WATCHLIST = "\n".join([
    "# Watchlist as of 2026-10-30",
    "",
    "| Symbol | Last close | Close date | 1D | 1W | 1M | 3M | 1M vs benchmark | 3M vs benchmark |",
    "|---|---|---|---|---|---|---|---|---|",
    "| TCS.NS | 2519.60 | 2026-10-30 | +1.68% | +0.42% | +1.79% | +5.56% | +0.0 pts | +0.0 pts |",
    "| INFY.NS | 1178.05 | 2026-10-28 (29 Oct not yet published) | +0.08% | +0.42% | +1.79% | +5.56% "
    "| +0.0 pts | +0.0 pts |",
])


@pytest.mark.unit
def test_with_no_terminal_the_market_tables_are_printed_whole(monkeypatch):
    """rich fits a pipe's output to 80 columns: every close date read '2026-1…'."""
    monkeypatch.setenv("COLUMNS", "80")
    for name in ("FORCE_COLOR", "TTY_COMPATIBLE"):
        monkeypatch.delenv(name, raising=False)
    _clock(monkeypatch, dt.datetime(2026, 10, 30, 14, 40, tzinfo=IST))
    result, _ = _market(monkeypatch, "--watchlist", "TCS,INFY", answer=WATCHLIST)
    assert result.exit_code == 0, result.output
    assert "…" not in result.output
    assert "| INFY.NS | 1178.05 | 2026-10-28 (29 Oct not yet published) |" in result.output


@pytest.mark.unit
def test_at_a_narrow_terminal_a_market_table_cell_folds_rather_than_being_cut(monkeypatch):
    buffer = io.StringIO()
    monkeypatch.setattr(cli_market, "console", Console(file=buffer, width=80, force_terminal=True,
                                                       color_system=None))
    monkeypatch.setattr(cli_market, "route_to_vendor", lambda method, *a: WATCHLIST)
    _clock(monkeypatch, dt.datetime(2026, 10, 30, 14, 40, tzinfo=IST))
    cli_market.show_market(exchanges.NSE, "TCS,INFY", "2026-10-30")
    out = buffer.getvalue()
    assert "…" not in out
    assert "2519.60" in out and "1178.05" in out


POSITION = {"ticker": "AAPL", "direction": "long", "entry_price": 250.1, "current_price": 255.0, "size_pct": 1.9,
            "current_value": 1988.0, "unrealized_pnl": 38.1, "unrealized_pnl_pct": 1.96,
            "opened_at": "2026-10-28T14:00:00Z"}


class _StatusArena:
    """AlphArena's public answers for `arena status`, as the client returns them."""

    mode = "sandbox"

    def __init__(self, *args, **kwargs):
        pass

    def agent_profile(self, agent_id):
        return {"agent_id": agent_id, "name": "Agent", "portfolio_value": 100523.25, "cash": 50000}

    def agent_pnl(self, agent_id, period="all"):
        return {"return_pct": 0.52, "equity_curve": [{"date": "2026-10-29", "portfolio_value": 100523.25}]}

    def agent_portfolio(self, agent_id):
        return {"cash": 50000, "cash_pct": 49.7, "positions": [POSITION]}


@pytest.mark.unit
@pytest.mark.parametrize("terminal", [False, True])
def test_arena_tables_never_cut_a_value_short(monkeypatch, terminal):
    """At 80 columns the Value column read '1,988…' and Opened '2026…'."""
    buffer = io.StringIO()
    monkeypatch.setattr(arena_cli, "console", Console(file=buffer, width=80, force_terminal=terminal,
                                                      color_system=None))
    monkeypatch.setattr(arena_cli, "AlphArenaClient", _StatusArena)
    monkeypatch.setitem(DEFAULT_CONFIG, "arena_agent_id", "agent-1")
    result = CliRunner().invoke(cli_main.app, ["arena", "status"])
    out = buffer.getvalue()
    assert result.exit_code == 0, out
    assert "…" not in out
    if not terminal:   # with no terminal the table keeps its natural width: every value on one line
        assert "1,988.0" in out and "2026-10-28T14:00:00Z" in out and "100,523.25" in out


# --- broker symbols with a series or a suffix (#11) ---------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("raw, canonical", [
    ("NSE:RELIANCE-EQ", "RELIANCE.NS"),       # Fyers writes NSE:SBIN-EQ
    ("nse:sbin-eq", "SBIN.NS"),
    ("NSE: BAJAJ-AUTO-EQ", "BAJAJ-AUTO.NS"),
    ("NSE:M&M-EQ", "M&M.NS"),
    ("NSE:RELIANCE.NS", "RELIANCE.NS"),
    ("NSE:RELIANCE.NSE", "RELIANCE.NS"),
    ("BSE:RELIANCE.BO", "RELIANCE.BO"),
    ("BOM:RELIANCE.BSE", "RELIANCE.BO"),
    ("BSE:RELIANCE-EQ", "RELIANCE.BO"),
    # unchanged
    ("NSE:RELIANCE", "RELIANCE.NS"),
    ("NSE:BAJAJ-AUTO", "BAJAJ-AUTO.NS"),
    ("BSE:RELIANCE", "RELIANCE.BO"),
    ("NSE:NIFTY 50", "^NSEI"),
    ("RELIANCE-EQ", "RELIANCE.NS"),
])
def test_an_exchange_prefixed_symbol_drops_a_series_or_listing_suffix(raw, canonical):
    """'NSE:RELIANCE-EQ' became RELIANCE-EQ.NS, which Yahoo does not know, reported as a vendor outage."""
    assert normalize_symbol(raw) == canonical
    assert resolve_symbol(raw, "NSE") == canonical


@pytest.mark.unit
def test_a_scrip_code_with_the_bse_prefix_and_suffix_is_still_refused():
    with pytest.raises(ValueError, match="BSE scrip code"):
        resolve_symbol("BOM:500325.BO")


# --- --vendor: every tool of the category served by a vendor that can run (#23) ----------------


@pytest.mark.unit
@pytest.mark.parametrize("flag, tool", [
    ("fundamental_data=sec_edgar", "get_fundamentals"),
    ("news_data=agent_reach", "get_insider_transactions"),
])
def test_a_vendor_that_leaves_a_tool_of_its_category_unserved_is_refused(flag, tool):
    """Accepted before, the run then died mid-graph on the unserved tool, after the analysts had been paid for."""
    with pytest.raises(ValueError) as refused:
        prompts.parse_vendors([flag])
    message = str(refused.value)
    assert tool in message and f"{flag},yfinance" in message


@pytest.mark.unit
def test_a_vendor_without_its_key_needs_another_after_it(monkeypatch):
    monkeypatch.delenv("ALPHA_VANTAGE_API_KEY", raising=False)
    with pytest.raises(ValueError, match="ALPHA_VANTAGE_API_KEY") as refused:
        prompts.parse_vendors(["core_stock_apis=alpha_vantage"])
    assert "core_stock_apis=alpha_vantage,yfinance" in str(refused.value)
    assert prompts.parse_vendors(["core_stock_apis=alpha_vantage,yfinance"]) == {
        "core_stock_apis": "alpha_vantage,yfinance"}
    monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", "placeholder")
    assert prompts.parse_vendors(["core_stock_apis=alpha_vantage"]) == {"core_stock_apis": "alpha_vantage"}


@pytest.mark.unit
def test_the_documented_vendor_chains_are_accepted(monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)   # macro data is optional: without it the run goes on
    chains = {"fundamental_data": "sec_edgar,yfinance", "news_data": "agent_reach,yfinance",
              "web_research": "agent_reach", "macro_data": "fred", "intraday_data": "yfinance"}
    assert prompts.parse_vendors([f"{category}={chain}" for category, chain in chains.items()]) == chains


@pytest.mark.unit
def test_a_tool_with_its_own_vendor_needs_none_from_the_category(monkeypatch):
    monkeypatch.setitem(DEFAULT_CONFIG, "tool_vendors", {"get_fundamentals": "yfinance"})
    assert prompts.parse_vendors(["fundamental_data=sec_edgar"]) == {"fundamental_data": "sec_edgar"}


@pytest.mark.unit
def test_the_cli_refuses_a_partial_vendor_before_the_run(monkeypatch):
    monkeypatch.setattr(cli_main, "run_analysis", lambda **kw: pytest.fail("the run must not start"))
    result = CliRunner().invoke(cli_main.app, ["--vendor", "fundamental_data=sec_edgar"])
    assert result.exit_code == 1 and "get_fundamentals" in result.output


# --- the tier models: one variable is enough only on openai (#24) ---------------------------------

SETUP_ENV = {"TRADINGAGENTS_OUTPUT_LANGUAGE": "English", "TRADINGAGENTS_MAX_DEBATE_ROUNDS": "1",
             "TRADINGAGENTS_MAX_RISK_ROUNDS": "1"}
FLAGS = {"ticker": "NVDA", "date": "2026-09-23", "analysts": "market,news", "save": True, "show": False}


def _env(monkeypatch, **variables):
    for name, value in {**SETUP_ENV, **variables}.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


def _models(provider, quick, deep):
    return {"TRADINGAGENTS_LLM_PROVIDER": provider, "TRADINGAGENTS_QUICK_THINK_LLM": quick,
            "TRADINGAGENTS_DEEP_THINK_LLM": deep}


@pytest.mark.unit
@pytest.mark.parametrize("provider, quick, deep, gap", [
    ("claude_code", "sonnet", None, "TRADINGAGENTS_DEEP_THINK_LLM"),
    ("anthropic", None, "claude-opus-5-5", "TRADINGAGENTS_QUICK_THINK_LLM"),
    ("google", None, None, "TRADINGAGENTS_QUICK_THINK_LLM and TRADINGAGENTS_DEEP_THINK_LLM"),
])
def test_without_a_terminal_each_tier_needs_its_model_off_openai(monkeypatch, provider, quick, deep, gap):
    """The unset tier took the built-in OpenAI model: deep=gpt-6-sol on claude_code, which failed only
    at the Research Manager, after every analyst and debate call."""
    _env(monkeypatch, **_models(provider, quick, deep))
    gaps = selections.unattended_gaps(FLAGS)
    assert [g for g in gaps if g.startswith(gap)], gaps


@pytest.mark.unit
@pytest.mark.parametrize("provider, quick, deep", [
    ("openai", "gpt-6-luna", None),          # the built-in models are OpenAI's
    ("OpenAI", None, "gpt-6-sol"),
    ("claude_code", "sonnet", "opus"),
])
def test_no_model_is_missing_when_each_tier_has_one(monkeypatch, provider, quick, deep):
    _env(monkeypatch, **_models(provider, quick, deep))
    assert selections.unattended_gaps(FLAGS) == []


@pytest.mark.unit
def test_a_headless_run_with_one_model_off_openai_stops_before_any_setup(monkeypatch, capsys):
    _env(monkeypatch, **_models("claude_code", "sonnet", None))
    monkeypatch.setattr(run.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(run, "get_user_selections", lambda flags: pytest.fail("set up with a tier's model unknown"))
    with pytest.raises(typer.Exit):
        run.run_analysis(flags=dict(FLAGS))
    assert "TRADINGAGENTS_DEEP_THINK_LLM" in capsys.readouterr().out


@pytest.mark.unit
def test_at_a_terminal_only_the_tier_without_a_model_is_asked(monkeypatch):
    _env(monkeypatch, **_models("anthropic", "claude-sonnet-5", None))
    # What the environment overlay put on DEFAULT_CONFIG when the package was imported.
    monkeypatch.setitem(selections.DEFAULT_CONFIG, "llm_provider", "anthropic")
    monkeypatch.setitem(selections.DEFAULT_CONFIG, "quick_think_llm", "claude-sonnet-5")
    monkeypatch.setattr(selections.updates, "enabled", lambda: False)
    monkeypatch.setattr(selections, "ensure_api_key", lambda provider: None)
    monkeypatch.setattr(selections, "select_shallow_thinking_agent",
                        lambda *a, **k: pytest.fail("asked for the quick model the environment sets"))
    monkeypatch.setattr(selections, "select_deep_thinking_agent", lambda provider, default=None: "claude-opus-5-5")

    chosen = selections._prompt_selections({}, FLAGS)

    assert chosen["quick_think_llm"] == "claude-sonnet-5" and chosen["deep_think_llm"] == "claude-opus-5-5"


# --- backtest --watchlist takes names only (#25) --------------------------------------------------


def _sweep(monkeypatch, path, *args):
    monkeypatch.setenv("COLUMNS", "500")   # the file's path on one line
    monkeypatch.setitem(DEFAULT_CONFIG, "watchlists", {})
    monkeypatch.setitem(DEFAULT_CONFIG, "watchlists_file", str(path))
    monkeypatch.setattr(cli_main, "run_backtest", lambda *a, **k: pytest.fail("swept a watchlist that is not there"))
    return CliRunner().invoke(cli_main.app, ["backtest", "--exchange", "nse", "--start", "2026-10-01",
                                             "--end", "2026-10-15", *args])


@pytest.mark.unit
def test_backtest_refuses_an_unknown_watchlist_name_naming_the_lists_and_the_file(monkeypatch, tmp_path):
    """'bankz' was swept as the ticker BANKZ.NS: a full model run per date, for a symbol that does not exist."""
    path = tmp_path / "watchlists.json"
    path.write_text('{"banks": ["HDFCBANK", "ICICIBANK", "SBIN"]}', encoding="utf-8")
    result = _sweep(monkeypatch, path, "--watchlist", "bankz")
    out = _flat(result.output)
    assert result.exit_code == 1, out
    assert "'bankz'" in out and "banks" in out and str(path) in out


@pytest.mark.unit
def test_backtest_with_no_watchlists_file_says_the_file_is_missing(monkeypatch, tmp_path):
    missing = tmp_path / "nowhere" / "watchlists.json"
    result = _sweep(monkeypatch, missing, "--watchlist", "banks")
    out = _flat(result.output)
    assert result.exit_code == 1, out
    assert str(missing) in out and "does not exist" in out


@pytest.mark.unit
def test_names_only_looks_a_name_up_and_refuses_symbols(tmp_path):
    config = {"watchlists": {"it": ["TCS", "INFY"]}, "watchlists_file": str(tmp_path / "none.json")}
    assert watchlist_symbols("IT", config, str.upper, names_only=True) == ["TCS", "INFY"]
    with pytest.raises(ValueError, match="no watchlist named 'TCS,INFY'"):
        watchlist_symbols("TCS,INFY", config, str.upper, names_only=True)
    assert watchlist_symbols("TCS,INFY", config, str.upper) == ["TCS", "INFY"]   # `market` still reads symbols


# --- the /day-trader script's interpreter (#27, #28) --------------------------------------------


@pytest.fixture(scope="module")
def dt_kit(tmp_path_factory):
    return Kit(tmp_path_factory.mktemp("day-trader-fixes"))


@pytest.fixture
def day_trader(dt_kit, tmp_path):
    return Skill(dt_kit, tmp_path)


@pytest.mark.unit
@needs_bash
@pytest.mark.parametrize("relative", ["./py", "bin/py"])
def test_a_relative_tradingagents_python_is_the_one_in_the_caller_s_directory(day_trader, relative):
    """It was checked there, then run from the repo root, where it is not: status 126 or 127."""
    link = day_trader.home / relative
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(day_trader.kit.python)
    done = day_trader.run("RELIANCE", TRADINGAGENTS_PYTHON=relative)
    assert done.returncode == 0, done.stderr
    started = day_trader.started()
    assert started["program"] == "py" and started["args"] == ["-m", "cli.main", *INTRADAY_FLAGS]
    assert Path(started["cwd"]).resolve() == day_trader.repo.resolve()


# A python3 as old as macOS's /usr/bin/python3 (3.9): its version check fails, and so would the CLI.
OLD_PYTHON3 = "#!/bin/sh\nexit 1\n"


@pytest.mark.unit
@needs_bash
def test_a_python3_older_than_3_11_is_named_before_the_run(day_trader, tmp_path):
    """The run died on `from enum import StrEnum` with 'TradingAgents exited with status 1'."""
    old = tmp_path / "old-python"
    old.mkdir()
    (old / "python3").write_text(OLD_PYTHON3, encoding="utf-8")
    (old / "python3").chmod(0o755)
    done = day_trader.run("RELIANCE", TRADINGAGENTS_PYTHON=None, PATH=f"{old}{os.pathsep}{day_trader.kit.tools}")
    assert done.returncode == 1
    assert "3.11" in done.stderr and "TRADINGAGENTS_PYTHON" in done.stderr
    assert "exited with status" not in done.stderr and day_trader.started() is None


@pytest.mark.unit
@needs_bash
def test_the_repo_s_virtual_environment_comes_before_python3(day_trader):
    venv = day_trader.repo / ".venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").symlink_to(day_trader.kit.python)
    try:
        done = day_trader.run("RELIANCE", TRADINGAGENTS_PYTHON=None, on_path=("python3",))
    finally:
        shutil.rmtree(day_trader.repo / ".venv")
    assert done.returncode == 0, done.stderr
    started = day_trader.started()
    assert started["program"] == "python" and started["args"] == ["-m", "cli.main", *INTRADAY_FLAGS]


# --- error text is printed as text (#31) ------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("parse, args, shown", [
    (prompts.parse_ticker, ("[/x]",), "not a ticker symbol: '[/x]'"),
    (prompts.parse_analysis_date, ("[/x]",), "not a date: '[/x]'"),
    (prompts.parse_analysts, ("[market,news]", AssetType.STOCK), "unknown analyst [market, news]"),
])
def test_a_bad_flag_value_is_echoed_as_text(capsys, parse, args, shown):
    """'[/x]' crashed the CLI with a MarkupError; '[market,news]' vanished from the message."""
    with pytest.raises(typer.Exit):
        selections._from_flag(parse, *args)
    assert shown in _flat(capsys.readouterr().out)


@pytest.mark.unit
def test_a_bad_date_typed_at_the_prompt_is_echoed_as_text(monkeypatch, capsys):
    answers = iter(["[/x]", "2026-09-23"])
    monkeypatch.setattr(selections.typer, "prompt", lambda *a, **k: next(answers))
    assert selections.get_analysis_date("2026-09-23") == "2026-09-23"
    assert "not a date: '[/x]'" in capsys.readouterr().out


@pytest.mark.unit
def test_an_endpoint_error_is_printed_as_text(monkeypatch, capsys):
    def refuse(provider, url):
        raise ValueError("the endpoint [/x] is not usable")

    monkeypatch.setattr(selections, "check_endpoint", refuse)
    with pytest.raises(typer.Exit):
        selections._check_tier_providers("openai")
    assert "the endpoint [/x] is not usable" in capsys.readouterr().out


@pytest.mark.unit
def test_a_portfolio_error_keeps_the_field_and_value_it_names(monkeypatch, tmp_path):
    monkeypatch.setenv("COLUMNS", "500")
    book = tmp_path / "book.json"
    book.write_text('{"cash": 100, "positions": [{"ticker": "RELIANCE", "quantity": "ten"}]}', encoding="utf-8")
    monkeypatch.setattr(cli_main, "run_analysis", lambda **kw: pytest.fail("ran with a bad portfolio"))
    result = CliRunner().invoke(cli_main.app, ["--portfolio", str(book)])
    assert result.exit_code == 1
    assert "input_value='ten'" in result.output


@pytest.mark.unit
def test_a_backtest_setup_error_is_printed_as_text(monkeypatch):
    def fail(*args, **kwargs):
        raise ValueError("no key for [/x]")

    monkeypatch.setattr(cli_main, "run_backtest", fail)
    result = CliRunner().invoke(cli_main.app, ["backtest", "NVDA", "--start", "2026-06-01", "--end", "2026-06-01"])
    assert result.exit_code == 1 and isinstance(result.exception, SystemExit)
    assert "no key for [/x]" in result.output


@pytest.mark.unit
def test_a_report_that_cannot_be_saved_names_its_folder_as_text(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("COLUMNS", "500")

    class Unwritable:
        def default_report_path(self, ticker):
            return tmp_path / "[bold]reports"

        def save_reports(self, state, ticker, path, html=True):
            raise PermissionError(13, "Permission denied", str(path))

    run._offer_reports({"final_trade_decision": "**Rating**: Hold"}, Unwritable(), "NVDA", save=True, show=False)
    assert "[bold]reports" in capsys.readouterr().out


@pytest.mark.unit
def test_an_openrouter_listing_failure_is_printed_as_text(monkeypatch, capsys):
    import requests

    def refuse(*args, **kwargs):
        raise requests.ConnectionError("connection refused [/x]")

    monkeypatch.setattr(requests, "get", refuse)
    assert prompts._fetch_openrouter_models() == []
    assert "connection refused [/x]" in capsys.readouterr().out


# --- --arena-yes with the arena off (#32) -------------------------------------------------------


@pytest.mark.unit
def test_arena_yes_with_the_arena_off_is_flagged_before_the_run(monkeypatch):
    """It was said only after the run: minutes of model calls before learning nothing could be sent."""
    buffer = io.StringIO()
    monkeypatch.setattr(arena_cli, "console", Console(file=buffer, width=500, color_system=None))
    monkeypatch.setattr(arena_cli, "AlphArenaClient", lambda *a, **k: pytest.fail("a client with the arena off"))
    config = {**DEFAULT_CONFIG, "arena_enabled": False}

    arena_cli.check_before_run("AAPL", "2026-10-29", config, assume_yes=True)
    assert "AlphArena is off for this run" in buffer.getvalue() and "--arena-yes" in buffer.getvalue()

    buffer.truncate(0)
    arena_cli.check_before_run("AAPL", "2026-10-29", config)
    assert buffer.getvalue() == ""
