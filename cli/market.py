"""``tradingagents market``: an exchange's session, and its indices, the rupee and crude, with no model called."""

from datetime import date

from rich.markdown import Markdown
from rich.markup import escape

from cli.display import console
from tradingagents.dataflows.config import run_config
from tradingagents.dataflows.router import route_to_vendor
from tradingagents.dataflows.symbols import resolve_symbol
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.markets.exchanges import Exchange, local_now, session_status
from tradingagents.markets.watchlists import watchlist_symbols


def show_market(exchange: Exchange, watchlist: str | None = None, as_of: str | None = None) -> None:
    """Print the session status, the market context of the exchange's benchmark, and a watchlist."""
    status = session_status(exchange)
    console.print(f"[bold]{escape(exchange.name)}[/bold]: {escape(status.describe())}")
    if status.next_open != status.local_time.date() or status.state == "closed":
        console.print(f"Next regular session: {status.next_open:%a %d %b %Y}, {exchange.session_hours()}.")
    as_of = as_of or local_now(exchange).date().isoformat()
    date.fromisoformat(as_of)   # a malformed date is refused before any request
    with run_config(DEFAULT_CONFIG):
        console.print(Markdown(route_to_vendor("get_market_context", exchange.benchmark, as_of)))
        if watchlist:
            symbols = watchlist_symbols(watchlist, DEFAULT_CONFIG, lambda s: resolve_symbol(s, exchange.code))
            console.print(Markdown(route_to_vendor("get_watchlist_snapshot", symbols, as_of)))
    console.print("[dim]Research data only: delayed or end-of-day prices from the configured vendor. "
                  "No orders are placed.[/dim]")
