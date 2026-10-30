import logging
import os
from datetime import datetime

import pandas as pd
import yfinance as yf

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.files import replace_file
from tradingagents.dataflows.symbols import listing_zone, normalize_symbol, safe_ticker_component
from tradingagents.dataflows.vendors.yahoo.common import raise_for_empty, yf_retry
from tradingagents.markets.exchanges import exchange_for_symbol, is_trading_day

logger = logging.getLogger(__name__)

# A vendor's latest OHLCV row this many calendar days before the requested date
# is treated as stale. Generous enough to span long holiday weekends, tight
# enough to catch the year-old frames yfinance occasionally returns (#1021).
MAX_OHLCV_STALE_DAYS = 10

# How long a same-day cache that does not yet reach the requested day may be
# reused before it is refetched (#1150). Short enough that an intraday run picks
# up today's close soon after it publishes, long enough that a day with no bar
# at all (weekend, holiday) cannot trigger a download on every call.
OHLCV_CACHE_TTL_SECONDS = 900

# Calendar days of history a download holds before the requested date: the
# indicators need about a year of sessions (a 200-day SMA, a 250-session relative
# return), so a date the 5-year window does not reach that far behind is
# downloaded from earlier.
HISTORY_BEFORE_AS_OF_DAYS = 400


def _ensure_date_column(data: pd.DataFrame) -> pd.DataFrame:
    """Normalize the date column to ``Date``.

    Some yfinance builds leave the index unnamed (so ``reset_index()`` yields
    ``index``) or use ``Datetime`` for intraday data. Rename the first
    date-like column so indicators don't silently drop when it isn't ``Date``.
    """
    if "Date" in data.columns:
        return data
    for candidate in ("index", "Datetime", "date"):
        if candidate in data.columns:
            return data.rename(columns={candidate: "Date"})
    return data


def _local_midnight(value) -> pd.Timestamp:
    """A single timestamp as its naive, midnight-normalized local date (or NaT)."""
    if pd.isna(value):
        return pd.NaT
    try:
        ts = pd.Timestamp(value)
    except (ValueError, TypeError):
        return pd.NaT
    if ts.tzinfo is not None:
        ts = ts.tz_localize(None)  # drop tz, keep the local wall-clock date
    return ts.normalize()


def _normalize_dates(dates) -> pd.Series:
    """Parse to naive, midnight-normalized dates so tz-aware or intraday
    timestamps compare correctly against the naive ``as_of_date`` cutoff (#1201).

    Normalized per element: 5 years of yfinance bars span daylight-saving
    changes (and cache CSVs round-trip the offsets as strings), so the series can
    carry mixed UTC offsets that ``pd.to_datetime`` cannot unify without
    ``utc=True`` — which would shift non-US (positive-offset) markets to the
    previous day. Keeping each bar's own local date avoids both.
    """
    return pd.to_datetime(pd.Series(dates).map(_local_midnight))


def _clean_dataframe(data: pd.DataFrame) -> pd.DataFrame:
    """Normalize a stock DataFrame for stockstats: parse/normalize dates and
    coerce prices to numeric (NaN where invalid). Dropping incomplete rows and
    filling gaps is left to ``_fill_price_gaps`` so the caller can first inspect
    the latest in-range bar (#1201)."""
    data = _ensure_date_column(data)
    data["Date"] = _normalize_dates(data["Date"])
    data = data.dropna(subset=["Date"]).copy()

    price_cols = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in data.columns]
    data[price_cols] = data[price_cols].apply(pd.to_numeric, errors="coerce")
    return data


def _fill_price_gaps(data: pd.DataFrame) -> pd.DataFrame:
    """Drop rows with no close and forward/back-fill remaining price gaps so
    indicators compute on a continuous series."""
    price_cols = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in data.columns]
    # copy() so a filtered (sliced) input is written to safely, not via a view.
    data = data.dropna(subset=["Close"]).copy()
    data[price_cols] = data[price_cols].ffill().bfill()
    return data


def _closed_day_filler(data: pd.DataFrame, canonical: str) -> pd.Series:
    """Which rows are Yahoo's filler for a day the listing's exchange was closed.

    Yahoo gives some NSE listings a row on an exchange holiday (RELIANCE.NS and
    M&M.NS on 15 Jan 2026): one price as open, high, low and close, and no
    volume. Read as a session it adds a zero-range bar to ATR and moves every
    window by a row. Only such a row on a day the calendar here has closed is
    filler: a row with trading is kept, holiday or not (NSE's budget session of
    Saturday 1 Feb 2025 is outside the calendar here), and so is a flat bar on a
    trading day, an illiquid listing's day without trades. Dates are read from
    ``Date`` or, without that column, the index.
    """
    filler = pd.Series(False, index=data.index)
    exchange = exchange_for_symbol(canonical)
    if exchange is None or not {"Open", "High", "Low", "Close", "Volume"} <= set(data.columns):
        return filler
    # yfinance's dividend adjustment scales open, high and low by Adj Close / Close but takes
    # Adj Close itself as the close, which can leave a flat row's prices an ulp apart: they
    # are compared within a tolerance far below any price tick.
    close = data["Close"]
    flat = data[["Open", "High", "Low"]].sub(close, axis=0).abs().le(close.abs() * 1e-9, axis=0).all(axis=1)
    dates = pd.DatetimeIndex(data["Date"] if "Date" in data.columns else data.index)
    for position in (flat & (data["Volume"].fillna(0) == 0)).to_numpy().nonzero()[0]:
        filler.iloc[position] = not is_trading_day(exchange, dates[position].date())
    return filler


def _coerce_ohlcv_dates(data: pd.DataFrame) -> pd.Series:
    """Return parsed dates from an OHLCV frame, whether Date is a column or the index."""
    if "Date" in data.columns:
        return pd.to_datetime(data["Date"], errors="coerce").dropna()
    # yfinance keeps the dates in the index (a DatetimeIndex, sometimes unnamed).
    if isinstance(data.index, pd.DatetimeIndex):
        return pd.Series(pd.to_datetime(data.index, errors="coerce")).dropna()
    # Fallback: expose the index and look for any date-like column.
    df = data.reset_index()
    for col in ("Date", "Datetime", "date", "index"):
        if col in df.columns:
            parsed = pd.to_datetime(df[col], errors="coerce").dropna()
            if not parsed.empty:
                return parsed
    return pd.Series(dtype="datetime64[ns]")


def _assert_ohlcv_not_stale(
    data: pd.DataFrame,
    as_of_date: str,
    symbol: str,
    canonical: str | None = None,
    *,
    max_stale_days: int = MAX_OHLCV_STALE_DAYS,
) -> None:
    """Reject OHLCV whose latest row is far older than as_of_date.

    Raises NoMarketDataError (with a stale-specific detail) so the router treats
    it like any other "no usable data from this vendor" — try the next vendor,
    then emit one clear unavailable signal. Empty frames are left to the
    caller's existing no-data handling; this guards only the dangerous case of
    present-but-stale rows (a vendor returning a year-old frame that would
    otherwise feed wrong prices to the agent, #1021).
    """
    if data is None or data.empty:
        return
    requested = pd.to_datetime(as_of_date, errors="coerce")
    if pd.isna(requested):
        return
    requested = requested.normalize()
    dates = _coerce_ohlcv_dates(data)
    if dates.empty:
        return
    latest = dates.max().normalize()
    stale_days = (requested - latest).days
    if stale_days > max_stale_days:
        raise NoMarketDataError(
            symbol,
            canonical,
            f"latest row is {latest.date()}, {stale_days} days before the "
            f"requested {requested.date()} (stale) — refusing to use it",
        )


def _cache_is_fresh(data_file, as_of_dt, now, zone=None) -> bool:
    """Whether the symbol's cached download can serve this request.

    The file holds the download made on the day it was written, so it serves
    only that day. A current-day request also refetches once the file is older
    than the TTL: Yahoo publishes a partial daily candle during market hours,
    whose ``Close`` is not the closing price, and row inspection cannot tell it
    from a final one (#1150).

    With ``zone`` (the listing's, see ``symbols.listing_zone``) the day is the
    listing's own. The machine's date can run ahead of it: in India it turns at
    midnight while the US session that opened the evening before still trades,
    so a download made then holds that session's forming bar under a date the
    machine already counts as past.
    """
    written = os.path.getmtime(data_file)
    if zone is None:
        written = pd.Timestamp.fromtimestamp(written)
        if written.date() != now.date():
            return False
        return as_of_dt.date() < now.date() or (now - written).total_seconds() <= OHLCV_CACHE_TTL_SECONDS
    moment = now.to_pydatetime().astimezone()   # a naive ``now`` is the machine's local time
    today = moment.astimezone(zone).date()
    if datetime.fromtimestamp(written, zone).date() != today:
        return False
    return as_of_dt.date() < today or moment.timestamp() - written <= OHLCV_CACHE_TTL_SECONDS


def load_ohlcv(symbol: str, as_of_date: str, fill_gaps: bool = True, *,
               max_stale_days: int | None = MAX_OHLCV_STALE_DAYS) -> pd.DataFrame:
    """Fetch OHLCV data with caching, filtered to prevent look-ahead bias.

    Downloads 5 years of data up to today, from earlier when as_of_date needs
    more history before it, and caches per symbol. On subsequent calls the
    cache is reused. Rows after as_of_date are filtered out so backtests never
    see future prices, and so are Yahoo's filler rows for days an NSE/BSE
    listing's exchange was closed.

    ``fill_gaps`` carries prices forward over gaps so indicators compute on a
    continuous series. Pass ``False`` to read the values as the vendor reported
    them, leaving a cell that was never reported empty.

    A frame whose newest row is more than ``max_stale_days`` calendar days
    before as_of_date is refused as stale (#1021). ``None`` serves it, for a
    caller that keeps only rows from dates of its own, which a stale frame
    cannot pass off as current prices.
    """
    # Resolve broker/forex symbols (XAUUSD+ -> GC=F) to Yahoo's convention,
    # then reject values that would escape the cache directory when
    # interpolated into the cache filename (e.g. ``../../tmp/x``).
    canonical = normalize_symbol(symbol)
    safe_symbol = safe_ticker_component(canonical)

    config = get_config()
    as_of_dt = pd.to_datetime(as_of_date).normalize()

    # One cache file per symbol, holding the latest 5y-to-today download. A date
    # the window does not reach far enough behind (a backtest of 2020) downloads
    # from the start of the year it needs instead, into a file named for that
    # year: the name says what the file covers, so it is never read as too short.
    now = pd.Timestamp.today()
    start_date = now - pd.DateOffset(years=5)
    cache_name = f"{safe_symbol}-YFin-data.csv"
    needed = as_of_dt - pd.Timedelta(days=HISTORY_BEFORE_AS_OF_DAYS)
    if needed < start_date:
        start_date = pd.Timestamp(needed.year, 1, 1)
        cache_name = f"{safe_symbol}-YFin-data-from-{needed.year}.csv"
    start_str = start_date.strftime("%Y-%m-%d")
    # yfinance ``end`` is EXCLUSIVE; request tomorrow so today's row is included
    # when as_of_date is the current day (#986). Look-ahead is still prevented by
    # the as_of_date filter below.
    end_str = (now + pd.Timedelta(days=1)).strftime("%Y-%m-%d")

    os.makedirs(config["data_cache_dir"], exist_ok=True)
    data_file = os.path.join(config["data_cache_dir"], cache_name)

    # A cached file may be empty if a prior fetch failed (unknown symbol,
    # transient rate limit). Treat an empty/columnless cache as a miss and
    # re-fetch rather than serving the poisoned file forever.
    data = None
    if os.path.exists(data_file):
        cached = pd.read_csv(data_file, on_bad_lines="skip", encoding="utf-8")
        if (
            not cached.empty
            and "Close" in cached.columns
            and _cache_is_fresh(data_file, as_of_dt, now, listing_zone(canonical))
        ):
            data = cached

    if data is None:
        # yf.download catches every error, a rate limit included, and returns
        # an empty frame. Ticker.history raises the rate limit, so it is retried.
        downloaded = yf_retry(lambda: yf.Ticker(canonical).history(
            start=start_str,
            end=end_str,
            auto_adjust=True,
            actions=False,
        ))
        if downloaded is None:
            raise_for_empty(symbol, canonical, "price rows")
        downloaded = _ensure_date_column(downloaded.reset_index())
        # Only cache real data — never persist an empty frame.
        if downloaded.empty or "Close" not in downloaded.columns:
            raise_for_empty(symbol, canonical, "price rows")
        replace_file(data_file, lambda temp: downloaded.to_csv(temp, index=False, encoding="utf-8"))
        data = downloaded

    data = _clean_dataframe(data)

    # Filter to as_of_date to prevent look-ahead bias in backtesting.
    data = data[data["Date"] <= as_of_dt]

    filler = _closed_day_filler(data, canonical)
    if filler.any():
        logger.info("%s: left out Yahoo's filler row for closed day(s) %s", canonical,
                    ", ".join(f"{day:%Y-%m-%d}" for day in data.loc[filler, "Date"]))
        data = data[~filler]

    # A closeless newest bar is an unsettled session, not a symbol without data.
    # _fill_price_gaps below drops it, here and mid-series alike, so the frame
    # ends at the last settled bar; only a range with no close anywhere is no
    # data (#1201, #1289).
    if not data.empty and pd.isna(data["Close"].iloc[-1]):
        settled = data["Close"].notna().to_numpy().nonzero()[0]
        if settled.size == 0:
            raise NoMarketDataError(
                symbol, canonical, "no bar in range has a closing price"
            )
        logger.warning(
            "%s: %d trailing bar(s) through %s have no closing price; using %s "
            "as the latest close.", canonical, len(data) - settled[-1] - 1,
            data["Date"].iloc[-1].date(), data["Date"].iloc[settled[-1]].date(),
        )

    # Indicators need a continuous series, so gaps are carried forward. A caller
    # that reports the numbers themselves asks for the frame as it was reported:
    # a filled cell is the previous session's price under this session's date.
    data = _fill_price_gaps(data) if fill_gaps else data.dropna(subset=["Close"]).copy()

    # Reject a stale frame (latest row far older than as_of_date) rather than
    # feeding year-old prices into indicators (#1021).
    if max_stale_days is not None:
        _assert_ohlcv_not_stale(data, as_of_date, symbol, canonical, max_stale_days=max_stale_days)

    return data


