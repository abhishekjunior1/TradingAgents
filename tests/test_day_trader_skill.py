"""The /day-trader Claude Code skill.

Its frontmatter lets only the user start it and pre-approves only its own
script. The script refuses a bad ticker or exchange before anything runs, runs
the CLI headless with the intraday flags, and prints the saved report, or the
end of the CLI's output when the run fails. A stand-in records how the CLI was
started; nothing here runs TradingAgents, a model or the network.
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / ".claude" / "skills" / "day-trader"
SCRIPT = SKILL_DIR / "scripts" / "day-trader.sh"
SCRIPT_RULE = "Bash(${CLAUDE_SKILL_DIR}/scripts/day-trader.sh *)"
INTRADAY_FLAGS = ["--horizon", "intraday", "--exchange", "nse", "--ticker", "RELIANCE",
                  "--analysts", "day_trader,market,news", "--save", "--no-show", "--html"]
# What the script runs besides bash's builtins. PATH offers only these and the
# stand-in, so no real python3 or tradingagents can be picked up.
TOOLS = ("bash", "dirname", "mktemp", "sed", "tr", "tail", "head", "cut", "wc", "awk", "rm")

# The CLI's stand-in. It passes the script's version check as a Python 3.11 or
# later would. Run as the CLI, it records how it was started, then does what
# STUB_MODE says: "saved" writes STUB_FILES into a report folder and announces
# it with rich, as cli/run.py does; "failed" ends like an unattended run missing
# a setting; "unsaved" finishes without a report.
STUB = r'''
import json, os, sys
from pathlib import Path

program, args = sys.argv[1], sys.argv[2:]
if args[:1] == ["-c"]:   # the version check
    sys.exit(0)
watched = ("TRADINGAGENTS_NO_UPDATE_CHECK", "COLUMNS", "TYPER_STANDARD_TRACEBACK",
           "FORCE_COLOR", "PY_COLORS", "TTY_COMPATIBLE")
Path(os.environ["STUB_RECORD"]).write_text(json.dumps({
    "program": os.path.basename(program),
    "args": args,
    "cwd": os.getcwd(),
    "stdin_is_devnull": os.path.samestat(os.fstat(0), os.stat(os.devnull)),
    "env": {name: os.environ.get(name) for name in watched},
}), encoding="utf-8")

mode = os.environ["STUB_MODE"]
if mode == "failed":
    for n in range(1, 61):
        print(f"progress line {n:03d}")
    print("\x1b[31m" + "x" * 2000 + "\x1b[0m")
    print("No terminal to answer the setup questions. Set:")
    print("  TRADINGAGENTS_LLM_PROVIDER")
    sys.exit(1)
if mode == "unsaved":
    print("Analysis Complete!")
    print("Error saving report: [Errno 28] No space left on device")
    sys.exit(0)

from rich.console import Console

folder = Path(os.environ["STUB_REPORT"])
for name, text in json.loads(os.environ["STUB_FILES"]).items():
    (folder / name).parent.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(text, encoding="utf-8")
console = Console()
console.print("\n[bold cyan]Analysis Complete![/bold cyan]\n")
console.print(f"\n[green]✓ Report saved to:[/green] {folder.resolve()}")
console.print("  [dim]Complete report:[/dim] complete_report.md")
console.print("  [dim]HTML report:[/dim] complete_report.html")
'''

needs_bash = pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None,
                                reason="the skill's script is a bash script")


def _frontmatter() -> tuple[dict, str]:
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\n"), "SKILL.md must open with its frontmatter"
    head, closed, body = text[4:].partition("\n---\n")
    assert closed, "the frontmatter is never closed"
    return yaml.safe_load(head), body


class Kit:
    """What every run shares: the script copied into a repo of its own, the shell
    tools PATH offers, and the stand-in CLI under each name the script looks for.

    Built once per module, since macOS checks each new executable the first time
    it runs; every name is a link to the one stand-in.
    """

    def __init__(self, root: Path):
        self.repo = root / "repo"
        self.script = self.repo / SCRIPT.relative_to(ROOT)
        self.script.parent.mkdir(parents=True)
        shutil.copy2(SCRIPT, self.script)
        (self.repo / "cli").mkdir()
        (self.repo / "cli" / "main.py").touch()
        self.tools = root / "tools"
        self.tools.mkdir()
        for tool in TOOLS:
            found = shutil.which(tool, path="/usr/bin:/bin") or shutil.which(tool)
            if found is None:
                pytest.skip(f"{tool} is not installed")
            (self.tools / tool).symlink_to(found)
        stub = root / "stub.py"
        stub.write_text(STUB, encoding="utf-8")
        cli = root / "stand-in"
        cli.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(stub))} "$0" "$@"\n',
                       encoding="utf-8")
        cli.chmod(0o755)
        self.python = root / "python"   # what TRADINGAGENTS_PYTHON names
        self.python.symlink_to(cli)
        self.folders = {}               # extra PATH folders, by the commands they hold
        for names in (("tradingagents", "python3"), ("python3",)):
            folder = root / "-".join(names)
            folder.mkdir()
            for name in names:
                (folder / name).symlink_to(cli)
            self.folders[names] = folder


class Skill:
    """One test's runs: the shared kit, with a log folder, report and record of its own."""

    def __init__(self, kit: Kit, tmp_path: Path):
        self.kit = kit
        self.repo = kit.repo
        self.home = tmp_path                     # also the directory the script starts in
        self.tmp = tmp_path / "tmp"              # TMPDIR, where the script keeps its log
        self.tmp.mkdir()
        self.record = tmp_path / "record.json"
        self.report = tmp_path / "results" / "reports" / "RELIANCE.NS_20261029_101500"
        self.files = {
            "complete_report.md": "# Trading Analysis Report: RELIANCE.NS\n\n- Analysis date: 2026-10-29\n"
                                  "- Rating: Buy\n\n## I. Analyst Team Reports\n\nAnalyst text.\n",
            "complete_report.html": "<html></html>\n",
            "5_portfolio/decision.md": "Rating: Buy. Long above the opening-range high.\n",
            "1_analysts/day_trader.md": "Target session 2026-10-30. Square-off 15:10 IST.\n",
        }

    def run(self, *args: str, mode: str = "saved", on_path: tuple[str, ...] = (),
            **env: str | None) -> subprocess.CompletedProcess:
        """Run the script with ``args``; ``on_path`` puts those stand-in commands on PATH,
        and ``env`` adds variables (None removes one)."""
        path = [str(self.kit.folders[on_path])] if on_path else []
        variables = {
            "PATH": os.pathsep.join([*path, str(self.kit.tools)]), "HOME": str(self.home),
            "TMPDIR": str(self.tmp), "PYTHONUTF8": "1", "TRADINGAGENTS_PYTHON": str(self.kit.python),
            "STUB_RECORD": str(self.record), "STUB_REPORT": str(self.report),
            "STUB_FILES": json.dumps(self.files), "STUB_MODE": mode, **env,
        }
        # stdin is a pipe here, so only the script's own redirect can make it /dev/null.
        return subprocess.run(
            [str(self.kit.script), *args], cwd=self.home, input="", capture_output=True,
            encoding="utf-8", errors="replace", timeout=60,
            env={k: v for k, v in variables.items() if v is not None},
        )

    def started(self) -> dict | None:
        """How the stand-in CLI was started, or None if it never was."""
        return json.loads(self.record.read_text(encoding="utf-8")) if self.record.exists() else None


@pytest.fixture(scope="module")
def kit(tmp_path_factory):
    return Kit(tmp_path_factory.mktemp("day-trader-skill"))


@pytest.fixture
def skill(kit, tmp_path):
    return Skill(kit, tmp_path)


@pytest.mark.unit
class TestSkillFile:
    def test_the_frontmatter_has_exactly_the_fields_the_skill_needs(self):
        meta, _ = _frontmatter()
        # Claude Code ignores unknown keys, so a misspelt one would silently drop its setting.
        assert set(meta) == {"name", "description", "argument-hint", "allowed-tools",
                             "disable-model-invocation"}
        assert meta["argument-hint"] == "[TICKER] [nse|bse]"

    def test_the_name_is_the_folder_name(self):
        meta, _ = _frontmatter()
        assert meta["name"] == SKILL_DIR.name == "day-trader"

    def test_the_description_fits_and_has_no_angle_brackets(self):
        description = _frontmatter()[0]["description"]
        assert isinstance(description, str)
        assert 0 < len(description) < 1024
        assert "<" not in description and ">" not in description

    def test_only_the_user_can_start_it(self):
        assert _frontmatter()[0]["disable-model-invocation"] is True

    def test_it_pre_approves_its_own_script_and_nothing_else(self):
        allowed = _frontmatter()[0]["allowed-tools"]
        rules = [allowed] if isinstance(allowed, str) else list(allowed)
        assert rules == [SCRIPT_RULE]
        # Bash(tradingagents:*) would pre-approve `tradingagents login` and `logout` too.
        assert not any("tradingagents" in rule for rule in rules)
        assert not any(rule.replace(" ", "") in ("Bash", "Bash(*)") for rule in rules)

    def test_the_body_is_short_and_states_the_rules_first(self):
        _, body = _frontmatter()
        lines = body.strip().splitlines()
        assert len(lines) < 150
        rules = "\n".join(lines[:12]).lower()
        assert "paper trading" in rules and "real order" in rules
        assert "not investment advice" in body
        assert "${CLAUDE_SKILL_DIR}/scripts/day-trader.sh" in body
        assert "$ARGUMENTS" in body
        # $0, $1 ... are substituted wherever they appear; the body reads $ARGUMENTS only.
        assert re.search(r"\$\d", body) is None

    @needs_bash
    def test_the_script_is_executable(self):
        assert os.access(SCRIPT, os.X_OK)


@pytest.mark.unit
@needs_bash
class TestScript:
    def test_a_valid_run_passes_exactly_the_intraday_flags(self, skill):
        done = skill.run("RELIANCE")
        assert done.returncode == 0, done.stderr
        started = skill.started()
        assert started["program"] == "python"
        assert started["args"] == ["-m", "cli.main", *INTRADAY_FLAGS]
        # From the repo root, found from the script's own path, with no terminal to prompt on.
        assert Path(started["cwd"]).resolve() == skill.repo.resolve()
        assert started["stdin_is_devnull"] is True
        assert started["env"]["TRADINGAGENTS_NO_UPDATE_CHECK"] == "1"

    def test_a_finished_run_prints_the_folder_decision_and_day_trader_report(self, skill):
        done = skill.run("RELIANCE", "nse")
        assert done.returncode == 0, done.stderr
        folder = skill.report.resolve()
        assert f"Report folder: {folder}" in done.stdout
        assert f"HTML report: {folder / 'complete_report.html'}" in done.stdout
        assert "- Rating: Buy" in done.stdout
        assert "Analyst text." not in done.stdout   # the header only, not the whole report
        assert "Rating: Buy. Long above the opening-range high." in done.stdout
        assert "Target session 2026-10-30. Square-off 15:10 IST." in done.stdout
        assert list(skill.tmp.iterdir()) == []      # a finished run's log is removed

    @pytest.mark.parametrize("given, passed", [("bse", "bse"), ("BSE", "bse"), ("Nse", "nse"), ("", "nse")])
    def test_the_exchange_is_nse_or_bse_in_any_case(self, skill, given, passed):
        assert skill.run("TCS", given).returncode == 0
        args = skill.started()["args"]
        assert args[args.index("--exchange") + 1] == passed

    @pytest.mark.parametrize("ticker", ["M&M", "BAJAJ-AUTO.NS", "^NSEI", "INR=X", "A" * 20])
    def test_a_ticker_with_symbols_reaches_the_cli_unchanged(self, skill, ticker):
        assert skill.run(ticker).returncode == 0
        args = skill.started()["args"]
        assert args[args.index("--ticker") + 1] == ticker

    @pytest.mark.parametrize("ticker", [
        "REL IANCE", "RELIANCE;ls", "`id`", "$(id)", "A" * 21, "", "RELIANCE|cat", "TCS\nnse",
        "a/b", "..", "--save", "RELİANCE",
    ])
    def test_a_bad_ticker_is_refused_before_anything_runs(self, skill, ticker):
        done = skill.run(ticker, "nse")
        assert done.returncode == 2
        assert "ticker" in done.stderr
        assert skill.started() is None
        assert list(skill.tmp.iterdir()) == []

    @pytest.mark.parametrize("exchange", ["nyse", "nse;ls", "nse ", "$(id)", "bse2", "NSE.NS"])
    def test_a_bad_exchange_is_refused_before_anything_runs(self, skill, exchange):
        done = skill.run("RELIANCE", exchange)
        assert done.returncode == 2
        assert "exchange" in done.stderr
        assert skill.started() is None
        assert list(skill.tmp.iterdir()) == []

    @pytest.mark.parametrize("args", [(), ("RELIANCE", "nse", "extra")])
    def test_a_wrong_number_of_arguments_is_refused(self, skill, args):
        done = skill.run(*args)
        assert done.returncode == 2
        assert "usage: day-trader.sh TICKER [nse|bse]" in done.stderr
        assert skill.started() is None

    def test_help_prints_the_usage(self, skill):
        done = skill.run("--help")
        assert done.returncode == 0
        assert "usage: day-trader.sh TICKER [nse|bse]" in done.stdout
        assert skill.started() is None

    def test_a_failed_run_shows_the_last_lines_of_its_output(self, skill):
        done = skill.run("RELIANCE", mode="failed")
        assert done.returncode == 1
        assert "TradingAgents exited with status 1." in done.stderr
        assert "No terminal to answer the setup questions. Set:" in done.stderr
        assert "  TRADINGAGENTS_LLM_PROVIDER" in done.stderr
        assert "progress line 060" in done.stderr
        assert "progress line 001" not in done.stderr     # the end only
        assert "\x1b" not in done.stderr
        assert "x" * 300 in done.stderr and "x" * 301 not in done.stderr   # long lines cut
        (log,) = skill.tmp.iterdir()                       # kept, and named
        assert str(log) in done.stderr

    def test_a_run_that_saves_no_report_is_a_failure(self, skill):
        done = skill.run("RELIANCE", mode="unsaved")
        assert done.returncode == 1
        assert "its output names no saved report" in done.stderr
        assert "Error saving report: [Errno 28] No space left on device" in done.stderr

    def test_a_named_folder_without_the_complete_report_is_a_failure(self, skill):
        del skill.files["complete_report.md"]
        done = skill.run("RELIANCE")
        assert done.returncode == 1
        assert "its output names no saved report" in done.stderr

    def test_report_files_are_bounded_and_lose_control_characters(self, skill):
        skill.files["5_portfolio/decision.md"] = "D" * 30000
        skill.files["1_analysts/day_trader.md"] = "levels \x1b[31mred\x1b[0m\x07 end\n"
        done = skill.run("RELIANCE")
        assert done.returncode == 0, done.stderr
        assert "D" * 10000 in done.stdout and "D" * 10001 not in done.stdout
        assert "[cut at 10000 of 30000 bytes" in done.stdout
        assert "levels red end" in done.stdout
        assert "\x1b" not in done.stdout and "\x07" not in done.stdout

    def test_a_missing_day_trader_report_is_named(self, skill):
        del skill.files["1_analysts/day_trader.md"]
        done = skill.run("RELIANCE")
        assert done.returncode == 0, done.stderr
        assert "===== Day-trader report (1_analysts/day_trader.md) =====\n(not in this report)" in done.stdout

    def test_a_long_folder_path_is_read_whole_from_plain_output(self, skill):
        # Wider than the 80 columns rich uses for a pipe; the colour variables
        # would make rich draw for a terminal.
        skill.report = skill.home / ("long-" * 30) / "reports" / "RELIANCE.NS_20261029_101500"
        done = skill.run("RELIANCE", FORCE_COLOR="1", PY_COLORS="1", TTY_COMPATIBLE="1")
        assert done.returncode == 0, done.stderr
        assert f"Report folder: {skill.report.resolve()}\n" in done.stdout
        env = skill.started()["env"]
        assert env["FORCE_COLOR"] is None and env["PY_COLORS"] is None and env["TTY_COMPATIBLE"] is None
        assert env["TYPER_STANDARD_TRACEBACK"] == "1"

    @pytest.mark.parametrize("python, on_path, program, prefix", [
        ("set", ("tradingagents", "python3"), "python", ["-m", "cli.main"]),
        ("", ("tradingagents", "python3"), "tradingagents", []),   # empty counts as unset
        (None, ("python3",), "python3", ["-m", "cli.main"]),
    ])
    def test_the_cli_is_tradingagents_python_then_tradingagents_then_python3(
            self, skill, python, on_path, program, prefix):
        env = {} if python == "set" else {"TRADINGAGENTS_PYTHON": python}
        done = skill.run("RELIANCE", on_path=on_path, **env)
        assert done.returncode == 0, done.stderr
        started = skill.started()
        assert started["program"] == program
        assert started["args"] == [*prefix, *INTRADAY_FLAGS]
        assert Path(started["cwd"]).resolve() == skill.repo.resolve()

    def test_with_no_way_to_run_the_cli_it_names_what_to_set(self, skill):
        done = skill.run("RELIANCE", TRADINGAGENTS_PYTHON=None)
        assert done.returncode == 1
        assert "TRADINGAGENTS_PYTHON" in done.stderr and "tradingagents command on PATH" in done.stderr
        assert skill.started() is None

    def test_a_tradingagents_python_that_cannot_run_is_named(self, skill):
        done = skill.run("RELIANCE", TRADINGAGENTS_PYTHON=str(skill.home / "no-such-python"))
        assert done.returncode == 1
        assert "TRADINGAGENTS_PYTHON does not name a program that can run" in done.stderr
        assert skill.started() is None
