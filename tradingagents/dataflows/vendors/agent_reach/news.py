"""Company and global news from Agent-Reach web search (Exa).

Exa's search has no date filter and answers from today's index, and the dates it
gives come from the pages, which can be wrong. A run dated in the past therefore
keeps only items dated inside its window and drops undated ones; a current run also
keeps undated items, marked as such. A past run keeps nothing dated after its date,
and a current run nothing dated after the machine's date (``cutoff``), which in the
night window can be a day after a US run's date. The days are the listing's
(``date_window.window_zone``), and so are the dates shown, but a time with no zone
is counted by ``client.local_day`` and shown on the date it states. A search that
keeps nothing raises NoMarketDataError, so the router tries the next vendor in the
chain (Yahoo by default): with no date filter, finding nothing in a window is not an
absence of news. Titles and snippets are third-party text: cleaned, cut, quoted and
labelled as untrusted.
"""

from __future__ import annotations

import functools
import logging
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.date_window import window_zone
from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.symbols import crypto_base, normalize_symbol
from tradingagents.dataflows.vendors.agent_reach.client import (
    UNTRUSTED_NOTE,
    count_setting,
    cutoff,
    in_dates,
    local_day,
    one_line,
    published_date,
    run,
)
from tradingagents.dataflows.vendors.yahoo.fundamentals import get_company_profile
from tradingagents.dataflows.vendors.yahoo.news import _split
from tradingagents.markets.exchanges import exchange_for_symbol

logger = logging.getLogger(__name__)

SOURCE = "Agent-Reach web search (Exa)"

# The default for ``agent_reach_india_queries``. Exa's semantic search suits
# multi-word topics, unlike Yahoo's keyword search, so India's macro news has its
# own queries rather than ``global_news_queries_india``.
INDIA_QUERIES = [
    "RBI monetary policy repo rate",
    "Nifty Sensex Indian stock market",
    "FII DII flows Indian equities",
    "rupee dollar exchange rate",
    "India inflation CPI",
    "SEBI regulation markets",
    "India Union Budget economy",
    "crude oil prices India",
]


def company_name(symbol: str) -> str | None:
    """Yahoo's name for ``symbol`` to search by, or None when the lookup fails.

    The name the run's identity comes from (``resolve_instrument_identity`` reads
    the same profile; importing it from here would make the data layer depend on
    the agents). An answer is cached for the process; a failure is asked again.
    """
    try:
        return _profile_name(symbol)
    except Exception as exc:  # noqa: BLE001 — fail open to the ticker
        logger.debug("Could not resolve a company name for %s: %s", symbol, exc)
        return None


@functools.lru_cache(maxsize=256)
def _profile_name(symbol: str) -> str | None:
    info = get_company_profile(symbol)
    for key in ("longName", "shortName"):
        name = one_line(info.get(key), 100)
        if name and name.lower() not in {"none", "n/a", "nan", "null"}:
            return name
    return None


def _search_query(canonical: str) -> str:
    """``"<Company name> stock news"``, with "India" for an NSE/BSE listing; the
    ticker (without its exchange suffix) stands in when the name is unknown."""
    exchange = exchange_for_symbol(canonical)
    bare = canonical
    if exchange is not None and canonical.upper().endswith(exchange.suffix):
        bare = canonical[: -len(exchange.suffix)]
    name = company_name(canonical) or bare
    if crypto_base(canonical):
        return f"{name} crypto news"
    return f"{name} stock news" + (" India" if exchange is not None else "")


def _link(url: str | None) -> str | None:
    """``url`` when it is a web address to show, else None."""
    if not url:
        return None
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return None
    return url if parts.scheme in ("http", "https") and host else None


def _keys(item: dict) -> set[str]:
    """What identifies a story across searches: its address and its title."""
    keys = set()
    link = _link(item["url"])
    if link:
        parts = urlsplit(link)
        host = (parts.hostname or "").removeprefix("www.")
        keys.add(f"url:{host}{parts.path.rstrip('/')}?{parts.query}")
    if item["title"]:
        keys.add("title:" + " ".join(item["title"].casefold().split()))
    return keys


def _in_window(items: list[dict], start_dt: datetime, end_dt: datetime, past: bool,
               seen: set[str], zone=None) -> tuple[list[dict], int]:
    """The items dated in the window, ``zone``'s days (and, in a current run, the
    undated ones), each with its day there as ``published`` (a time with no zone,
    the date it states), and how many more repeated a story in ``seen``.

    ``seen`` gains the kept stories' keys, so searches made in turn share no story.
    """
    kept, repeats = [], 0
    for item in items:
        published = published_date(item["published_at"])
        keys = _keys(item)
        if not keys or not in_dates(published, start_dt, end_dt, past, zone):
            continue
        if keys & seen:
            repeats += 1
            continue
        seen |= keys
        if isinstance(published, datetime) and published.tzinfo is None:
            # Counted by local_day (the next day in India for an evening time), but
            # shown on the date the page states, as read_web_page shows it.
            published = published.date()
        kept.append({**item, "published": None if published is None else local_day(published, zone)})
    return kept, repeats


def _search(queries, per_search: int, limit: int, start_dt: datetime, end_dt: datetime,
            past: bool, seen: set[str], zone=None) -> tuple[list[dict], int, list[str]]:
    """Up to ``limit`` stories in the window from web searches for ``queries`` in turn,
    how many more repeated stories already kept, and the queries searched.

    A failed search raises rather than being skipped: a section that shows nothing
    must mean the searches found nothing, not that one of them failed.
    """
    stories, repeats, searched = [], 0, []
    for query in queries:
        query = one_line(query, 200)
        if not query:
            continue
        searched.append(query)
        found = run("web", "search", [query], {"limit": per_search})["items"]
        kept, repeated = _in_window(found, start_dt, end_dt, past, seen, zone)
        stories += kept
        repeats += repeated
        if len(stories) >= limit:
            break
    return stories, repeats, searched


def _kept_none(results: str, start_date: str, end_date: str, past: bool) -> str:
    """Why a search kept nothing, in our own words: the detail of its NoMarketDataError."""
    if past:
        return f"no {results} is dated in {start_date}..{end_date}, and undated ones are not used for a past date"
    return f"no {results} falls in {start_date}..{end_date}"


def _format_items(items: list[dict], snippet_chars: int, heading: str = "###") -> str:
    out = ""
    for item in items:
        link = _link(item["url"])
        source = f"source: {urlsplit(link).hostname.removeprefix('www.')}" if link else "source not given"
        when = item["published"]
        dated = f"({source}, published {when:%Y-%m-%d})" if when else f"({source}) (date not given)"
        out += f"{heading} {item['title'] or 'No title'} {dated}\n"
        snippet = one_line(item["text"], snippet_chars)
        if snippet:
            out += f"> {snippet}\n"
        if link:
            out += f"Link: {link}\n"
        out += "\n"
    return out


def _format_groups(groups: list[dict], found: list[tuple[list[dict], int, list[str]]],
                   limit: int, snippet_chars: int) -> str:
    """The groups' stories under one section each, the limit split as Yahoo's global
    news splits it; empty when no group kept any. A section showing nothing says why."""
    taken = _split([len(stories) for stories, _, _ in found], limit)
    if not any(taken):
        return ""
    out = ""
    for group, (stories, repeats, searched), n in zip(groups, found, taken, strict=True):
        label = group["label"]
        queries = ", ".join(f'"{q}"' for q in searched)
        out += f"### {label}\n{group['about']} (Agent-Reach web searches: {queries or 'none configured'}).\n\n"
        if n:
            out += _format_items(stories[:n], snippet_chars, heading="####")
            continue
        if stories:
            empty = (f"{len(stories)} {label} stories found in this window; none shown "
                     f"within the article limit of {limit}.")
        elif repeats:
            empty = (f"Agent-Reach's web searches returned no {label} stories in this window "
                     f"other than ones the searches above had already found.")
        elif searched:
            empty = (f"Agent-Reach's web searches returned no {label} stories in this window "
                     f"(they have no date filter, so this is not an absence of news).")
        else:
            empty = f"No {label} searches are configured."
        out += f"{empty}\n\n"
    return out


def get_news_agent_reach(ticker: str, start_date: str, end_date: str) -> str:
    """News about ``ticker`` published in the window, from an Agent-Reach web search (Exa).

    One search for the company's name (``"<name> stock news"``, plus "India" for an
    NSE/BSE listing) asks for twice ``agent_reach_max_results`` results, since the
    date rules drop many. Finding none in the window raises NoMarketDataError and a
    failed search VendorUnavailableError, so a configured next vendor is tried either way.
    """
    canonical = normalize_symbol(ticker)
    resolved = "" if canonical == ticker else f" (resolved to {canonical})"
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    _, end_dt, past = cutoff(end_date, canonical)
    query = _search_query(canonical)
    found = run("web", "search", [query], {"limit": 2 * count_setting("agent_reach_max_results", 5)})["items"]
    stories, _ = _in_window(found, start_dt, end_dt, past, set(), window_zone(canonical))
    if not stories:
        raise NoMarketDataError(ticker, canonical, _kept_none(
            f'{SOURCE} result for "{query}"', start_date, end_date, past))
    snippet_chars = count_setting("agent_reach_snippet_chars", 600)
    return (f'## {ticker}{resolved} News, from {start_date} to {end_date} ({SOURCE}: "{query}"):\n'
            f"{UNTRUSTED_NOTE}\n\n{_format_items(stories, snippet_chars)}")


def get_global_news_agent_reach(
    as_of_date: str,
    look_back_days: int | None = None,
    limit: int | None = None,
) -> str:
    """Global and macro news from Agent-Reach web searches (Exa).

    With ``global_news_groups`` in the config (set for an NSE/BSE listing) the
    stories come in two sections, India from ``agent_reach_india_queries`` and
    Global from ``global_news_queries``, the limit split between them and no story
    repeated; otherwise ``global_news_queries`` are searched as one list. Each
    search asks for twice ``agent_reach_max_results`` results, and a section's
    searches stop once it has ``limit`` stories. When no search keeps a story,
    NoMarketDataError lets a configured next vendor try.

    Args:
        as_of_date: The analysis date, yyyy-mm-dd.
        look_back_days: Days to look back; ``None`` uses ``global_news_lookback_days``.
        limit: Most stories to show; ``None`` uses ``global_news_article_limit``.
    """
    config = get_config()
    if look_back_days is None:
        look_back_days = config.get("global_news_lookback_days", 7)
    if limit is None:
        limit = config.get("global_news_article_limit", 10)
    start_dt = datetime.strptime(as_of_date, "%Y-%m-%d") - timedelta(days=look_back_days)
    start_date = start_dt.strftime("%Y-%m-%d")
    _, end_dt, past = cutoff(as_of_date)
    zone = window_zone()   # the run's listing's days
    per_search = 2 * count_setting("agent_reach_max_results", 5)
    snippet_chars = count_setting("agent_reach_snippet_chars", 600)
    global_queries = list(config.get("global_news_queries") or [])

    if config.get("global_news_groups"):
        groups = [
            {"label": "India", "about": "India-related stories",
             "queries": list(config.get("agent_reach_india_queries", INDIA_QUERIES) or [])},
            {"label": "Global", "about": "Global macro stories", "queries": global_queries},
        ]
        # Each group is searched to the whole limit, so a share another group
        # leaves unused can be filled; stories are not repeated across groups.
        seen: set[str] = set()
        found = [_search(g["queries"], per_search, limit, start_dt, end_dt, past, seen, zone) for g in groups]
        news_str = _format_groups(groups, found, limit, snippet_chars)
    else:
        stories, _, _ = _search(global_queries, per_search, limit, start_dt, end_dt, past, set(), zone)
        news_str = _format_items(stories[:limit], snippet_chars)

    if not news_str:
        raise NoMarketDataError("global news", None, _kept_none(f"{SOURCE} result", start_date, as_of_date, past))
    return f"## Global Market News, from {start_date} to {as_of_date} ({SOURCE}):\n{UNTRUSTED_NOTE}\n\n{news_str}"
