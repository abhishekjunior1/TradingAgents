"""``tradingagents market``: an exchange's session, and its indices, the rupee and crude, with no model called."""

from datetime import date

from rich.markdown import Markdown, TableElement
from rich.markup import escape
from rich.table import Table

from cli.display import console
from cli.prompts import parse_analysis_date
from cli.tables import fit
from tradingagents.dataflows.config import run_config
from tradingagents.dataflows.router import route_to_vendor
from tradingagents.dataflows.symbols import resolve_symbol
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.markets.exchanges import Exchange, next_trading_day, session_status
from tradingagents.markets.watchlists import watchlist_symbols


def show_market(exchange: Exchange, watchlist: str | None = None, as_of: str | None = None) -> None:
    """Print the session status, the market context of the exchange's benchmark, and a watchlist."""
    status = session_status(exchange)
    today = status.local_time.date()
    # Refused before anything is printed or requested, on the exchange's clock, whose date is the default.
    as_of = today.isoformat() if as_of is None else parse_analysis_date(as_of, today)
    console.print(f"[bold]{escape(exchange.name)}[/bold]: {escape(status.describe())}")
    if status.next_open != today or status.state == "closed":
        for line in _next_sessions(exchange, status.next_open):
            console.print(escape(line))
    with run_config(DEFAULT_CONFIG):
        _show(route_to_vendor("get_market_context", exchange.benchmark, as_of))
        if watchlist:
            symbols = watchlist_symbols(watchlist, DEFAULT_CONFIG, lambda s: resolve_symbol(s, exchange.code))
            _show(route_to_vendor("get_watchlist_snapshot", symbols, as_of))
    console.print("[dim]Research data only: delayed or end-of-day prices from the configured vendor. "
                  "No orders are placed.[/dim]")


def _next_sessions(exchange: Exchange, day: date) -> list[str]:
    """The session ``day`` holds: a special one by name, with its own hours, then the regular session after it."""
    special = exchange.special_sessions.get(day)
    if special is None:
        return [f"Next regular session: {day:%a %d %b %Y}, {exchange.session_hours(day)}."]
    name, pre_open, _, _ = special
    regular = next_trading_day(exchange, day)
    while regular in exchange.special_sessions:
        regular = next_trading_day(exchange, regular)
    return [f"Next session: {day:%a %d %b %Y}, {name}, {exchange.session_hours(day)} "
            f"(pre-open from {pre_open:%H:%M}).",
            f"Next regular session: {regular:%a %d %b %Y}, {exchange.session_hours(regular)}."]


class _WholeTable(TableElement):
    """A markdown table drawn with every value whole on its line (``cli.tables.fit``), never ending in "…"."""

    def __rich_console__(self, console, options):
        for table in super().__rich_console__(console, options):
            if isinstance(table, Table):
                for column in table.columns:
                    column.overflow = "fold"
                table = fit(table, console, options)
            yield table


class _Markdown(Markdown):
    elements = {**Markdown.elements, "table_open": _WholeTable}


def _show(markdown: str) -> None:
    """A vendor's markdown with every value whole: a row's close date, and a late close's note, are the point.

    With no terminal (a pipe, a log, Claude Code's Bash tool) rich would fit the tables to 80
    columns and cut their cells short, so the markdown is printed as it is; at a terminal it is
    rendered, a table narrowed only as far as its values allow, else shown row by row.
    """
    if console.is_terminal:
        console.print(_Markdown(markdown))
    else:
        console.print(markdown, markup=False, emoji=False, highlight=False, soft_wrap=True)
