"""Intraday bars from Yahoo Finance, served only up to the analysis cut-off.

A run dated in the past sees bars through that session's close; a run dated
today sees the bars that have ended by now, never the one still forming (Yahoo
labels a bar by its start, so it is complete only at start + interval). Bars are
the regular session only, in the exchange's time zone, and not dividend-adjusted.
Yahoo serves 1m bars for about the last 7 days and 5m and 15m bars for about the
last 60: an older date is reported as unavailable, not as a day without trading,
and is never requested.

One cache file per symbol and interval holds the latest download of the whole
window (the #1150 rules, as for daily bars): it serves only the exchange-local
day it was written, and a request whose cut-off is in a session still trading
refetches once the file is older than ``intraday_cache_ttl_seconds``. No bar
ending after the file was fetched is served, so a cached forming bar is never
read as complete.
"""

import logging
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.date_window import is_historical
from tradingagents.dataflows.errors import NoMarketDataError, VendorError
from tradingagents.dataflows.files import replace_file
from tradingagents.dataflows.symbols import (
    crypto_quote_currency,
    is_us_listing,
    normalize_symbol,
    safe_ticker_component,
)
from tradingagents.dataflows.vendors.yahoo.common import raise_for_empty, yf_retry
from tradingagents.dataflows.vendors.yahoo.ohlcv import MAX_OHLCV_STALE_DAYS, load_ohlcv
from tradingagents.markets import india, intraday
from tradingagents.markets.exchanges import (
    Exchange,
    day_status,
    exchange_for_symbol,
    is_trading_day,
    previous_trading_day,
    session_status,
)

logger = logging.getLogger(__name__)

# How far back Yahoo serves each interval, in calendar days counting today
# (yfinance 1.7.0 asks for 8 days of 1m and 60 of 5m or 15m; Yahoo's chart API
# refused older 1m and 5m windows on 2026-10-29). Also the allowlist of
# intervals, which name the cache file.
YAHOO_WINDOW_DAYS: dict[str, int] = {"1m": 7, "5m": 60, "15m": 60}

INTRADAY_CACHE_TTL_SECONDS = 60

# Yahoo lists NSE quotes as real-time and BSE quotes as 15 minutes delayed
# (help.yahoo.com/kb/SLN2310); a delayed feed's newest bar may still be forming.
FEED_DELAY: dict[str, timedelta] = {"BSE": timedelta(minutes=15)}

COLUMNS = ["Datetime", "Open", "High", "Low", "Close", "Volume"]
PRICES = ["Open", "High", "Low", "Close"]
SOURCE = "Yahoo Finance"


class IntradayWindowError(NoMarketDataError):
    """The date is older than Yahoo serves bars of the interval: unavailable, not an absence of trading."""


@dataclass(frozen=True)
class IntradayMeta:
    """Where served bars came from and where they were cut."""

    symbol: str
    interval: str
    timezone: str
    cutoff: datetime          # no bar ending later is served
    fetched_at: datetime      # when the download behind the bars was made
    live: bool                # the bars' latest session may still change (it is trading or not yet final)
    delay: timedelta          # the feed delay allowed for
    sessions: tuple[date, ...]


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _as_date(value) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        raise ValueError(f"as_of_date must be YYYY-MM-DD, got {value!r}") from None


def _today(now: datetime, exchange: Exchange | None) -> date:
    """The exchange's date at ``now``.

    For a listing whose zone is not known before its bars arrive, the latest
    date anywhere (UTC+14): the window check may then refuse a date a day early,
    but never admits one Yahoo's window does not reach.
    """
    if exchange is not None:
        return now.astimezone(exchange.tz).date()
    return (now.astimezone(UTC) + timedelta(hours=14)).date()


def _session_end(as_of: date, exchange: Exchange | None, zone) -> datetime:
    """The latest moment a bar of a run dated ``as_of`` may end: that day's close where the exchange is known."""
    if exchange is not None and is_trading_day(exchange, as_of):
        return datetime.combine(as_of, exchange.hours_on(as_of)[2], tzinfo=zone)
    return datetime.combine(as_of + timedelta(days=1), time(0), tzinfo=zone)


def _settled_at(as_of: date, now: datetime, exchange: Exchange | None, zone) -> datetime:
    """When the latest session the request reaches is final, so no bar it may serve can still change.

    NSE/BSE: the close-final time of the latest trading day on or before
    ``as_of`` whose session has opened by ``now``. Elsewhere, with no calendar:
    the end of ``as_of``'s local day.
    """
    if exchange is None:
        return datetime.combine(as_of + timedelta(days=1), time(0), tzinfo=zone)
    local = now.astimezone(zone)
    day = min(as_of, local.date())
    if not is_trading_day(exchange, day) or (day == local.date() and local.time() < exchange.hours_on(day)[1]):
        day = previous_trading_day(exchange, day)
    return datetime.combine(day, exchange.close_final, tzinfo=zone)


def _cache_is_fresh(fetched_at: datetime, now: datetime, settled_at: datetime, ttl: float, zone) -> bool:
    """Whether a download made at ``fetched_at`` can serve a request at ``now``.

    It serves only the exchange-local day it was written. Made after the
    request's latest session was final, it holds all that request may see;
    otherwise that session is still trading and the TTL governs (#1150).
    """
    if fetched_at.astimezone(zone).date() != now.astimezone(zone).date():
        return False
    return fetched_at >= settled_at or (now - fetched_at).total_seconds() <= ttl


def _zone_name(zone) -> str:
    return getattr(zone, "key", None) or str(zone)


def _priced(frame: pd.DataFrame) -> pd.DataFrame:
    """The rows with a time and four prices. Yahoo sometimes prints a price of 0, which is no price at all."""
    frame = frame.dropna(subset=["Datetime", *PRICES])
    return frame[(frame[PRICES] > 0).all(axis=1)]


def _normalized(downloaded: pd.DataFrame, exchange: Exchange | None) -> tuple[pd.DataFrame, str]:
    """yfinance's frame as Datetime (exchange time) and OHLCV columns, rows without a price dropped."""
    frame = downloaded.reset_index()
    if "Datetime" not in frame.columns:
        for candidate in ("Date", "index", "datetime", "date"):
            if candidate in frame.columns:
                frame = frame.rename(columns={candidate: "Datetime"})
                break
    if "Datetime" not in frame.columns or not {"Open", "High", "Low", "Close"} <= set(frame.columns):
        return pd.DataFrame(columns=COLUMNS), _zone_name(exchange.tz if exchange else UTC)
    stamps = pd.to_datetime(frame["Datetime"], errors="coerce")
    if stamps.dt.tz is None:
        stamps = stamps.dt.tz_localize(exchange.tz if exchange is not None else UTC)
    zone = exchange.tz if exchange is not None else stamps.dt.tz
    frame["Datetime"] = stamps.dt.tz_convert(zone)
    if "Volume" not in frame.columns:
        frame["Volume"] = 0.0
    frame = frame[COLUMNS].copy()
    for column in COLUMNS[1:]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return _priced(frame).reset_index(drop=True), _zone_name(zone)


def _write(frame: pd.DataFrame, zone_name: str, fetched_at: datetime, temp: str) -> None:
    """The cache file: the bars with their zone, stamped with the moment of the download."""
    out = frame.copy()
    out.insert(1, "Timezone", zone_name)
    out.to_csv(temp, index=False, encoding="utf-8")
    stamp = fetched_at.timestamp()
    os.utime(temp, (stamp, stamp))   # the rename keeps it, so the file's mtime is the fetch time


def _read_cache(path: str) -> tuple[pd.DataFrame, str, datetime] | None:
    """(bars, zone name, fetch time) from the cache file, or None for a missing or unusable one."""
    if not os.path.exists(path):
        return None
    try:
        cached = pd.read_csv(path, on_bad_lines="skip", encoding="utf-8")
    except (OSError, ValueError) as exc:
        logger.warning("Ignoring the unreadable intraday cache %s: %s", os.path.basename(path), exc)
        return None
    if cached.empty or not {*COLUMNS, "Timezone"} <= set(cached.columns) or cached["Timezone"].isna().all():
        return None
    zone_name = str(cached["Timezone"].dropna().iloc[0])
    try:
        zone = ZoneInfo(zone_name)
    except (KeyError, ValueError):
        return None
    cached["Datetime"] = pd.to_datetime(cached["Datetime"], utc=True, errors="coerce").dt.tz_convert(zone)
    for column in COLUMNS[1:]:
        cached[column] = pd.to_numeric(cached[column], errors="coerce")
    frame = _priced(cached[COLUMNS]).reset_index(drop=True)
    if frame.empty:
        return None
    return frame, zone_name, datetime.fromtimestamp(os.path.getmtime(path), UTC)


def _download(symbol: str, canonical: str, interval: str, today: date, now: datetime, path: str,
              exchange: Exchange | None) -> tuple[pd.DataFrame, str, datetime]:
    """The whole window Yahoo serves for ``interval``, cached unless empty."""
    start = today - timedelta(days=YAHOO_WINDOW_DAYS[interval] - 1)
    end = today + timedelta(days=1)    # yfinance's end is exclusive
    fetched_at = now
    downloaded = yf_retry(lambda: yf.Ticker(canonical).history(
        start=start.isoformat(),
        end=end.isoformat(),
        interval=interval,
        prepost=False,
        auto_adjust=False,
        actions=False,
    ))
    if downloaded is None:
        raise_for_empty(symbol, canonical, f"{interval} bars")
    frame, zone_name = _normalized(downloaded, exchange)
    # Only real bars are cached: an empty download would otherwise be served all day.
    if frame.empty:
        raise_for_empty(symbol, canonical, f"{interval} bars")
    replace_file(path, lambda temp: _write(frame, zone_name, fetched_at, temp))
    return frame, zone_name, fetched_at


def load_intraday_with_meta(symbol: str, as_of_date, interval: str = "5m", sessions: int = 5,
                            now: datetime | None = None) -> tuple[pd.DataFrame, IntradayMeta]:
    """``load_intraday``'s bars and where they came from and were cut (``IntradayMeta``)."""
    if interval not in YAHOO_WINDOW_DAYS:
        raise ValueError(f"interval must be one of {', '.join(YAHOO_WINDOW_DAYS)}, got {interval!r}")
    try:
        sessions = int(sessions)
    except (TypeError, ValueError):
        raise ValueError(f"sessions must be a whole number, got {sessions!r}") from None
    if sessions < 1:
        raise ValueError(f"sessions must be at least 1, got {sessions}")
    as_of = _as_date(as_of_date)
    canonical = normalize_symbol(symbol)
    safe_symbol = safe_ticker_component(canonical)
    exchange = exchange_for_symbol(canonical)
    now = _aware(now or _now())

    today = _today(now, exchange)
    days = YAHOO_WINDOW_DAYS[interval]
    if (today - as_of).days >= days:
        raise IntradayWindowError(
            symbol, canonical,
            f"Yahoo serves {interval} bars for about the last {days} days; {as_of} is outside that window "
            "(unavailable, not an absence of trading)",
        )

    config = get_config()
    cache_dir = os.path.join(config["data_cache_dir"], "intraday")
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, f"{safe_symbol}-YFin-{interval}.csv")
    ttl = float(config.get("intraday_cache_ttl_seconds", INTRADAY_CACHE_TTL_SECONDS))

    cached = _read_cache(path)
    if cached is not None:
        zone = exchange.tz if exchange is not None else ZoneInfo(cached[1])
        if not _cache_is_fresh(cached[2], now, _settled_at(as_of, now, exchange, zone), ttl, zone):
            cached = None
    frame, zone_name, fetched_at = cached or _download(symbol, canonical, interval, today, now, path, exchange)
    zone = exchange.tz if exchange is not None else ZoneInfo(zone_name)
    frame = frame.assign(Datetime=frame["Datetime"].dt.tz_convert(zone), Volume=frame["Volume"].fillna(0.0))
    frame = frame.sort_values("Datetime").drop_duplicates("Datetime", keep="last").reset_index(drop=True)
    if exchange is not None:
        frame = frame[intraday.in_regular_session(frame, exchange)].reset_index(drop=True)

    # The look-ahead rule: a bar is served only once it has ended, by now, by the
    # time its download was made (less the feed's delay), and, for a past date,
    # by that session's close.
    delay = FEED_DELAY.get(exchange.code, timedelta(0)) if exchange is not None else timedelta(0)
    cutoff = min(now, fetched_at - delay, _session_end(as_of, exchange, zone))
    day_of = frame["Datetime"].dt.date
    served = frame[(day_of <= as_of) & (intraday.bar_ends(frame, interval, exchange) <= cutoff)]
    kept = sorted(set(served["Datetime"].dt.date))[-sessions:]
    served = served[served["Datetime"].dt.date.isin(kept)].reset_index(drop=True)

    if served.empty:
        raise NoMarketDataError(symbol, canonical, f"no {interval} bars on or before {as_of} in Yahoo's window")
    stale_days = (as_of - kept[-1]).days
    if stale_days > MAX_OHLCV_STALE_DAYS:
        raise NoMarketDataError(symbol, canonical, f"latest {interval} bar is from {kept[-1]}, {stale_days} days "
                                f"before the requested {as_of} (stale) — refusing to use it")

    meta = IntradayMeta(
        symbol=canonical, interval=interval, timezone=zone_name, cutoff=cutoff, fetched_at=fetched_at,
        live=now < _settled_at(as_of, now, exchange, zone), delay=delay, sessions=tuple(kept),
    )
    return served, meta


def load_intraday(symbol: str, as_of_date, interval: str = "5m", sessions: int = 5,
                  now: datetime | None = None) -> pd.DataFrame:
    """Yahoo's regular-session bars for the last ``sessions`` sessions up to the analysis cut-off.

    Columns Datetime (exchange time, the bar's start), Open, High, Low, Close,
    Volume; rows without a price dropped, volume-0 rows kept (NSE's first bar
    often has none). The cut-off is ``as_of_date``'s close for a past date and
    ``now`` (default: the clock) for today; a bar is served only if it ended by
    then. ``attrs`` carries the cut-off, fetch time and time zone. Raises
    ``IntradayWindowError`` for a date Yahoo no longer serves, ``NoMarketDataError``
    when there are no bars, ``VendorUnavailableError`` when Yahoo cannot be reached.
    """
    bars, meta = load_intraday_with_meta(symbol, as_of_date, interval, sessions, now)
    bars.attrs.update(symbol=meta.symbol, interval=meta.interval, timezone=meta.timezone,
                      cutoff=meta.cutoff.isoformat(), fetched_at=meta.fetched_at.isoformat(), live=meta.live)
    return bars


def _daily(canonical: str, as_of_date: str) -> tuple[pd.DataFrame | None, str | None]:
    """Daily bars up to the run date for the daily ATR, or why they are missing."""
    try:
        return load_ohlcv(canonical, as_of_date, fill_gaps=False), None
    except VendorError as exc:
        return None, f"Daily bars unavailable, so the daily ATR is too ({exc})."


# Indices Yahoo lists without a caret, as the default benchmark_map names them.
_CARETLESS_INDICES = frozenset({"000001.SS", "399001.SZ", "FTSEMIB.MI"})


def _dividend_adjusted(canonical: str) -> bool:
    """Whether ``_daily``'s rows for ``canonical`` can be dividend-adjusted: not an index's (some NSE
    indices, such as NIFTY_FIN_SERVICE.NS, have no caret), a currency's, a future's or a crypto pair's,
    which pay no dividend, so Yahoo leaves their rows as printed."""
    upper = canonical.upper()
    return not (upper.startswith("^") or upper in india.INDEX_BY_SYMBOL or upper in _CARETLESS_INDICES
                or upper.endswith(("=X", "=F")) or crypto_quote_currency(upper) is not None)


def _sizing_examples(levels: dict, exchange: Exchange | None, config: dict, capital: float | None) -> list[dict]:
    """Paper sizes for a long and a short at the newest price, with stops 1 and 2 x the bars' ATR away."""
    reference, atr = levels.get("reference_price"), levels.get("atr_intraday")
    if not reference or not atr or atr <= 0:
        return []
    capital = float(capital or config.get("intraday_paper_capital", 100_000))
    risk_pct = float(config.get("intraday_risk_pct", 0.5))
    leverage_cap = float(config.get("intraday_leverage_cap", 1.0))
    profile = intraday.intraday_cost_profile(exchange, config)
    venue = exchange.code if exchange is not None else None
    examples = []
    for side, sign in (("Long", -1), ("Short", 1)):
        for multiple in (1, 2):
            stop = reference + sign * multiple * atr
            sized = intraday.intraday_position(capital, risk_pct, reference, stop, leverage_cap=leverage_cap,
                                               profile=profile, venue=venue)
            if sized is not None:
                where = "below" if sign < 0 else "above"
                sized["label"] = f"{side} at the newest close, stop {multiple} x ATR {where}"
                examples.append(sized)
    return examples


def _trading_now(exchange: Exchange | None, as_of: date, now: datetime, zone,
                 hours: tuple[time, time] | None) -> bool:
    """Whether a run dated ``as_of`` is looking at a regular session still trading (a bar may be forming)."""
    local = now.astimezone(zone)
    if as_of < local.date():
        return False
    if exchange is not None:
        return session_status(exchange, now).state == "open"
    return hours is not None and local.weekday() < 5 and hours[0] <= local.time() < hours[1]


def _notes(meta: IntradayMeta, exchange: Exchange | None, as_of: date, levels: dict, now: datetime,
           square_off: time | None, trading: bool, daily_note: str | None, *, daily_adjusted: bool) -> list[str]:
    notes = ["Yahoo's intraday bars come through the unofficial yfinance library: regular session only, labelled "
             "by their start, not dividend-adjusted; the official open and close can differ slightly from the "
             "first and last bar."]
    # load_ohlcv serves the daily rows dividend-adjusted (auto_adjust), which on an ex-date
    # lowers the prices before it; the unadjusted ones are not at hand to use instead. The
    # snapshot labels every figure taken from them, and one of these notes says what that means.
    if daily_adjusted and (levels.get("last_session") or {}).get("source") == "daily bar":
        # Both gaps are then measured against that bar's close, so this note covers them too.
        notes.append("The bars hold no session before the plan's, so the last session was read from Yahoo's "
                     "daily bar, which is dividend-adjusted (the intraday bars are not): on an ex-dividend day its "
                     "prices are the ones that traded less about the dividend, so the pivots from it sit about the "
                     "dividend lower, and a gap measured against its close reads higher than it traded by about "
                     "the dividend yield.")
    elif daily_adjusted and levels.get("official_gap"):
        notes.append("The gap at the opening price is measured against Yahoo's daily close, which is "
                     "dividend-adjusted (the intraday bars are not): on an ex-dividend day that close is the one "
                     "that traded less the dividend, so the gap reads higher than it traded by about the dividend "
                     "yield.")
    zone = exchange.tz if exchange is not None else ZoneInfo(meta.timezone)
    local = now.astimezone(zone)
    # Only a run dated today plans the rest of today's session; a past-dated one plans as of its date's close.
    if (square_off is not None and as_of >= local.date() and levels.get("target_session") == local.date()
            and local.time() >= square_off):
        notes.append(f"Today's square-off time ({square_off:%H:%M} {intraday.tz_label(zone)}) has passed: brokers "
                     "take no new intraday positions today, so a plan for the rest of today's session cannot be "
                     "acted on.")
    if exchange is not None:
        # A special session on other hours (Muhurat trading) is held to the hours the calendar
        # gives it, not the regular session's rules.
        notes.append(f"{exchange.code} hours in force on {as_of:%d %b %Y}: {india.hours_in_force(exchange, as_of)}.")
        notes.append("Yahoo carries no pre-open trades, so the first bar can show little or no volume. Intraday "
                     "(MIS) positions are squared off the same day; short selling is allowed intraday only.")
        # Today's state for a run dated today; a past run is told only about its own day.
        if as_of >= now.astimezone(zone).date():
            notes.append(session_status(exchange, now).describe())
        else:
            notes.append(day_status(exchange, as_of))
    else:
        notes.append(f"No exchange calendar is known for this listing: sessions are the weekdays its bars show, "
                     f"in {meta.timezone}, and Yahoo's feed delay for it is not known here.")
    if meta.delay:
        notes.append(f"Yahoo lists these quotes as {int(meta.delay.total_seconds() // 60)} minutes delayed, so bars "
                     "ending within that long of the download are left out.")
    if trading:
        notes.append("Bars still forming are left out: the newest bar shown is the latest complete one.")
    if not levels.get("target_started") and levels.get("target_session"):
        notes.append("The plan's session has not traded in these bars: the levels come from completed sessions.")
    if daily_note:
        notes.append(daily_note)
    return notes


def _unavailable(canonical: str, interval: str, exc: IntradayWindowError) -> str:
    return (f"# Intraday snapshot for {canonical}\n\n"
            f"Unavailable: {exc.detail}. Do not estimate intraday prices or levels; say that {interval} intraday "
            "data was unavailable for this date and rely on daily data.")


def get_intraday_bars(symbol: str, as_of_date: str, interval: str = "5m", sessions: int = 5,
                      capital: float | None = None, capital_source: str | None = None) -> str:
    """The intraday snapshot for ``symbol`` as of ``as_of_date``: the plan's session, levels, paper sizing, recent bars.

    ``capital`` (a paper notional in the listing's currency) overrides config
    ``intraday_paper_capital``; ``capital_source`` says where it came from, for
    the notes. A date beyond Yahoo's window returns an explicit unavailable
    notice without a request; no bars at all raise the typed errors the router
    turns into its sentinels.
    """
    canonical = normalize_symbol(symbol)
    now = _now()
    try:
        bars, meta = load_intraday_with_meta(symbol, as_of_date, interval, sessions, now)
    except IntradayWindowError as exc:
        return _unavailable(canonical, interval, exc)
    exchange = exchange_for_symbol(canonical)
    zone = exchange.tz if exchange is not None else ZoneInfo(meta.timezone)
    as_of = _as_date(as_of_date)
    config = get_config()

    hours = None
    if exchange is None:
        # Read from five sessions, as plan_session reads them: fewer shown (one, after its
        # close) can hold no completed session. The same download serves them.
        recent = bars if len(meta.sessions) >= 5 else load_intraday_with_meta(symbol, as_of, interval, 5, now)[0]
        completed = recent[recent["Datetime"].dt.date < now.astimezone(zone).date()]
        hours = intraday.infer_session_hours(completed, interval)
    target = intraday.target_session(exchange, as_of, now, tz=zone, hours=hours)
    daily, daily_note = _daily(canonical, as_of.isoformat())
    levels = intraday.intraday_levels(bars, daily, target[0], now, interval=interval, exchange=exchange,
                                      cutoff=meta.cutoff)
    square_off = intraday.square_off_time(exchange, config)
    trading = _trading_now(exchange, as_of, now, zone, hours)
    adjusted = _dividend_adjusted(canonical)
    notes = _notes(meta, exchange, as_of, levels, now, square_off, trading, daily_note, daily_adjusted=adjusted)
    notes.append(f"Paper capital: {capital_source or 'given for this run'}." if capital else
                 "Paper capital: intraday_paper_capital, a configured paper notional, not an account balance.")
    return intraday.format_intraday_snapshot(
        canonical, levels, target, square_off,
        _sizing_examples(levels, exchange, config, capital),
        notes,
        recent_bars=bars, source=SOURCE,
        fetched_at=meta.fetched_at if meta.live else None,
        daily_adjusted=adjusted,    # load_ohlcv's rows are auto_adjust=True
    )


def plan_session(symbol: str, as_of_date, now: datetime | None = None) -> dict:
    """The session an intraday plan made now, in a run dated ``as_of_date``, is for, and what it is read against.

    For the run's state at its start (``intraday_context``): ``session`` (ISO
    date) and ``why``, from ``intraday.target_session``; ``opens`` ("HH:MM", when
    that session opens: the calendar's for NSE/BSE, else the bars' usual first
    bar, None when not known); ``exit_by`` ("HH:MM", the configured square-off
    for NSE/BSE, None for the session close); ``timezone`` and ``tz_label``;
    ``reference_price``, the newest complete 5m bar's close by the run's cut-off,
    with ``reference_through`` (when that bar ended); ``interval``, ``source``,
    and ``note`` saying why a figure is missing;
    ``past_dated``, whether the run's tools serve it as dated in the past
    (``date_window.is_historical``), so its plan is one made at that date's close.
    A past date's figures come from its own session, so nothing later is named.
    Never raises for missing data: without bars the session follows the
    exchange's calendar, or weekdays on the listing's clock, and the price is None.
    """
    canonical = normalize_symbol(symbol)
    exchange = exchange_for_symbol(canonical)
    now = _aware(now or _now())
    as_of = _as_date(as_of_date)
    bars, meta, note = None, None, None
    try:
        bars, meta = load_intraday_with_meta(symbol, as_of, "5m", sessions=5, now=now)
    except IntradayWindowError as exc:
        note = exc.detail
    except VendorError as exc:
        note = f"5m bars unavailable: {exc}"

    if exchange is not None:
        zone = exchange.tz
    elif meta is not None:
        zone = ZoneInfo(meta.timezone)
    else:
        zone = ZoneInfo("America/New_York") if is_us_listing(canonical) else UTC
    hours = None
    if exchange is None and bars is not None:
        hours = intraday.infer_session_hours(bars[bars["Datetime"].dt.date < now.astimezone(zone).date()], "5m")
    session, why = intraday.target_session(exchange, as_of, now, tz=zone, hours=hours)
    opens = exchange.hours_on(session)[1] if exchange is not None else hours[0] if hours else None
    try:
        square_off = intraday.square_off_time(exchange, get_config())
    except ValueError as exc:
        square_off, note = None, "; ".join(filter(None, [note, str(exc)]))

    reference = through = None
    if bars is not None and len(bars):
        reference = float(bars["Close"].iloc[-1])
        through = intraday.bar_ends(bars.tail(1), "5m", exchange).iloc[-1]
    return {
        "symbol": canonical,
        "session": session.isoformat(),
        "why": why,
        "opens": f"{opens:%H:%M}" if opens is not None else None,
        "exit_by": f"{square_off:%H:%M}" if square_off else None,
        "timezone": _zone_name(zone),
        "tz_label": intraday.tz_label(zone),
        "reference_price": reference,
        "reference_through": through.isoformat() if through is not None else None,
        "interval": "5m",
        "source": SOURCE,
        "note": note,
        "past_dated": is_historical(as_of.isoformat(), canonical),
    }
