"""yfinance-based news data fetching functions."""

import contextlib
import re
from datetime import UTC, datetime

import yfinance as yf
from dateutil.relativedelta import relativedelta

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.date_window import coverage_gap, in_window, window_zone
from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.symbols import normalize_symbol
from tradingagents.dataflows.vendors.yahoo.common import yf_retry


def _extract_article_data(article: dict) -> dict:
    """Extract article data from yfinance news format (handles nested 'content' structure).

    Yahoo sends some fields as null rather than leaving them out, so each falls
    back to its default on a null as well as on a missing key (#1458).
    ``related`` is the symbols a search result is tagged with; it is not printed.
    """
    related = [str(t) for t in article.get("relatedTickers") or []]
    content = article.get("content")
    if isinstance(content, dict):
        url_obj = content.get("canonicalUrl") or content.get("clickThroughUrl") or {}
        pub_date_str = content.get("pubDate") or ""
        pub_date = None
        if pub_date_str:
            with contextlib.suppress(ValueError, AttributeError):
                pub_date = datetime.fromisoformat(pub_date_str.replace("Z", "+00:00"))

        return {
            "title": content.get("title") or "No title",
            "summary": content.get("summary") or "",
            "publisher": (content.get("provider") or {}).get("displayName") or "Unknown",
            "link": url_obj.get("url") or "",
            "pub_date": pub_date,
            "related": related,
        }
    # Fallback for flat structure. Parse the epoch publish time so flat
    # articles are date-filterable too (otherwise they bypass the
    # historical window and leak future news, #992/#1007).
    pub_date = None
    ts = article.get("providerPublishTime")
    if ts:
        # Epoch seconds are UTC; parse them as UTC-aware so filtering does
        # not shift with the host timezone (#1126).
        with contextlib.suppress(ValueError, OSError, TypeError):
            pub_date = datetime.fromtimestamp(ts, tz=UTC)
    return {
        "title": article.get("title") or "No title",
        "summary": article.get("summary") or "",
        "publisher": article.get("publisher") or "Unknown",
        "link": article.get("link") or "",
        "pub_date": pub_date,
        "related": related,
    }


def _search_news(canonical: str, limit: int) -> tuple[list, bool]:
    """Yahoo search's articles tagged with ``canonical``, and whether it knows the symbol.

    Search answers when the quote feed is empty, but its articles are a
    relevance-picked sample, and some are tagged loosely; only those tagged with
    the symbol are about it.
    """
    search = yf_retry(lambda: yf.Search(canonical, news_count=limit))
    if search is None:  # Yahoo answered "not found": its search is down
        return [], False
    tagged = [
        a for a in (search.news or [])
        if canonical in (t.upper() for t in a.get("relatedTickers") or [])
    ]
    knows = any((q.get("symbol") or "").upper() == canonical for q in search.quotes or [])
    return tagged, knows or bool(search.news)


def _format_articles(articles: list[dict], heading: str = "###") -> str:
    out = ""
    for data in articles:
        out += f"{heading} {data['title']} (source: {data['publisher']})\n"
        if data["summary"]:
            out += f"{data['summary']}\n"
        if data["link"]:
            out += f"Link: {data['link']}\n"
        out += "\n"
    return out


def get_news_yfinance(
    ticker: str,
    start_date: str,
    end_date: str,
) -> str:
    """News about ``ticker`` published in the window, from Yahoo Finance.

    The symbol's quote feed comes first. When it is empty, Yahoo search serves
    the articles tagged with the symbol; when it has none, or neither answers,
    Yahoo news is unavailable, which is not an absence of news, and a configured
    next vendor is tried. Search results are a sample, so finding none in the
    window never reads as "no news".
    """
    article_limit = get_config()["news_article_limit"]
    # Query Yahoo with the canonical symbol, like every other yfinance path —
    # a raw broker/forex/crypto alias (XAUUSD, BTCUSD) otherwise silently
    # returns no news. Keep the user's ticker in the report header.
    canonical = normalize_symbol(ticker)
    resolved = "" if canonical == ticker else f" (resolved to {canonical})"
    subject = f"news for {ticker}{resolved}"
    feed = yf_retry(lambda: yf.Ticker(canonical).get_news(count=article_limit)) or []
    articles = [_extract_article_data(a) for a in feed]
    if not feed:
        found, answered = _search_news(canonical, article_limit)
        if not answered:
            raise VendorUnavailableError(f"Yahoo Finance returned no news response for {canonical}")
        if not found:
            # With the quote feed empty, no tagged article says nothing about the
            # company's news (Yahoo tags no article with most non-US listings).
            raise VendorUnavailableError(f"Yahoo Finance has no news articles tagged {canonical}")
        articles = [_extract_article_data(a) for a in found]

    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    # Keep only articles within the requested window, in the listing's days (look-ahead safe).
    zone = window_zone(canonical)
    in_range = [a for a in articles if in_window(a["pub_date"], start_dt, end_dt, zone)]
    if in_range:
        return f"## {ticker}{resolved} News, from {start_date} to {end_date}:\n\n{_format_articles(in_range)}"

    if not feed:
        return (f"<Yahoo Finance news unavailable for {start_date}..{end_date}: Yahoo search "
                f"returns only a sample of recent articles about {canonical}, so this is not "
                f"an absence of {subject}>")
    gap = coverage_gap(
        (a["pub_date"] for a in articles),
        start_date, end_date, "Yahoo Finance news", subject,
    )
    return gap or f"No news found for {ticker}{resolved} between {start_date} and {end_date}"


def _term_pattern(term: str) -> re.Pattern:
    """Case-insensitive whole-word match for ``term`` (India, not Indiana)."""
    return re.compile(rf"\b{re.escape(term)}\b", re.IGNORECASE)


def _relevant(data: dict, group: dict) -> bool:
    """Whether an article belongs in ``group``.

    A group with no ``terms``, ``weak_terms`` or ``tickers`` keeps every article.
    Otherwise the title or summary must mention a term, or two ``weak_terms``
    (each alone also names things outside the group, such as another country's
    rupee or another RBI), or Yahoo must tag the article with one of the tickers
    (an entry starting with "." matches any symbol with that suffix).
    """
    terms, weak = group.get("terms") or [], group.get("weak_terms") or []
    tickers = [t.upper() for t in group.get("tickers") or []]
    if not terms and not weak and not tickers:
        return True
    text = f"{data['title']} {data['summary']}"
    if any(_term_pattern(term).search(text) for term in terms):
        return True
    if sum(bool(_term_pattern(term).search(text)) for term in weak) >= 2:
        return True
    suffixes = tuple(t for t in tickers if t.startswith("."))
    return any(r.upper() in tickers or r.upper().endswith(suffixes) for r in data["related"])


def _search_window(queries, fuzzy, limit, start_dt, end_dt, keep=None, seen=None,
                   zone=None) -> tuple[list[dict], int]:
    """Up to ``limit`` in-window articles from Yahoo searches for ``queries``, and
    how many more in-window articles were skipped as titles already kept.

    Titles in ``seen`` are skipped and the ones kept are added to it, so several
    groups searched in turn share no article. The window's days are ``zone``'s.
    """
    seen = set() if seen is None else seen
    articles, repeats = [], 0
    for query in queries:
        found = yf_retry(lambda q=query: yf.Search(
            query=q,
            news_count=limit,
            enable_fuzzy_query=fuzzy,
        ).news)

        for article in found or []:
            # Window first: the limit counts what the run may read, so an
            # out-of-window article must not spend the budget or cut the
            # remaining searches short (#1356). Flat articles are filtered
            # on the same rule, so none can leak future news (#1007).
            data = _extract_article_data(article)
            if not in_window(data["pub_date"], start_dt, end_dt, zone):
                continue
            if keep is not None and not keep(data):
                continue
            if data["title"] in seen:
                repeats += 1
            elif data["title"]:
                seen.add(data["title"])
                articles.append(data)

        if len(articles) >= limit:
            break
    return articles, repeats


def _split(counts: list[int], limit: int) -> list[int]:
    """How many articles each group shows: an even split of ``limit``, the earlier
    groups taking the remainder, and a share one group leaves unused going to the others."""
    shares = [limit // len(counts) + (i < limit % len(counts)) for i in range(len(counts))]
    taken = [min(n, share) for n, share in zip(counts, shares, strict=True)]
    for i, n in enumerate(counts):
        extra = min(limit - sum(taken), n - taken[i])
        taken[i] += max(extra, 0)
    return taken


def _format_groups(groups: list[dict], found: list[tuple[list[dict], int]], limit: int,
                   start_date: str, end_date: str) -> str:
    """The groups' articles under one section each; empty when no group kept any.

    ``found`` holds each group's articles and its count of in-window repeats of
    titles already kept. A group showing nothing says why: the limit left it no
    room, an earlier group's searches already found its stories, or its search kept
    none, which over a window Yahoo's search does not reach is no absence of news.
    """
    taken = _split([len(articles) for articles, _ in found], limit)
    if not any(taken):
        return ""
    out = ""
    for group, (articles, repeats), n in zip(groups, found, taken, strict=True):
        queries = ", ".join(f'"{q}"' for q in group["queries"]) or "none configured"
        out += f"### {group['label']}\n{group.get('about') or 'Stories'} (Yahoo news searches: {queries}).\n\n"
        if n:
            out += _format_articles(articles[:n], heading="####")
            continue
        if articles:
            empty = (f"{len(articles)} {group['label']} stories found in this window; none shown "
                     f"within the article limit of {limit}.")
        elif repeats:
            empty = (f"Yahoo's news search returned no {group['label']} stories in this window "
                     f"other than ones the searches above had already found.")
        else:
            # As for the single list, the merged searches prove no coverage of the window.
            empty = (group.get("empty")
                     or coverage_gap((), start_date, end_date, "Yahoo Finance global news", f"{group['label']} news")
                     or f"Yahoo's news search returned no {group['label']} stories in this window.")
        out += f"{empty}\n\n"
    return out


def get_global_news_yfinance(
    as_of_date: str,
    look_back_days: int | None = None,
    limit: int | None = None,
) -> str:
    """
    Retrieve global/macro economic news using yfinance Search.

    With ``global_news_groups`` in the config (a list of ``{"label", "queries",
    "fuzzy", "terms", "weak_terms", "tickers", "about", "empty"}``), the articles are searched
    and reported per group, the limit split between them; otherwise
    ``global_news_queries`` are searched as one list.

    Args:
        as_of_date: Current date in yyyy-mm-dd format
        look_back_days: Number of days to look back. ``None`` falls back to
            ``global_news_lookback_days`` from the active config.
        limit: Maximum number of articles to return. ``None`` falls back to
            ``global_news_article_limit`` from the active config.

    Returns:
        Formatted string containing global news articles
    """
    config = get_config()
    if look_back_days is None:
        look_back_days = config["global_news_lookback_days"]
    if limit is None:
        limit = config["global_news_article_limit"]
    groups = config.get("global_news_groups")

    curr_dt = datetime.strptime(as_of_date, "%Y-%m-%d")
    start_dt = curr_dt - relativedelta(days=look_back_days)
    start_date = start_dt.strftime("%Y-%m-%d")
    zone = window_zone()   # the run's listing's days

    if groups:
        # Each group is searched to the whole limit, so a share another group
        # leaves unused can be filled; titles are not repeated across groups.
        seen = set()
        found = [
            _search_window(g["queries"], g.get("fuzzy", True), limit, start_dt, curr_dt,
                           lambda d, g=g: _relevant(d, g), seen, zone)
            for g in groups
        ]
        news_str = _format_groups(groups, found, limit, start_date, as_of_date)
    else:
        found, _ = _search_window(config["global_news_queries"], True, limit, start_dt, curr_dt, zone=zone)
        news_str = _format_articles(found[:limit])

    # Nothing fell inside the window -> say so rather than return an
    # empty-bodied report (#993).
    if not news_str:
        # Results merge several fuzzy searches, so their timestamps prove no
        # continuous coverage; judge the window against the present only.
        gap = coverage_gap((), start_date, as_of_date, "Yahoo Finance global news", "market news")
        return gap or f"No global news found between {start_date} and {as_of_date}"

    return f"## Global Market News, from {start_date} to {as_of_date}:\n\n{news_str}"
