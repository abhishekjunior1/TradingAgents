import os
import sys
from enum import StrEnum
from typing import Annotated

import typer
from rich.markup import escape

from cli.arena import app as arena_app
from cli.display import console
from cli.models import AssetType
from cli.prompts import (
    DEFAULT_ANALYSTS,
    filter_analysts_for_asset_type,
    parse_analysis_date,
    parse_analysts,
    parse_horizon,
    parse_vendors,
)
from cli.run import run_analysis
from cli.selections import run_horizon
from tradingagents.backtest import iter_grid, run_backtest, summarize
from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.symbols import resolve_symbol
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.llm_clients.claude_code_client import ClaudeCodeError
from tradingagents.llm_clients.factory import tier_provider
from tradingagents.llm_clients.openai_client import SIGNED_IN_PROVIDERS
from tradingagents.markets.exchanges import get_exchange
from tradingagents.markets.watchlists import watchlist_symbols
from tradingagents.memory.settlement import check_benchmark
from tradingagents.portfolio import load_portfolio

# prompt_toolkit's win32 output module is importable only on Windows (it asserts
# the platform at import time), so gate on the platform rather than catching the
# failure — that way a genuinely broken prompt_toolkit on Windows still surfaces
# instead of silently disabling the handler below. Off Windows this stays an
# empty tuple, which `except` accepts and never matches (#1138).
if sys.platform == "win32":  # pragma: no cover - platform dependent
    from prompt_toolkit.output.win32 import NoConsoleScreenBufferError

    _NO_CONSOLE_ERRORS: tuple[type[BaseException], ...] = (NoConsoleScreenBufferError,)
else:
    _NO_CONSOLE_ERRORS = ()

app = typer.Typer(
    name="TradingAgents",
    help="TradingAgents CLI: Multi-Agents LLM Financial Trading Framework",
    add_completion=True,  # Enable shell completion
)


@app.callback(invoke_without_command=True)
def analyze(
    ctx: typer.Context,
    checkpoint: bool | None = typer.Option(
        None,
        "--checkpoint/--no-checkpoint",
        help="Enable/disable checkpoint-resume (save state after each node so a "
        "crashed run can resume). Omit to honor TRADINGAGENTS_CHECKPOINT_ENABLED.",
    ),
    clear_checkpoints: bool = typer.Option(
        False,
        "--clear-checkpoints",
        help="Delete all saved checkpoints before running (force fresh start).",
    ),
    portfolio: str = typer.Option(
        None,
        "--portfolio",
        help="JSON file with current holdings and cash, so the trader, risk and "
        "portfolio agents size against your actual position.",
    ),
    ticker: str = typer.Option(None, "--ticker", help="Ticker to analyze, e.g. NVDA or 0700.HK; skips the prompt"),
    date: str = typer.Option(None, "--date", help="Analysis date, YYYY-MM-DD; skips the prompt"),
    analysts: str = typer.Option(
        None, "--analysts", help="Comma-separated analysts, e.g. market,news; skips the prompt"
    ),
    save: bool | None = typer.Option(
        None, "--save/--no-save", help="Save the report under results_dir without asking"
    ),
    show: bool | None = typer.Option(
        None, "--show/--no-show", help="Show the full report at the end without asking"
    ),
    html: bool | None = typer.Option(
        None, "--html/--no-html",
        help="Save the report as one HTML page too, complete_report.html, without asking (default: yes)",
    ),
    exchange: str = typer.Option(
        None, "--exchange",
        help="nse or bse: a bare ticker (RELIANCE) is read as that exchange's listing (RELIANCE.NS). "
        "Omit to honor TRADINGAGENTS_DEFAULT_EXCHANGE.",
    ),
    benchmark: str = typer.Option(
        None, "--benchmark",
        help="Index or symbol this run's swing decision is measured against, e.g. NIFTY IT, BANKNIFTY or "
        "QQQ; checked before the run (default: NIFTY 50 or SENSEX for an NSE/BSE listing, else the "
        "benchmark_map index for its suffix, SPY for a US listing)",
    ),
    vendor: Annotated[list[str] | None, typer.Option(
        "--vendor",
        help="Data vendor for a category for this run, as category=vendor, e.g. "
        "core_stock_apis=alpha_vantage; repeatable",
    )] = None,
    horizon: str = typer.Option(
        None, "--horizon",
        help="swing (a position rating) or intraday (a paper plan for one session, with the Day Trader "
        "analyst; the date defaults to today). Omit to honor TRADINGAGENTS_DECISION_HORIZON.",
    ),
    arena: bool | None = typer.Option(
        None, "--arena/--no-arena",
        help="Offer the decision to AlphArena as a paper trade with simulated money, after the run and "
        "once you confirm: swing decisions on US stocks and ETFs dated today in New York only "
        "(docs/arena.md). Omit to honor TRADINGAGENTS_ARENA.",
    ),
    arena_yes: bool = typer.Option(
        False, "--arena-yes",
        help="Send that AlphArena paper trade without asking, e.g. with no terminal",
    ),
):
    """Run an analysis. This is what a bare `tradingagents` does.

    Flags answer their questions; with provider, models, depth and language also
    set through TRADINGAGENTS_* variables, the run asks nothing.
    """
    if ctx.invoked_subcommand is not None:
        return
    try:
        listing = get_exchange(exchange or DEFAULT_CONFIG.get("default_exchange"))
        vendors = parse_vendors(vendor)
        horizon = parse_horizon(horizon)
    except ValueError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=1) from None
    # The graph refuses such a benchmark too, but only after the setup questions have been
    # answered. An intraday plan has no benchmark, so its run checks none.
    chosen = benchmark or DEFAULT_CONFIG.get("benchmark_ticker")
    closes = {}     # what the check fetches, for the graph's own checks to be answered from
    if run_horizon({"horizon": horizon}) == "intraday":
        if benchmark:
            console.print("--benchmark applies to swing decisions; this intraday run does not use it.")
    elif chosen:
        try:
            run_date = parse_analysis_date(date) if date else None
        except ValueError:
            run_date = None     # the date step refuses it, with its reason
        try:
            # At --date's day: a past-dated run needs prices there. The closes are fetched through
            # today, so they answer the graph's check of the last 30 days as well.
            check_benchmark(chosen, DEFAULT_CONFIG, as_of=run_date, seen=closes)
        except (ValueError, VendorUnavailableError) as exc:
            source = "--benchmark" if benchmark else "TRADINGAGENTS_BENCHMARK_TICKER"
            console.print(f"[red]{escape(f'{source} {chosen}: {exc}')}[/red]")
            raise typer.Exit(code=1) from None
    if clear_checkpoints:
        from tradingagents.graph.checkpointer import clear_all_checkpoints
        n = clear_all_checkpoints(DEFAULT_CONFIG["data_cache_dir"])
        console.print(f"[yellow]Cleared {n} checkpoint(s).[/yellow]")
    portfolio_context = None
    if portfolio:
        try:
            portfolio_context = load_portfolio(portfolio, listing.code if listing else None)
        except ValueError as exc:
            console.print(f"[red]{escape(str(exc))}[/red]")
            raise typer.Exit(code=1) from None

    try:
        flags = {"ticker": ticker, "date": date, "analysts": analysts, "save": save, "show": show, "html": html,
                 "exchange": listing.code if listing else None, "benchmark": benchmark, "vendors": vendors,
                 "horizon": horizon, "arena": arena, "arena_yes": arena_yes}
        # The benchmark's closes, when the check fetched any, so the graph does not fetch them again.
        kept = {"benchmark_closes": closes} if closes else {}
        run_analysis(checkpoint=checkpoint, portfolio=portfolio_context, flags=flags, **kept)
    except _NO_CONSOLE_ERRORS:
        # A terminal with no console buffer cannot host the interactive prompts.
        # Emit one actionable line on stderr instead of a prompt_toolkit
        # traceback; plain text, since rich may not render here either (#1138).
        typer.echo(
            "Error: no Windows console available. The interactive CLI needs a real "
            "console buffer — run it from Windows Terminal, PowerShell, or cmd.exe "
            "rather than a piped or embedded terminal.",
            err=True,
        )
        raise typer.Exit(code=1) from None
    except ClaudeCodeError as exc:
        # Claude Code's message names what to fix (the model, the sign-in, the
        # plan's limit); a traceback through the graph adds nothing to it.
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=1) from None


@app.command()
def backtest(
    tickers: str = typer.Argument(None, help="Comma-separated tickers, e.g. NVDA,AAPL or RELIANCE.NS,TCS.NS"),
    start: str = typer.Option(..., "--start", help="First analysis date, YYYY-MM-DD"),
    end: str = typer.Option(..., "--end", help="Last analysis date, YYYY-MM-DD"),
    every: int = typer.Option(7, "--every", help="Days between analysis dates"),
    analysts: str = typer.Option(
        None, "--analysts", help="Comma-separated analysts to run: market, sentiment, news, fundamentals, day_trader; "
        "omit for all the asset type allows but day_trader"
    ),
    asset_type: str = typer.Option("stock", "--asset-type", help="stock or crypto"),
    portfolio: str = typer.Option(
        None, "--portfolio", help="JSON file with holdings and cash, held constant across the grid"
    ),
    run_id: str = typer.Option(
        None, "--run-id", help="Continue an earlier sweep: its cells are skipped and its log reused"
    ),
    exchange: str = typer.Option(
        None, "--exchange", help="nse or bse: bare tickers are read as that exchange's listings"
    ),
    watchlist: str = typer.Option(
        None, "--watchlist", help="Sweep a named watchlist (see TRADINGAGENTS_WATCHLISTS_FILE) instead of TICKERS"
    ),
):
    """Score past decisions over a grid of tickers and dates."""

    try:
        dates = iter_grid(start, end, every)
        listing = get_exchange(exchange or DEFAULT_CONFIG.get("default_exchange"))
        code = listing.code if listing else None
        book = load_portfolio(portfolio, code) if portfolio else None
        kind = AssetType(asset_type.strip().lower())
        # The analysts are named and checked as for an analysis; without a
        # choice, every analyst the asset type allows runs, but the Day Trader.
        chosen = (parse_analysts(analysts, kind) if analysts
                  else filter_analysts_for_asset_type(list(DEFAULT_ANALYSTS), kind))
        if watchlist:
            # Names only: a typo read as a ticker would be swept, a model run per date.
            names = watchlist_symbols(watchlist, DEFAULT_CONFIG, lambda s: resolve_symbol(s, code), names_only=True)
        else:
            names = [resolve_symbol(t.strip(), code) for t in (tickers or "").split(",") if t.strip()]
    except ValueError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=1) from None

    if not names:
        console.print("[red]No ticker to analyze; pass them comma-separated, e.g. NVDA,AAPL, or --watchlist[/red]")
        raise typer.Exit(code=1)

    # A sweep asks nothing, and the built-in models are OpenAI's: a tier on another provider
    # without its own model would fail in every cell, after the cell's other calls were paid for.
    providers = {tier: tier_provider(DEFAULT_CONFIG, tier) for tier in ("quick", "deep")}
    missing = [tier for tier, provider in providers.items()
               if provider.strip() != "openai"
               and not os.environ.get(f"TRADINGAGENTS_{tier.upper()}_THINK_LLM")]
    for tier in missing:
        console.print(f"[red]The {tier} tier runs on {escape(providers[tier])}, so a backtest needs "
                      f"TRADINGAGENTS_{tier.upper()}_THINK_LLM set to one of its models.[/red]")
    if missing:
        raise typer.Exit(code=1)

    def show_progress(done, total, ticker, date):
        # The ticker is the user's: as markup, "[/X]" would end the sweep before its settlement pass.
        console.print(f"[{done}/{total}] {ticker} {date}", style="dim", markup=False)

    kwargs = {"asset_type": kind.value, "portfolio": book, "run_id": run_id, "progress": show_progress,
              "selected_analysts": [a.value for a in chosen]}

    try:
        result = run_backtest(names, dates, DEFAULT_CONFIG, **kwargs)
    except Exception as exc:  # a missing key or an unknown analyst is a setup error
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=1) from None
    console.print(summarize(result).render())
    console.print(f"\nRan {result.cells_run} cells, skipped {result.skipped}. Log: {escape(str(result.log_path))}")
    console.print(f"Continue or settle this sweep: --run-id {result.run_id}")
    # Reasons are exception text, often with brackets ("[type=missing, ...]"): printed as text.
    for ticker, date, reason in result.failures:
        console.print(f"[yellow]failed:[/yellow] {escape(ticker)} {escape(date)}: {escape(reason)}")
    for ticker, reason in result.settlement_failures:
        console.print(f"[yellow]unsettled:[/yellow] {escape(ticker)}: {escape(reason)}")


@app.command()
def market(
    exchange: str = typer.Option("nse", "--exchange", help="nse or bse"),
    watchlist: str = typer.Option(
        None, "--watchlist", help="A named watchlist (see TRADINGAGENTS_WATCHLISTS_FILE) or comma-separated symbols"
    ),
    date: str = typer.Option(None, "--date", help="As-of date, YYYY-MM-DD (default: today in the exchange's time zone)"),
):
    """Show an Indian exchange's session (IST) and its indices, India VIX, the rupee and crude; no model is called."""
    from cli.market import show_market

    try:
        listing = get_exchange(exchange)
        if listing is None:   # a blank value, such as an unset variable in a script
            raise ValueError(f"--exchange takes nse or bse, got {exchange!r}")
        show_market(listing, watchlist, date)
    except ValueError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=1) from None


# `tradingagents arena status|leaderboard|trades|close`: AlphArena paper trading (cli/arena.py).
app.add_typer(arena_app, name="arena")


# The plans `login` and `logout` take: the providers signed in to, not keyed.
Plan = StrEnum("Plan", {name: name for name in SIGNED_IN_PROVIDERS})


@app.command()
def login(plan: Annotated[Plan, typer.Argument(help="The plan to sign in with")]):
    """Sign in with your ChatGPT Plus or Pro plan, for the chatgpt provider."""
    from tradingagents.llm_clients import chatgpt_auth

    try:
        # One unwrapped line, so the sign-in address can be copied whole.
        email = chatgpt_auth.sign_in(
            say=lambda line: console.print(line, soft_wrap=True, markup=False, highlight=False))
    except chatgpt_auth.ChatGPTAuthError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=1) from None
    console.print(f"[green]Signed in to ChatGPT as {escape(email)}.[/green]")


@app.command()
def logout(plan: Annotated[Plan, typer.Argument(help="The plan to sign out of")]):
    """Sign out of your ChatGPT plan: revoke this machine's access and forget it."""
    from tradingagents.llm_clients import chatgpt_auth

    outcome = chatgpt_auth.sign_out()
    if outcome == "revoked":
        console.print("Signed out of ChatGPT.")
    elif outcome == "forgotten":
        console.print("[yellow]Signed out here, but OpenAI could not be reached to revoke the sign-in; "
                      "it stays valid at OpenAI until it expires.[/yellow]")
    else:
        console.print("Not signed in to ChatGPT.")


if __name__ == "__main__":
    app()
