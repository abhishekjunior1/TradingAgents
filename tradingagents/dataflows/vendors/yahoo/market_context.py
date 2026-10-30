"""The market around an instrument: benchmarks, its sector index, volatility, the rupee and crude.

For an NSE/BSE instrument: NIFTY 50, SENSEX, NIFTY Bank, India VIX, the closest
NIFTY sector index, USD/INR and Brent crude, with the stock's return relative
to the benchmark and its sector. Elsewhere: the configured benchmark only.

Every series is Yahoo's daily closes up to the run date (``load_ohlcv``, so a
backtest never sees a later close), and every row carries the date of its own
last close: Yahoo publishes some index closes a day late, and a reader must be
able to see that rather than take yesterday's level for today's.
"""

import logging
from datetime import date

import pandas as pd
import yfinance as yf

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.errors import NoMarketDataError, VendorError
from tradingagents.dataflows.symbols import normalize_symbol
from tradingagents.dataflows.vendors.yahoo.common import price_text, yf_retry
from tradingagents.dataflows.vendors.yahoo.ohlcv import load_ohlcv
from tradingagents.markets import india, risk
from tradingagents.markets.exchanges import exchange_for_symbol, expected_last_session
from tradingagents.markets.money import format_percent

logger = logging.getLogger(__name__)

# Sessions over which changes are reported: a day, a week, a month, a quarter.
_WINDOWS = (("1D", 1), ("1W", 5), ("1M", 21), ("3M", 63))

# What the data is, per Yahoo's own exchange list (help.yahoo.com/kb/SLN2310).
SOURCE_NOTE = ("Yahoo Finance daily bars, read through the unofficial yfinance library. Yahoo lists NSE quotes "
               "as real-time and BSE quotes as 15-minute delayed, and says its data is not intended for trading; "
               "today's bar is intraday until the exchange's close is final.")


_CLASSIFICATION: dict[str, tuple[str | None, str | None]] = {}


def _classification(symbol: str) -> tuple[str | None, str | None]:
    """Yahoo's present-day sector and industry for ``symbol``, or (None, None).

    An answer is kept for the process; a failed lookup is asked again next time.
    """
    if symbol in _CLASSIFICATION:
        return _CLASSIFICATION[symbol]
    try:
        info = yf_retry(lambda: yf.Ticker(symbol).info) or {}
    except VendorError as exc:   # the sector index is optional context
        logger.debug("No classification for %s: %s", symbol, exc)
        return None, None
    _CLASSIFICATION[symbol] = (info.get("sector"), info.get("industry"))
    return _CLASSIFICATION[symbol]


def _closes(symbol: str, as_of_date: str) -> pd.Series:
    """Daily closes up to ``as_of_date``, indexed by date; raises VendorError when there are none."""
    frame = load_ohlcv(symbol, as_of_date)
    series = pd.Series(frame["Close"].to_numpy(), index=pd.to_datetime(frame["Date"]).dt.date).dropna()
    if series.empty:
        raise NoMarketDataError(symbol, symbol, "no closing prices")
    return series


def _row(label: str, symbol: str, closes: pd.Series, expected: date | None) -> str:
    last_date = closes.index[-1]
    changes = " | ".join(format_percent(risk.total_return(closes, n)) for _, n in _WINDOWS)
    late = ""
    if expected is not None and last_date < expected:
        late = f" (Yahoo has not published the {expected:%d %b} close)"
    return f"| {label} | {symbol} | {price_text(float(closes.iloc[-1]))} | {last_date}{late} | {changes} |"


def _series_plan(symbol: str) -> tuple[list[tuple[str, str]], object]:
    """(label, Yahoo symbol) of each series to show for ``symbol``, and the sector index if any."""
    exchange = exchange_for_symbol(symbol)
    if exchange is None:
        config = get_config()
        suffix = next((s for s in sorted(config.get("benchmark_map", {}), key=len, reverse=True)
                       if s and symbol.endswith(s)), "")
        benchmark = config.get("benchmark_ticker") or config.get("benchmark_map", {}).get(suffix, "SPY")
        return [("Benchmark", benchmark)], None
    plan = [(index.name, index.symbol) for index in
            (india.INDEX_BY_SYMBOL[s] for s in ("^NSEI", "^BSESN", "^NSEBANK", "^INDIAVIX"))]
    sector = None
    if symbol.upper() not in india.INDEX_BY_SYMBOL:
        sector = india.sector_index_for(*_classification(symbol))
        if sector is not None and sector.symbol not in {s for _, s in plan}:
            plan.append((f"{sector.name} (closest sector index)", sector.symbol))
    plan += [("USD/INR", india.USD_INR), ("Brent crude (USD)", india.BRENT)]
    return plan, sector


def get_market_context(symbol: str, as_of_date: str) -> str:
    """The instrument's market context as of ``as_of_date``: one row per series, then relative performance."""
    canonical = normalize_symbol(symbol)
    plan, sector = _series_plan(canonical)
    exchange = exchange_for_symbol(canonical)
    expected = expected_last_session(exchange, date.fromisoformat(as_of_date)) if exchange else None

    rows, loaded, missing = [], {}, []
    for label, series_symbol in plan:
        try:
            closes = _closes(series_symbol, as_of_date)
        except VendorError as exc:
            missing.append(f"{label} ({series_symbol}): unavailable ({exc})")
            continue
        loaded[series_symbol] = closes
        # USD/INR and Brent trade on other calendars; only Indian series are held to NSE's.
        rows.append(_row(label, series_symbol, closes, expected if exchange_for_symbol(series_symbol) else None))

    header = "| Series | Symbol | Last close | Close date | 1D | 1W | 1M | 3M |\n|---|---|---|---|---|---|---|---|"
    lines = [
        f"# Market context for {canonical} as of {as_of_date}",
        f"Source: {SOURCE_NOTE} Each row gives the date of its own last close; changes are price returns over "
        "the last 1, 5, 21 and 63 sessions of that series.",
        "",
        header,
        *rows,
    ]
    if missing:
        lines += ["", "Not available:", *(f"- {m}" for m in missing)]

    relative = _relative_lines(canonical, as_of_date, plan, loaded, sector)
    if relative:
        lines += ["", f"Relative performance of {canonical} (its return minus the index's, same sessions):", *relative]
    return "\n".join(lines)


def get_watchlist_snapshot(symbols: list[str], as_of_date: str) -> str:
    """One row per symbol: last close and its date, changes, and 1M / 3M return against its benchmark."""
    rows, benchmarks, expected = [], {}, {}
    for raw in symbols:
        symbol = normalize_symbol(raw)
        try:
            closes = _closes(symbol, as_of_date)
        except VendorError as exc:
            rows.append(f"| {symbol} | unavailable ({exc}) | | | | | | | |")
            continue
        exchange = exchange_for_symbol(symbol)
        late = ""
        if exchange is not None:
            if exchange.code not in expected:
                expected[exchange.code] = expected_last_session(exchange, date.fromisoformat(as_of_date))
            if closes.index[-1] < expected[exchange.code]:
                late = f" ({expected[exchange.code]:%d %b} not yet published)"
        benchmark = exchange.benchmark if exchange else _series_plan(symbol)[0][0][1]
        if benchmark not in benchmarks:
            try:
                benchmarks[benchmark] = _closes(benchmark, as_of_date)
            except VendorError:
                benchmarks[benchmark] = None
        bench = benchmarks[benchmark]
        relative = [risk.relative_return(closes, bench, n) if bench is not None else None for n in (21, 63)]
        changes = " | ".join(format_percent(risk.total_return(closes, n)) for _, n in _WINDOWS)
        gaps = " | ".join("n/a" if g is None else f"{g * 100:+.1f} pts" for g in relative)
        rows.append(f"| {symbol} | {price_text(float(closes.iloc[-1]))} | {closes.index[-1]}{late} | {changes} | "
                    f"{gaps} |")
    return "\n".join([
        f"# Watchlist as of {as_of_date}",
        f"Source: {SOURCE_NOTE} Relative columns: the symbol's return minus its exchange benchmark's "
        "(NIFTY 50 for NSE, SENSEX for BSE) over the same 21 / 63 sessions.",
        "",
        "| Symbol | Last close | Close date | 1D | 1W | 1M | 3M | 1M vs benchmark | 3M vs benchmark |",
        "|---|---|---|---|---|---|---|---|---|",
        *rows,
    ])


def _relative_lines(canonical, as_of_date, plan, loaded, sector) -> list[str]:
    if canonical.upper() in india.INDEX_BY_SYMBOL or not plan:
        return []
    try:
        own = _closes(canonical, as_of_date)
    except VendorError as exc:
        return [f"- unavailable: no closes for {canonical} ({exc})"]
    lines = []
    benchmark_symbol = plan[0][1]
    for name, symbol in ((plan[0][0], benchmark_symbol),
                         *(((sector.name, sector.symbol),) if sector is not None else ())):
        closes = loaded.get(symbol)
        if closes is None:
            continue
        cells = []
        for label, n in (("1M", 21), ("3M", 63), ("1Y", 250)):
            gap = risk.relative_return(own, closes, n)
            cells.append(f"{label} {'n/a' if gap is None else f'{gap * 100:+.1f} pts'}")
        lines.append(f"- vs {name} ({symbol}): " + ", ".join(cells))
    if sector is not None:
        lines.append(f"  The sector index is the closest NIFTY index to Yahoo's present-day classification of "
                     f"{canonical}, not NSE's index membership.")
    return lines
