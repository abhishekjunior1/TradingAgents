"""Web pages and YouTube videos through Agent-Reach: read a page, list a topic's
videos, read a video's captions.

A live page shows today's content, so a run dated in the past is refused one.
Videos are dated by their upload (``youtube read``; search results carry no date):
a past run lists or transcribes none uploaded after its date, a current run none
uploaded after the machine's date (``cutoff``), counting the run's listing's days
(``date_window.window_zone``). Page text, titles,
channel names and captions are third-party text: cleaned, cut, quoted and labelled
as untrusted.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlsplit

from tradingagents.dataflows.date_window import window_zone
from tradingagents.dataflows.errors import VendorError
from tradingagents.dataflows.vendors.agent_reach.client import (
    MAX_URL_CHARS,
    UNTRUSTED_NOTE,
    clean_text,
    count_setting,
    cutoff,
    in_dates,
    local_day,
    one_line,
    published_date,
    quoted,
    run,
)

# Captions in English (manual, then auto-generated) or Hindi, first found first.
CAPTION_LANGS = "en,en-orig,hi"
DEFAULT_VIDEOS = 3

# Hosts that name this machine or a private network (Agent-Reach refuses the
# same, utils/url.py); a URL for one never leaves for Jina.
_LOCAL_HOSTS = frozenset({
    "home.arpa", "instance-data", "internal", "ip6-localhost", "ip6-loopback", "lan",
    "local", "localdomain", "localhost", "metadata.google.internal",
})
_LOCAL_SUFFIXES = (".home.arpa", ".internal", ".lan", ".local", ".localdomain", ".localhost")

# Markdown images, and links whose address holds at most one level of parentheses.
_IMAGE = re.compile(r"!\[[^\[\]\n]*\]\((?:[^()\n]|\([^()\n]*\))*\)")
_LINK = re.compile(r"\[([^\[\]\n]*)\]\((?:[^()\n]|\([^()\n]*\))*\)")

_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
_VIDEO_URL = re.compile(
    r"(?:https?://)?"
    r"(?:(?:www\.|m\.)?youtube\.com/(?:watch\?(?:[^#\s]*&)?v=|shorts/)|youtu\.be/)"
    r"([A-Za-z0-9_-]{11})(?:[?&#/]\S*)?",
    re.IGNORECASE,
)


def check_url(url) -> str:
    """``url`` normalized when it is a public http(s) address to read, else ValueError saying why."""
    candidate = url.strip() if isinstance(url, str) else ""
    if len(candidate) > MAX_URL_CHARS:
        raise ValueError(f"the URL is longer than {MAX_URL_CHARS} characters")
    if not candidate or "\\" in candidate or any(
            c.isspace() or ord(c) < 0x20 or ord(c) == 0x7F for c in candidate):
        raise ValueError("give the page's full http(s) URL, with no spaces or control characters")
    try:
        parts = urlsplit(candidate)
        host = (parts.hostname or "").lower().rstrip(".")
        _ = parts.port   # raises for a malformed or out-of-range port
    except ValueError:
        raise ValueError("the URL is not valid") from None
    if parts.scheme.lower() not in ("http", "https") or not host:
        raise ValueError("only http:// and https:// URLs are read")
    if parts.username is not None or parts.password is not None:
        raise ValueError("URLs with a user name or password are not read")
    if _local(host):
        raise ValueError("addresses on this machine or a private network are not read")
    return parts.geturl()


def _local(host: str) -> bool:
    if "%" in host or host in _LOCAL_HOSTS or host.endswith(_LOCAL_SUFFIXES):
        return True
    address = _ip_literal(host)
    if address is None:
        return "." not in host   # a single-label name is an intranet one
    return not address.is_global


def _ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """``host`` as an IP address, legacy IPv4 spellings (127.1, 0x7f.0.0.1) included."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    try:
        return ipaddress.IPv4Address(socket.inet_aton(host))
    except OSError:
        return None


def video_id(video) -> str | None:
    """The 11-character id in a YouTube video URL (``youtube.com/watch?v=``,
    ``youtu.be/``, ``youtube.com/shorts/``) or given alone, else None."""
    text = video.strip() if isinstance(video, str) else ""
    if not text or len(text) > 500 or any(c.isspace() or ord(c) < 0x20 for c in text):
        return None
    if _VIDEO_ID.fullmatch(text):
        return text
    match = _VIDEO_URL.fullmatch(text)
    return match.group(1) if match else None


def watch_url(vid: str) -> str:
    return f"https://www.youtube.com/watch?v={vid}"


def _clock(seconds: int | None) -> str | None:
    if seconds is None:
        return None
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _page_text(text: str) -> str:
    """Page text with images left out and links shown as their text.

    Jina writes a page as markdown: on a news site the images, ad links and menus
    before the article can take the whole character budget (on an Economic Times
    article on 2026-10-30, 5,717 of the first 6,000 characters were link syntax).
    """
    return clean_text(_LINK.sub(r"\1", _IMAGE.sub("", text)))


def _published(published: date) -> str:
    """A page's date: with a time and zone, in the run's listing's zone and named so; else as the page gives it."""
    if not isinstance(published, datetime):
        return f"{published:%Y-%m-%d} (as the page states)"
    if published.tzinfo is None:
        return f"{published:%Y-%m-%d %H:%M} (as the page states, with no time zone)"
    return f"{published.astimezone(window_zone() or UTC):%Y-%m-%d %H:%M %Z}"


def read_web_page(url: str, as_of_date: str) -> str:
    """The text of the web page at ``url``, read through Agent-Reach (Jina Reader).

    A run dated in the past gets a refusal: a live page shows today's content,
    not what it said on the analysis date. Only a public http(s) URL is read.
    Images are left out and links shown as their text (``_page_text``).
    """
    day, _, past = cutoff(as_of_date)
    if past:
        return (f"read_web_page is not available for a run dated {day}: a live page shows "
                f"today's content, which postdates {day}.")
    try:
        target = check_url(url)
    except ValueError as exc:
        return f"Refused: {exc}."
    found = run("web", "read", [target])["items"]
    if not found:
        return f"No page found at {target} (Agent-Reach web read found nothing there)."
    page = found[0]
    text = _page_text(page["text"] or "")
    limit = count_setting("agent_reach_page_chars", 6000)
    published = published_date(page["published_at"])
    lines = [f"## Web page: {page['title'] or target}",
             f"Source: Agent-Reach web read (Jina Reader), {target}"]
    if published:
        lines.append(f"Published: {_published(published)}")
    lines += [UNTRUSTED_NOTE, ""]
    if not text:
        lines.append("The page returned no text.")
    else:
        lines.append(f"Text (first {limit} of {len(text)} characters):" if len(text) > limit else "Text:")
        lines.append(quoted(clean_text(text, limit)))
    return "\n".join(lines)


def get_video_coverage(
    query: str,
    as_of_date: str,
    limit: int | None = None,
    look_back_days: int | None = None,
) -> str:
    """YouTube videos about ``query`` uploaded in the look-back window ending on the
    run's date: title, channel, upload date, length and link.

    A YouTube search (relevance order, no dates) finds ``limit`` videos (default 3,
    at most ``agent_reach_max_results``), and each is read for its upload date.
    The window is ``look_back_days`` long (``agent_reach_video_lookback_days``,
    default 30). A read that fails leaves out that video and the ones after it,
    counted; when it fails before any video is listed, the call raises.
    """
    day, end_dt, past = cutoff(as_of_date)
    zone = window_zone()
    query = one_line(query, 200)
    if not query:
        return "Refused: give a search query."
    wanted = limit if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0 else DEFAULT_VIDEOS
    wanted = min(wanted, count_setting("agent_reach_max_results", 5))
    if isinstance(look_back_days, bool) or not isinstance(look_back_days, int) or look_back_days < 0:
        look_back_days = count_setting("agent_reach_video_lookback_days", 30)
    start_dt = end_dt - timedelta(days=look_back_days)
    window = f"{start_dt:%Y-%m-%d} to {end_dt:%Y-%m-%d}"

    videos: list[str] = []
    for item in run("youtube", "search", [query], {"limit": wanted})["items"]:
        vid = video_id(item["url"] or "")
        if vid and vid not in videos:
            videos.append(vid)
    videos = videos[:wanted]
    if not videos:
        return f'YouTube search found no videos for "{query}".'

    listed, outside, gone, unchecked = [], 0, 0, 0
    for index, vid in enumerate(videos):
        try:
            found = run("youtube", "read", [watch_url(vid)])["items"]
        except (VendorError, ValueError):
            # A rate limit, an outage or YouTube's bot check fails every later read too.
            if not listed:
                raise
            unchecked = len(videos) - index
            break
        if not found:
            gone += 1
            continue
        info = found[0]
        uploaded = published_date(info["published_at"])
        if not in_dates(uploaded, start_dt, end_dt, past, zone):
            outside += 1
            continue
        listed.append((info, None if uploaded is None else local_day(uploaded, zone), vid))

    notes = []
    if outside:
        notes.append(f"{outside} uploaded outside {window}" + (" or with no upload date" if past else ""))
    if gone:
        notes.append(f"{gone} not found (private or removed)")
    if unchecked:
        notes.append(f"{unchecked} not checked (Agent-Reach youtube read failed)")
    not_listed = f"Not listed: {'; '.join(notes)}." if notes else ""
    if not listed:
        return f'None of the top {len(videos)} YouTube results for "{query}" was uploaded in {window}. {not_listed}'.strip()

    lines = [f'## YouTube videos for "{query}", uploaded {window} (Agent-Reach youtube search, '
             f"top {len(videos)} results checked):", UNTRUSTED_NOTE, ""]
    for info, uploaded, vid in listed:
        details = [f"channel: {info['author'] or 'not given'}",
                   f"uploaded {uploaded:%Y-%m-%d}" if uploaded else "upload date not given"]
        length = _clock(info["duration"])
        if length:
            details.append(length)
        lines.append(f"- {info['title'] or 'No title'} ({'; '.join(details)}) {watch_url(vid)}")
    if not_listed:
        lines += ["", not_listed]
    return "\n".join(lines)


def get_video_transcript(video: str, as_of_date: str) -> str:
    """The captions of a YouTube video (English, or else Hindi), from Agent-Reach.

    ``video`` is a youtube.com/watch?v=, youtu.be/ or youtube.com/shorts/ URL or
    an 11-character id. The video's upload date must not be after the run's date in
    a past run, or after the machine's date in a current one (``cutoff``); a run
    dated in the past is also refused a video with no upload date.
    """
    vid = video_id(video)
    if vid is None:
        return ("Refused: give a YouTube video URL (youtube.com/watch?v=..., youtu.be/..., "
                "youtube.com/shorts/...) or an 11-character video id.")
    day, end_dt, past = cutoff(as_of_date)
    url = watch_url(vid)
    found = run("youtube", "read", [url])["items"]
    if not found:
        return f"No YouTube video found at {url}; it may be private or removed."
    info = found[0]
    uploaded = published_date(info["published_at"])
    if uploaded is None and past:
        return (f"Refused: Agent-Reach gave no upload date for {url}, so it cannot be shown "
                f"to be from on or before {day}.")
    if uploaded is not None:
        uploaded = local_day(uploaded, window_zone())   # the day the run's listing counts it on
        if uploaded > end_dt.date():
            if past:   # not when: a past-dated run is told nothing dated after its date
                return f"Refused: {url} was uploaded after {day}."
            return f"Refused: {url} was uploaded on {uploaded:%Y-%m-%d}, after {end_dt:%Y-%m-%d}."

    captions = run("youtube", "subtitle", [url], {"lang": CAPTION_LANGS})["items"]
    text = (captions[0]["text"] or "") if captions else ""
    if not text:
        return f"No English or Hindi captions found for {url}."
    limit = count_setting("agent_reach_transcript_chars", 8000)
    lang = captions[0]["lang"]
    details = [f"channel: {info['author'] or 'not given'}",
               f"uploaded {uploaded:%Y-%m-%d}" if uploaded else "upload date not given"]
    lines = [f"## YouTube transcript: {info['title'] or url}",
             f"Video: {url} ({'; '.join(details)})",
             "Source: Agent-Reach youtube subtitle (yt-dlp)" + (f", captions in {lang}" if lang else ""),
             UNTRUSTED_NOTE, "",
             f"Transcript (first {limit} of {len(text)} characters):" if len(text) > limit else "Transcript:",
             quoted(clean_text(text, limit))]
    return "\n".join(lines)
