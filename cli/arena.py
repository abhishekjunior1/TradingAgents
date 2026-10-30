"""AlphArena paper trades from the CLI: the offer after an analysis, and ``tradingagents arena``.

AlphArena (alpharena.io) is a public arena where trading agents compete with simulated
money; nothing here places a real order. After a run, its decision is offered only when
arena_enabled is on (``--arena``, or TRADINGAGENTS_ARENA) and the run may be sent: a
swing decision on a US stock or ETF, dated today in New York, never a backtest. Before
the run, the same rules say whether it could be, and the key it would need is checked.
The person sees the paper trade and confirms it, unless they passed ``--arena-yes``, and
every open and close sent goes into the ledger. What AlphArena answers, other agents' names
included, is printed escaped for the person to read; none of it reaches a prompt.
"""

from __future__ import annotations

import math
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from enum import StrEnum
from typing import Annotated, NoReturn

import typer
from rich import box
from rich.console import Console
from rich.markup import escape
from rich.table import Column, Table
from rich.text import Text

from cli.display import console
from cli.tables import fit
from tradingagents.arena import (
    AlphArenaClient,
    ArenaError,
    ArenaLedger,
    ArenaPlan,
    close_plan,
    ledger_path,
    plain_text,
    propose_plan,
    refusal,
    report_note,
    send_plan,
    ticker_refusal,
)
from tradingagents.arena.alpharena import KEY_ENV, PERIODS, SORTS, STATUSES
from tradingagents.dataflows.config import run_config
from tradingagents.dataflows.errors import VendorError
from tradingagents.default_config import DEFAULT_CONFIG

# The questions asked before anything is sent; both default to no.
CONFIRM = "Send this paper trade to AlphArena (simulated money)?"
CONFIRM_CLOSE = "Close these paper trades on AlphArena (simulated money)?"
_PUBLIC = "Public AlphArena figures as it reports them; simulated money."


# --- the offer after an analysis ------------------------------------------------------------


def check_before_run(ticker: str, trade_date: str, config: dict, asset_type: str = "stock",
                     assume_yes: bool = False) -> None:
    """Before an analysis with arena_enabled on, whether its decision could go to AlphArena.

    When it could not (``refusal``, or no terminal to confirm at and no --arena-yes) the
    reason is printed and the analysis goes on: it is still useful. When it could, the key
    is checked now, with no request, rather than after a paid run: a missing key, or one
    for the other mode, ends the run before it starts, with exit code 2. The key is never
    shown. The offer after the run checks everything again. With arena_enabled off, only
    --arena-yes is remarked on: it will send nothing.
    """
    if not config.get("arena_enabled"):
        if assume_yes:
            _say("[dim]AlphArena is off for this run (--arena or TRADINGAGENTS_ARENA turns it on), "
                 "so --arena-yes will send nothing.[/dim]")
        return
    with run_config(config):
        reason = refusal(ticker, trade_date, horizon=config.get("decision_horizon") or "swing",
                         asset_type=asset_type or "stock")
        if reason is None and not assume_yes and not _at_terminal():
            reason = "There is no terminal to confirm the paper trade at and no --arena-yes"
        if reason:
            _say(f"[dim]AlphArena: this run's decision will not be sent. {escape(reason)}.[/dim]")
            return
        client = AlphArenaClient(config.get("arena_mode"))
        try:
            client.check_key()
        except ArenaError as exc:
            _say(f"[red]AlphArena is on for this run, but its key cannot be used: {escape(exc.message)}.[/red]")
            _say(f"The analysis has not started. Set {KEY_ENV} to your AlphArena {escape(client.mode)} key, "
                 "or rerun with --no-arena.")
            raise typer.Exit(code=2) from None


def offer_paper_trade(final_state: dict, config: dict, assume_yes: bool = False) -> str | None:
    """Offer a finished run's decision to AlphArena as a paper trade; called after the live view.

    Returns the saved report's line on what AlphArena accepted, or None. Nothing is sent
    unless arena_enabled is on and ``refusal`` lets the run through; the person sees the
    trade and is asked first, unless ``assume_yes`` (--arena-yes). With no terminal and
    no --arena-yes nothing is sent. Nothing here fails the run, whose report is still to
    be offered: a problem is printed instead.
    """
    if not config.get("arena_enabled"):
        if assume_yes:
            _say("[dim]AlphArena is off for this run (--arena or TRADINGAGENTS_ARENA turns it on), "
                 "so --arena-yes sent nothing.[/dim]")
        return None
    try:
        with run_config(config):
            return _offer(final_state, config, assume_yes)
    except Exception as exc:  # noqa: BLE001 — the run is done, and its report is still to be saved
        stopped = f"stopped by an unexpected {type(exc).__name__}"
    except KeyboardInterrupt:  # Ctrl+C while AlphArena answers: this step stops, the report is still offered
        stopped = "interrupted"
    _say(f"[yellow]AlphArena: {escape(stopped)}; `tradingagents arena trades` lists what reached "
         "AlphArena.[/yellow]")
    return None


def _offer(final_state: dict, config: dict, assume_yes: bool) -> str | None:
    horizon = config.get("decision_horizon") or "swing"
    reason = refusal(final_state.get("company_of_interest", ""), final_state.get("trade_date", ""),
                     horizon=horizon, asset_type=final_state.get("asset_type") or "stock")
    if reason:
        _say(f"[dim]AlphArena: nothing sent. {escape(reason)}.[/dim]")
        return None
    # Decided before any request: with nobody to ask, the key is not used at all.
    if not assume_yes and not _at_terminal():
        _say("[yellow]AlphArena: no terminal to confirm the paper trade at and no --arena-yes, "
             "so nothing was sent.[/yellow]")
        return None
    client = AlphArenaClient(config.get("arena_mode"))
    try:
        plan = propose_plan(final_state, client, horizon=horizon, config=config)
    except (ArenaError, VendorError) as exc:
        _say(f"[yellow]AlphArena: nothing sent ({escape(str(exc))}).[/yellow]")
        return None
    _show_plan("AlphArena paper trade", plan, client.mode)
    if plan.refused or plan.empty:
        return None
    ledger = ArenaLedger(ledger_path(config))
    if not _ledger_ready(ledger):
        return None
    if not assume_yes and not _confirmed(CONFIRM):
        _say("Nothing was sent to AlphArena.")
        return None
    results = send_plan(plan, client, ledger)
    _show_results(results, ledger)
    return report_note(results)


def _show_plan(title: str, plan: ArenaPlan, mode: str) -> None:
    _say(f"\n[bold]{title}[/bold] ({escape(mode)} mode, simulated money):")
    for line in plan.describe().splitlines():
        _say(f"  {escape(line)}")


def _ledger_ready(ledger: ArenaLedger) -> bool:
    """Whether the ledger can be written: nothing is sent that could not be recorded."""
    try:
        ledger.path.parent.mkdir(parents=True, exist_ok=True)
        with open(ledger.path, "ab"):
            pass
    except OSError as exc:
        _say(f"[red]AlphArena: the ledger {escape(str(ledger.path))} cannot be written "
             f"({escape(type(exc).__name__)}), so nothing was sent.[/red]")
        return False
    return True


def _at_terminal() -> bool:
    return bool(sys.stdin and sys.stdin.isatty())


def _confirmed(question: str) -> bool:
    """The person's answer, no by default; Ctrl+C or a closed input counts as no."""
    try:
        return typer.confirm(question, default=False)
    except typer.Abort:
        return False


def _show_results(results: list[dict], ledger: ArenaLedger) -> None:
    """One line per trade, with AlphArena's answer as it gave it, then where they are recorded."""
    for result in results:
        _say(_result_line(result))
    if any("response" in result or "error" in result for result in results):
        _say(f"[dim]Recorded in {escape(str(ledger.path))}[/dim]")


def _result_line(result: dict) -> str:
    """One request's outcome as a line of markup, every value from it escaped."""
    kind, _, direction = str(result.get("action", "")).partition("_")
    ticker = result.get("ticker", "")
    if kind == "open":
        done, todo = f"Opened a {direction} in {ticker}", f"open a {direction} in {ticker}"
    else:
        trade = (result.get("payload") or {}).get("trade_id")
        done = f"Closed the {direction} in {ticker} (trade {trade})"
        todo = f"close the {direction} in {ticker} (trade {trade})"
    response, error = result.get("response"), result.get("error")
    if isinstance(response, dict):
        return f"[green]✓ {escape(done)}[/green]{escape(_answered(kind, response))}"
    if isinstance(error, dict):
        failure = ArenaError(str(error.get("code") or "ERROR"), str(error.get("message") or ""),
                             error.get("status") if isinstance(error.get("status"), int) else None)
        return f"[red]✗ Could not {escape(todo)}: {escape(str(failure))}[/red]"
    return f"[yellow]Did not {escape(todo)}: {escape(str(result.get('skipped') or 'not sent'))}[/yellow]"


def _answered(kind: str, response: dict) -> str:
    """What AlphArena said about a request it accepted, as it reported it."""
    parts = []
    if kind == "open":
        parts.append(f"trade {_shown(response.get('trade_id'), 64)}")
        if _number(response.get("fill_price")):
            parts.append(f"filled at {_shown(response['fill_price'])} (AlphArena's price)")
    else:
        if _number(response.get("exit_price")):
            parts.append(f"exit at {_shown(response['exit_price'])} (AlphArena's price)")
        if _number(response.get("realized_pnl")):
            pct = response.get("realized_pnl_pct")
            parts.append(f"realized P&L {_shown(response['realized_pnl'])}"
                         + (f" ({_shown(pct)}%)" if _number(pct) else ""))
    warnings = [warning for warning in _list(response.get("warnings")) if isinstance(warning, str)]
    if warnings:
        parts.append("AlphArena warns: " + _shown(warnings, 120))
    return ": " + "; ".join(parts) if parts else ""


# --- tradingagents arena ----------------------------------------------------------------------

app = typer.Typer(
    name="arena",
    no_args_is_help=True,
    help="AlphArena paper trading, simulated money on US stocks and ETFs (docs/arena.md): your "
    "agent's standing, the leaderboard, your trades, and closing them.",
)

# The documented values, as choices, and the defaults the commands use.
Period = StrEnum("Period", {period: period for period in PERIODS})
Sort = StrEnum("Sort", {sort: sort for sort in SORTS})
Status = StrEnum("Status", {status: status for status in STATUSES})
_THIRTY_DAYS, _BY_RETURN, _OPEN = Period("30d"), Sort("return"), Status("open")


def _fail(message: str) -> NoReturn:
    _say(f"[red]{escape(message)}[/red]")
    raise typer.Exit(code=1)


@contextmanager
def _arena() -> Iterator[AlphArenaClient]:
    """A client on the configured mode; an AlphArena error ends the command with its message."""
    try:
        with run_config(DEFAULT_CONFIG):
            yield AlphArenaClient()
    except ArenaError as exc:
        _say(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=1) from None


@app.command()
def status(period: Annotated[Period, typer.Option("--period", help="The period of the P&L figures")] = _THIRTY_DAYS):
    """Your agent's public profile, P&L and open positions (TRADINGAGENTS_ARENA_AGENT_ID; no key needed)."""
    agent_id = DEFAULT_CONFIG.get("arena_agent_id")
    if not agent_id:
        _fail("Set TRADINGAGENTS_ARENA_AGENT_ID to your AlphArena agent id.")
    with _arena() as client:
        profile = client.agent_profile(agent_id)
        pnl = client.agent_pnl(agent_id, period.value)
        portfolio = client.agent_portfolio(agent_id)
    stats = _dict(profile.get("stats"))
    _say(f"[bold]AlphArena agent {escape(_shown(profile.get('agent_id') or agent_id, 64))}[/bold]: "
         f"{escape(_shown(profile.get('name'), 80))}")
    _say(_pairs("Profile", (
        ("Status", profile.get("status")), ("Tier", profile.get("tier")),
        ("Portfolio value", profile.get("portfolio_value")), ("Cash", profile.get("cash")),
        ("Trades", stats.get("total_trades")), ("Open trades", stats.get("open_trades")),
        ("Total return (%)", stats.get("total_return_pct")), ("Win rate", stats.get("win_rate")),
        ("Sharpe ratio", stats.get("sharpe_ratio")), ("Max drawdown (%)", stats.get("max_drawdown_pct")),
        ("Consistency score", stats.get("consistency_score")),
    )))
    curve = [point for point in _list(pnl.get("equity_curve")) if isinstance(point, dict)]
    last = Text(f"{len(curve)} points; the last, {_shown(curve[-1].get('date'))}: "
                f"{_shown(curve[-1].get('portfolio_value'))}") if curve else None
    _say(_pairs(f"P&L, {period.value}", (
        ("Return (%)", pnl.get("return_pct")), ("Sharpe ratio", pnl.get("sharpe")),
        ("Max drawdown", pnl.get("max_drawdown")), ("Trades", pnl.get("trades_count")),
        ("Win rate", pnl.get("win_rate")), ("Equity curve", last),
    )))
    positions = [row for row in _list(portfolio.get("positions")) if isinstance(row, dict)]
    _say(f"Open positions: {len(positions)} (cash {escape(_shown(portfolio.get('cash')))}, "
         f"{escape(_shown(portfolio.get('cash_pct')))}% of the book)")
    if positions:
        table = _table("Ticker", "Direction", "Entry", "Current", "Size (%)", "Value", "Unrealized P&L",
                       "Unrealized (%)", "Opened")
        for row in positions:
            table.add_row(*_cells(row.get("ticker"), row.get("direction"), row.get("entry_price"),
                                  row.get("current_price"), row.get("size_pct"), row.get("current_value"),
                                  row.get("unrealized_pnl"), row.get("unrealized_pnl_pct"), row.get("opened_at")))
        _say(table)
    _say(f"[dim]{_PUBLIC}[/dim]")


@app.command()
def leaderboard(
    period: Annotated[Period, typer.Option("--period", help="The period ranked")] = _THIRTY_DAYS,
    sort: Annotated[Sort, typer.Option("--sort", help="What the ranking is by")] = _BY_RETURN,
    limit: int = typer.Option(20, "--limit", min=1, max=100, help="How many agents to show"),
):
    """AlphArena's public leaderboard (no key needed). Names are other people's text."""
    with _arena() as client:
        board = client.leaderboard(period.value, sort.value, limit)
    rows = [row for row in _list(board.get("leaderboard")) if isinstance(row, dict)]
    _say(f"[bold]AlphArena leaderboard[/bold] ({period.value}, by {sort.value})")
    if not rows:
        _say("No agent is ranked for this period.")
    else:
        table = _table("Rank", "Agent", "Name", "Return (%)", "Sharpe", "Win rate", "Trades", "Max drawdown (%)")
        for row in rows:
            stats = _dict(row.get("stats"))
            table.add_row(*_cells(row.get("rank"), row.get("agent_id"), row.get("name"),
                                  stats.get("total_return_pct"), stats.get("sharpe_ratio"), stats.get("win_rate"),
                                  stats.get("total_trades"), stats.get("max_drawdown_pct")))
        _say(table)
    _say(f"[dim]{_PUBLIC}[/dim]")


@app.command()
def trades(status: Annotated[Status, typer.Option("--status", help="Which of your trades")] = _OPEN):
    """Your AlphArena paper trades, newest first (needs ALPHARENA_API_KEY)."""
    with _arena() as client:
        answer = client.list_trades(status.value)
        mode = client.mode
    rows = [row for row in _list(answer.get("trades")) if isinstance(row, dict)]
    _say(f"[bold]Your AlphArena trades[/bold] ({escape(mode)} mode, {status.value}): {len(rows)}")
    if rows:
        table = _table("Trade", "Ticker", "Direction", "Status", "Fill", "Exit", "Size (%)", "Opened", "Closed")
        for row in rows:
            table.add_row(*_cells(row.get("trade_id"), row.get("ticker"), row.get("direction"), row.get("status"),
                                  row.get("fill_price"), row.get("exit_price"), row.get("size_pct"),
                                  row.get("opened_at"), row.get("closed_at")))
        _say(table)
    total = _dict(answer.get("pagination")).get("total")
    if _number(total) and total > len(rows):
        _say(f"[dim]The newest {len(rows)} of {_shown(total)} are shown.[/dim]")


@app.command()
def close(
    ticker: str = typer.Argument(..., help="A US stock or ETF ticker, e.g. AAPL"),
    yes: bool = typer.Option(False, "--yes", help="Close without asking, e.g. with no terminal"),
):
    """Close every open AlphArena paper trade in TICKER, once you confirm (needs ALPHARENA_API_KEY)."""
    reason = ticker_refusal(ticker)
    if reason:
        _fail(reason)
    with _arena() as client:
        plan = close_plan(ticker, client.list_trades("open")["trades"])
        _show_plan("AlphArena", plan, client.mode)
        if plan.refused or plan.empty:
            return
        ledger = ArenaLedger()
        if not _ledger_ready(ledger):
            raise typer.Exit(code=1)
        if not yes:
            if not _at_terminal():
                _fail("No terminal to confirm at; pass --yes to close without asking.")
            if not _confirmed(CONFIRM_CLOSE):
                _say("Nothing was closed.")
                return
        results = send_plan(plan, client, ledger)
    _show_results(results, ledger)
    if any(result.get("error") for result in results):
        raise typer.Exit(code=1)


# --- showing AlphArena's answers --------------------------------------------------------------


def _say(markup) -> None:
    """Print the CLI's own markup, with AlphArena's values escaped into it; emoji codes in those
    values (":warning:") stay text too.

    A table's values are never split or cut short. With no terminal (a pipe, a log, Claude
    Code's Bash tool) rich would fit it to 80 columns, so it is printed at its own width; at a
    terminal it is narrowed only as far as its values allow, else shown row by row (``fit``).
    """
    if isinstance(markup, Table):
        if not console.is_terminal:
            width = console.measure(markup, options=console.options.update_width(10_000)).maximum
            Console(file=console.file, width=max(width, console.width), color_system=None,
                    emoji=False).print(markup)
            return
        markup = fit(markup, console, console.options)
    console.print(markup, emoji=False)


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _shown(value, limit: int = 40) -> str:
    """A value from AlphArena's answer as plain text: numbers as reported, "-" when there is none."""
    if _number(value):
        return format(value, ",")
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, str) and value.strip():
        return plain_text(value, limit)
    if isinstance(value, list):
        return plain_text(", ".join(item for item in value if isinstance(item, str)), limit) or "-"
    return "-"


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value) -> list:
    return value if isinstance(value, list) else []


def _table(*columns: str) -> Table:
    # A value too long for even a line of its own (``fit``) folds rather than ending in "…".
    return Table(*(Column(column, overflow="fold") for column in columns), header_style="bold magenta",
                 box=box.SIMPLE_HEAD)


def _cells(*values) -> list[Text]:
    """Table cells as plain text: neither markup nor emoji codes in AlphArena's text are read."""
    return [Text(_shown(value)) for value in values]


def _pairs(title: str, pairs) -> Table:
    """Label and value rows; a value already made into ``Text`` is shown as it is."""
    table = Table(Column(overflow="fold"), Column(overflow="fold"), show_header=False, box=None, title=title,
                  title_justify="left", title_style="bold", padding=(0, 2))
    for label, value in pairs:
        table.add_row(Text(label), value if isinstance(value, Text) else Text(_shown(value)))
    return table
