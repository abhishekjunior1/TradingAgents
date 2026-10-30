"""CLI fixes from the second review: backtest prints each cell's progress line as text, so a
bracketed ticker fails as its own cell and the sweep still settles and sums up; the /day-trader
script leaves out its .venv example whenever the Python it refused is that venv's, by any name.

The graph and the CLI's stand-in are faked: nothing here reaches the network, runs a model or
starts TradingAgents.
"""

import os
import shutil

import pytest
from typer.testing import CliRunner

import cli.main as cli_main
import tradingagents.backtest as backtest
from tests.test_day_trader_skill import Kit, Skill, needs_bash
from tradingagents.dataflows.symbols import safe_ticker_component
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.memory import TradingMemoryLog
from tradingagents.memory.settlement import Settlement

# --- backtest: the progress line as text (R5; R15, #31) -------------------------------------------


class _Graph:
    """TradingAgentsGraph as a sweep meets it: a cell checks its ticker first, as the real graph
    does, then fails; settling a ticker leaves its last cell pending."""

    settled: list = []

    def __init__(self, selected_analysts=None, config=None, **kwargs):
        self.memory_log = TradingMemoryLog(config)

    def propagate(self, ticker, trade_date, asset_type="stock", portfolio=None):
        safe_ticker_component(ticker)
        raise RuntimeError(f"no closes for {ticker} [type=missing]")

    def settle_pending(self, ticker):
        _Graph.settled.append(ticker)
        return Settlement(failed=[(ticker, "2026-06-01", "price fetch [rate limited]")])


@pytest.mark.unit
@pytest.mark.parametrize("typed, ticker", [("[/x]", "[/X]"), ("[@x]", "[@X]"), ("[#x]", "[#X]")])
def test_a_bracketed_ticker_fails_as_its_own_cell_and_the_sweep_settles_and_sums_up(
        monkeypatch, tmp_path, typed, ticker):
    """'[/x]' in the progress line ended the sweep in a MarkupError, before the settlement pass and the
    summary; '[@x]' and '[#x]' vanished from it."""
    monkeypatch.setenv("COLUMNS", "500")
    monkeypatch.setattr(backtest, "get_current_date", lambda: "2026-10-30")
    monkeypatch.setattr(backtest, "TradingAgentsGraph", _Graph)
    monkeypatch.setattr(_Graph, "settled", [])
    for key, value in {"results_dir": str(tmp_path), "decision_horizon": "swing", "default_exchange": None,
                       "llm_provider": "openai", "quick_think_provider": None, "deep_think_provider": None}.items():
        monkeypatch.setitem(DEFAULT_CONFIG, key, value)

    result = CliRunner().invoke(cli_main.app, ["backtest", f"NVDA,{typed}", "--start", "2026-06-01",
                                               "--end", "2026-06-01"])

    assert result.exit_code == 0, (result.output, result.exception)
    out = result.output
    assert "[1/2] NVDA 2026-06-01\n" in out and f"[2/2] {ticker} 2026-06-01\n" in out
    assert "Resolved cells: 0 · pending: 0" in out and "Continue or settle this sweep: --run-id" in out
    assert "failed: NVDA 2026-06-01: no closes for NVDA [type=missing]" in out
    assert (f"failed: {ticker} 2026-06-01: ticker contains characters not allowed in a filesystem path: "
            f"'{ticker}'") in out
    assert _Graph.settled == ["NVDA", ticker]
    assert "unsettled: NVDA: 2026-06-01: price fetch [rate limited]" in out
    assert f"unsettled: {ticker}: 2026-06-01: price fetch [rate limited]" in out


# --- the /day-trader script: the refused repo venv by any name (R6; R14, #28) ---------------------

OLD_PYTHON = "#!/bin/sh\nexit 1\n"   # fails the version check, as macOS's 3.9 does


def _old_venv(folder):
    """A venv of a Python older than 3.11 at ``folder``: bin/python, and bin/python3 linked to it."""
    bin_dir = folder / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python").write_text(OLD_PYTHON, encoding="utf-8")
    (bin_dir / "python").chmod(0o755)
    (bin_dir / "python3").symlink_to("python")
    return bin_dir


@pytest.fixture(scope="module")
def dt_kit(tmp_path_factory):
    return Kit(tmp_path_factory.mktemp("day-trader-review2"))


@pytest.fixture
def day_trader(dt_kit, tmp_path):
    """A run of the shared kit's repo; the repo's .venv, a folder or a link, is removed afterwards."""
    yield Skill(dt_kit, tmp_path)
    venv = dt_kit.repo / ".venv"
    if venv.is_symlink():
        venv.unlink()
    elif venv.exists():
        shutil.rmtree(venv)


def _refused(done):
    assert done.returncode == 1, (done.stdout, done.stderr)
    assert "is not Python 3.11 or later" in done.stderr and "TRADINGAGENTS_PYTHON" in done.stderr


@pytest.mark.unit
@needs_bash
@pytest.mark.parametrize("named, linked_venv", [
    ("python3", False),            # TRADINGAGENTS_PYTHON=<repo>/.venv/bin/python3
    ("relative", False),           # TRADINGAGENTS_PYTHON=.venv/bin/python, from a linked folder
    ("on PATH", False),            # TRADINGAGENTS_PYTHON=python3, with the venv activated
    ("python3", True),             # .venv is a link to a venv kept elsewhere
    ("unset", True),               # the fallback finds it, as before
])
def test_a_refused_repo_venv_is_not_offered_as_the_fix_by_any_name(day_trader, tmp_path, named, linked_venv):
    """Only the spelling <repo_root>/.venv/bin/python left the example out: .venv/bin/python3, or a path
    through a linked folder, was told to set TRADINGAGENTS_PYTHON to the Python just refused."""
    repo = day_trader.repo
    if linked_venv:
        _old_venv(tmp_path / "venvs" / "ta")
        (repo / ".venv").symlink_to(tmp_path / "venvs" / "ta")
    else:
        _old_venv(repo / ".venv")
    env = {"TRADINGAGENTS_PYTHON": None}
    refused = repo.resolve() / ".venv" / "bin" / "python"     # the Python the message names
    if named == "python3":
        refused = repo / ".venv" / "bin" / "python3"
        env["TRADINGAGENTS_PYTHON"] = str(refused)
    elif named == "relative":
        # A shell that did `cd link` keeps that path as $PWD; the script's repo root is the real one.
        link = tmp_path / "link"
        link.symlink_to(repo)
        day_trader.home = link                    # where the script starts
        refused = link / ".venv" / "bin" / "python"
        env.update(TRADINGAGENTS_PYTHON=".venv/bin/python", PWD=str(link))
    elif named == "on PATH":
        refused = "python3"
        env.update(TRADINGAGENTS_PYTHON="python3",
                   PATH=f"{repo / '.venv' / 'bin'}{os.pathsep}{day_trader.kit.tools}")

    done = day_trader.run("RELIANCE", **env)

    _refused(done)
    assert f"day-trader: {refused} is not Python 3.11 or later" in done.stderr, done.stderr
    assert "for example" not in done.stderr, done.stderr
    assert day_trader.started() is None and list(day_trader.tmp.iterdir()) == []


@pytest.mark.unit
@needs_bash
def test_a_refused_python_outside_the_repo_venv_still_gets_the_example(day_trader, tmp_path):
    _old_venv(day_trader.repo / ".venv")
    other = _old_venv(tmp_path / "other")      # an old python3 of its own, first on PATH

    done = day_trader.run("RELIANCE", TRADINGAGENTS_PYTHON="python3",
                          PATH=f"{other}{os.pathsep}{day_trader.kit.tools}")

    _refused(done)
    assert f"(for example {day_trader.repo.resolve()}/.venv/bin/python)" in done.stderr, done.stderr
