"""CLI fixes from the review of the earlier ones: market and AlphArena tables at a terminal keep
every value whole; backtest refuses a tier with no model of its own provider before the sweep and
prints its failure reasons as text; the /day-trader script checks whichever Python it was given.

Vendors, AlphArena, the sweep and the CLI's stand-in are faked: nothing here reaches the network,
runs a model or starts TradingAgents.
"""

import io
import os
import re
import shutil
from collections import Counter

import pytest
from rich.console import Console
from typer.testing import CliRunner

import cli.arena as arena_cli
import cli.main as cli_main
import cli.market as cli_market
from tests.test_day_trader_skill import Kit, Skill, needs_bash
from tradingagents.backtest import BacktestResult
from tradingagents.default_config import DEFAULT_CONFIG


def _terminal(monkeypatch, module, width: int) -> io.StringIO:
    """``module``'s console as a terminal ``width`` columns wide; returns what it prints."""
    buffer = io.StringIO()
    monkeypatch.setattr(module, "console", Console(file=buffer, width=width, force_terminal=True,
                                                   color_system=None))
    return buffer


def _not_whole(words: list[str], out: str) -> list[str]:
    """The words the output does not show whole, on one line, as often as they were given."""
    shown = Counter(out.split())
    return [word for word, count in Counter(words).items() if shown[word] < count]


# --- market and AlphArena tables at a terminal (R12, #26) -----------------------------------------

# The vendor's tables for 30 Oct 2026, as Yahoo's closes made them, and a late close.
CONTEXT = "\n".join([
    "| Series | Symbol | Last close | Close date | 1D | 1W | 1M | 3M |",
    "|---|---|---|---|---|---|---|---|",
    "| NIFTY 50 | ^NSEI | 23053.10 | 2026-10-30 | +0.51% | +2.00% | +1.48% | -5.20% |",
    "| BSE SENSEX | ^BSESN | 74459.53 | 2026-10-30 | +0.42% | +1.83% | +2.66% | -4.45% |",
    "| NIFTY Bank | ^NSEBANK | 54686.95 | 2026-10-30 | -0.41% | +0.74% | +0.79% | -4.31% |",
    "| India VIX | ^INDIAVIX | 15.3025 | 2026-10-30 | +1.21% | -9.24% | +14.11% | +25.84% |",
    "| USD/INR | USDINR=X | 94.95 | 2026-10-30 | -0.27% | +0.25% | -1.02% | -0.40% |",
    "| Brent crude (USD) | BZ=F | 113.49 | 2026-10-30 | -1.21% | -2.77% | +10.93% | +35.48% |",
])
WATCHLIST = "\n".join([
    "| Symbol | Last close | Close date | 1D | 1W | 1M | 3M | 1M vs benchmark | 3M vs benchmark |",
    "|---|---|---|---|---|---|---|---|---|",
    "| TCS.NS | 2502.60 | 2026-10-30 | +0.99% | -1.08% | +23.79% | +3.46% | +22.3 pts | +8.7 pts |",
    "| INFY.NS | 1178.05 | 2026-10-28 (29 Oct not yet published) | +0.08% | +0.42% | +1.79% | +5.56% "
    "| +1.3 pts (to 28 Oct) | -2.4 pts (to 28 Oct) |",
    "| HDFCBANK.NS | 692.80 | 2026-10-30 | +1.39% | +0.00% | -4.14% | -8.11% | -5.6 pts | -2.9 pts |",
])


def _cell_words(markdown: str) -> list[str]:
    rows = [line for line in markdown.splitlines() if line.startswith("|") and not line.startswith("|---")]
    return [word for row in rows for cell in row.strip("|").split("|") for word in cell.split()]


def _show_market(monkeypatch, width: int) -> str:
    buffer = _terminal(monkeypatch, cli_market, width)
    cli_market._show(CONTEXT)
    cli_market._show(WATCHLIST)
    return buffer.getvalue()


@pytest.mark.unit
@pytest.mark.parametrize("width", [50, 60, 70, 80, 90, 100, 120])
def test_at_a_terminal_every_market_value_is_whole_on_its_line(monkeypatch, width):
    """At 80 columns every close date read '2026-10-3' with the '0' alone on the next line."""
    out = _show_market(monkeypatch, width)
    assert _not_whole(_cell_words(CONTEXT) + _cell_words(WATCHLIST), out) == [], out
    assert "…" not in out
    assert max(len(line) for line in out.splitlines()) <= width, out


@pytest.mark.unit
def test_a_table_whose_values_fit_whole_stays_a_table_and_one_that_cannot_goes_row_by_row(monkeypatch):
    out = _show_market(monkeypatch, 80)
    lines = [line.split() for line in out.splitlines()]
    # The context table fits at 80 columns once 'Brent crude (USD)' wraps at its spaces: one row, one line.
    assert ["NIFTY", "50", "^NSEI", "23053.10", "2026-10-30", "+0.51%", "+2.00%", "+1.48%", "-5.20%"] in lines, out
    # The watchlist cannot: each row is a block of labels and values, a value beside its label.
    assert ["Last", "close", "1178.05"] in lines and ["Close", "date", "2026-10-30"] in lines, out


TRADE = {"trade_id": "tr_8f1c2b7a9d", "ticker": "NVDA", "direction": "long", "status": "closed",
         "fill_price": 1234.56, "exit_price": 1301.25, "size_pct": 1.95,
         "opened_at": "2026-10-28T14:00:00Z", "closed_at": "2026-10-29T19:45:12Z"}
POSITION = {"ticker": "AAPL", "direction": "long", "entry_price": 250.1, "current_price": 255.0, "size_pct": 1.9,
            "current_value": 1988.0, "unrealized_pnl": 38.1, "unrealized_pnl_pct": 1.96,
            "opened_at": "2026-10-28T14:00:00Z"}


class _Arena:
    """AlphArena's answers for `arena status` and `arena trades`, as the client returns them."""

    mode = "sandbox"

    def __init__(self, *args, **kwargs):
        pass

    def agent_profile(self, agent_id):
        return {"agent_id": agent_id, "name": "Agent", "portfolio_value": 100523.25, "cash": 50000}

    def agent_pnl(self, agent_id, period="all"):
        return {"return_pct": 0.52, "equity_curve": [{"date": "2026-10-29", "portfolio_value": 100523.25}]}

    def agent_portfolio(self, agent_id):
        return {"cash": 50000, "cash_pct": 49.7, "positions": [POSITION]}

    def list_trades(self, status="open"):
        return {"trades": [TRADE]}


@pytest.mark.unit
@pytest.mark.parametrize("width", [50, 60, 80, 100, 140])
def test_at_a_terminal_every_arena_value_is_whole_on_its_line(monkeypatch, width):
    """At 80 columns a fill read '1,234.' / '56' and Opened '2026-' / '10-28' / 'T14:0' / '0:00Z'."""
    buffer = _terminal(monkeypatch, arena_cli, width)
    monkeypatch.setattr(arena_cli, "AlphArenaClient", _Arena)
    monkeypatch.setitem(DEFAULT_CONFIG, "arena_agent_id", "agent-1")
    for command in (["arena", "status"], ["arena", "trades", "--status", "closed"]):
        result = CliRunner().invoke(cli_main.app, command)
        assert result.exit_code == 0, (result.output, result.exception)
    out = buffer.getvalue()
    values = ["1,234.56", "1,301.25", "1.95", "2026-10-29T19:45:12Z", "tr_8f1c2b7a9d",   # the trade
              "250.1", "255.0", "1,988.0", "38.1", "1.96",                                # the position
              "2026-10-28T14:00:00Z", "2026-10-28T14:00:00Z", "100,523.25"]
    assert _not_whole(values, out) == [], out
    assert "…" not in out
    assert max(len(line) for line in out.splitlines()) <= width, out


# --- backtest: each tier's model before the sweep (R13, #24) ----------------------------------------


def _backtest(monkeypatch, tmp_path, provider, quick=None, deep=None, quick_provider=None, deep_provider=None):
    """`tradingagents backtest` with this provider and these models; returns the result and the configs swept."""
    monkeypatch.setenv("COLUMNS", "500")
    monkeypatch.setitem(DEFAULT_CONFIG, "llm_provider", provider)
    monkeypatch.setitem(DEFAULT_CONFIG, "quick_think_provider", quick_provider)
    monkeypatch.setitem(DEFAULT_CONFIG, "deep_think_provider", deep_provider)
    monkeypatch.setenv("TRADINGAGENTS_QUICK_THINK_LLM", quick or "")   # blank reads as unset
    monkeypatch.setenv("TRADINGAGENTS_DEEP_THINK_LLM", deep or "")
    swept = []

    def sweep(names, dates, config, **kwargs):
        swept.append(config)
        return BacktestResult(run_id="r", log_path=tmp_path / "trading_memory.md")

    monkeypatch.setattr(cli_main, "run_backtest", sweep)
    result = CliRunner().invoke(cli_main.app, ["backtest", "RELIANCE", "--exchange", "nse",
                                               "--start", "2026-10-01", "--end", "2026-10-15"])
    return result, swept


@pytest.mark.unit
@pytest.mark.parametrize("models, named", [
    ({"provider": "claude_code", "quick": "sonnet"}, ["The deep tier runs on claude_code",
                                                      "TRADINGAGENTS_DEEP_THINK_LLM"]),
    ({"provider": "anthropic", "deep": "claude-opus-5-5"}, ["The quick tier runs on anthropic",
                                                            "TRADINGAGENTS_QUICK_THINK_LLM"]),
    ({"provider": "google"}, ["TRADINGAGENTS_QUICK_THINK_LLM", "TRADINGAGENTS_DEEP_THINK_LLM"]),
    # A tier on a provider of its own, not openai, with no model of its own.
    ({"provider": "openai", "quick": "gpt-6-luna", "deep_provider": "anthropic"},
     ["The deep tier runs on anthropic", "TRADINGAGENTS_DEEP_THINK_LLM"]),
])
def test_a_backtest_with_a_tier_off_openai_and_no_model_stops_before_the_sweep(
        monkeypatch, tmp_path, models, named):
    """The sweep ran with deep=gpt-6-sol on claude_code: every cell paid for its analysts and debate,
    then failed at the Research Manager."""
    result, swept = _backtest(monkeypatch, tmp_path, **models)
    assert result.exit_code == 1, result.output
    assert swept == []
    out = " ".join(result.output.split())
    assert all(text in out for text in named), out


@pytest.mark.unit
@pytest.mark.parametrize("models", [
    {"provider": "openai"},                                   # the built-in models, as before
    {"provider": "openai", "quick": "gpt-5.6-luna"},
    {"provider": "claude_code", "quick": "sonnet", "deep": "opus"},
    # The deep tier is on openai, where the built-in model is served.
    {"provider": "anthropic", "quick": "claude-sonnet-5", "deep_provider": "openai"},
])
def test_a_backtest_whose_tiers_each_have_a_model_is_swept(monkeypatch, tmp_path, models):
    result, swept = _backtest(monkeypatch, tmp_path, **models)
    assert result.exit_code == 0, result.output
    assert len(swept) == 1


# --- backtest: failure reasons as text (R15, #31) -------------------------------------------------

PYDANTIC = ("1 validation error for get_stock_data\nend_date\n  Field required "
            "[type=missing, input_value={'symbol': 'NVDA'}, input_type=dict]")


@pytest.mark.unit
def test_backtest_failure_reasons_and_its_log_are_printed_as_text(monkeypatch, tmp_path):
    """A reason's '[type=missing, ...]' vanished, and '[/x]' ended the CLI in a MarkupError after the sweep."""
    monkeypatch.setenv("COLUMNS", "500")
    log = tmp_path / "[/x] runs" / "trading_memory.md"
    monkeypatch.setattr(cli_main, "run_backtest", lambda names, dates, config, **kwargs: BacktestResult(
        run_id="r", log_path=log,
        failures=[("NVDA", dates[0], PYDANTIC), ("AAPL", dates[0], "no closes for [/x] in the window")],
        settlement_failures=[("NVDA", f"{dates[0]}: {PYDANTIC}"), ("AAPL", "no closes for [/x] in the window")]))
    result = CliRunner().invoke(cli_main.app, ["backtest", "NVDA,AAPL", "--start", "2026-06-01",
                                               "--end", "2026-06-01"])
    assert result.exit_code == 0, (result.output, result.exception)
    out = result.output
    assert f"Log: {log}" in out
    assert "failed: NVDA 2026-06-01: 1 validation error" in out
    assert "failed: AAPL 2026-06-01: no closes for [/x] in the window" in out
    assert "unsettled: NVDA: 2026-06-01: 1 validation error" in out
    assert "unsettled: AAPL: no closes for [/x] in the window" in out
    assert out.count("Field required [type=missing, input_value={'symbol': 'NVDA'}, input_type=dict]") == 2


# --- the /day-trader script checks the Python it is given (R14, #28) -------------------------------

# A Python as old as macOS's /usr/bin/python3 (3.9) fails the script's version check.
OLD_PYTHON = "#!/bin/sh\nexit 1\n"


@pytest.fixture(scope="module")
def dt_kit(tmp_path_factory):
    return Kit(tmp_path_factory.mktemp("day-trader-review"))


@pytest.fixture
def day_trader(dt_kit, tmp_path):
    return Skill(dt_kit, tmp_path)


def _old_python(folder, name):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(OLD_PYTHON, encoding="utf-8")
    (folder / name).chmod(0o755)
    return folder / name


@pytest.mark.unit
@needs_bash
@pytest.mark.parametrize("given", ["name", "path"])
def test_a_tradingagents_python_older_than_3_11_is_named_before_the_run(day_trader, tmp_path, given):
    """TRADINGAGENTS_PYTHON=python3 skipped the check and died on `from enum import StrEnum`."""
    old = _old_python(tmp_path / "old", "python3")
    done = day_trader.run("RELIANCE", TRADINGAGENTS_PYTHON="python3" if given == "name" else str(old),
                          PATH=f"{old.parent}{os.pathsep}{day_trader.kit.tools}")
    assert done.returncode == 1
    assert "is not Python 3.11 or later" in done.stderr and "TRADINGAGENTS_PYTHON" in done.stderr
    assert "exited with status" not in done.stderr and day_trader.started() is None
    assert list(day_trader.tmp.iterdir()) == []      # stopped before the run's log was made


@pytest.mark.unit
@needs_bash
def test_a_repo_venv_older_than_3_11_is_not_offered_as_the_fix(day_trader):
    venv = _old_python(day_trader.repo / ".venv" / "bin", "python")
    try:
        done = day_trader.run("RELIANCE", TRADINGAGENTS_PYTHON=None)
    finally:
        shutil.rmtree(day_trader.repo / ".venv")
    assert done.returncode == 1
    assert re.search(rf"{re.escape(str(venv.resolve()))} is not Python 3\.11 or later", done.stderr), done.stderr
    assert "for example" not in done.stderr and day_trader.started() is None
