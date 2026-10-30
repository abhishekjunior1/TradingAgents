"""Settling past decisions: once a decision's holding window has traded, score
it against its benchmark and record a reflection on it in the memory log."""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pandas as pd

from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.symbols import normalize_symbol
from tradingagents.dataflows.vendors.yahoo.market import get_closes
from tradingagents.markets.exchanges import exchange_for_symbol
from tradingagents.markets.india import INDICES, index_for
from tradingagents.memory import intraday_settlement

logger = logging.getLogger(__name__)

# Never a benchmark, nor offered as one: a volatility index has no return to measure a decision against.
# India VIX, and the others under Yahoo's symbols: Cboe's on the S&P 500 (VIX at each term, by its current
# and former names, and the volatility of VIX), on other indices, ETFs, stocks, gold, oil, the euro and
# Treasuries, and those on the Hang Seng, the Nikkei 225, the ASX 200 and the EURO STOXX 50.
_NOT_A_BENCHMARK = frozenset({
    "^INDIAVIX",
    "^VIX", "^VIX1D", "^VIX9D", "^VIX3M", "^VIX6M", "^VIX1Y", "^VXST", "^VXV", "^VXMT", "^VVIX",
    "^VXN", "^VXD", "^RVX", "^VXO", "^VXEEM", "^VXEFA", "^VXEWZ", "^VXFXI", "^VXGDX", "^VXSLV", "^VXXLE",
    "^VXAPL", "^VXAZN", "^VXGOG", "^VXGS", "^VXIBM", "^GVZ", "^OVX", "^EVZ", "^VXTLT", "^MOVE",
    "^VHSI", "^JNIV", "^AXVI", "V2TX.DE",
})


def resolve_benchmark(ticker: str, config: dict) -> str:
    """Pick the benchmark ticker for alpha calculation against ``ticker``.

    ``config["benchmark_ticker"]`` overrides everything when set; otherwise
    the suffix map matches the ticker's exchange suffix (e.g. ``.T`` for
    Tokyo), and an NSE/BSE index (``^NSEBANK``) takes its exchange's
    headline index (NIFTY 50, SENSEX; the headline index itself has no
    alpha, as SPY has none against SPY). US-listed tickers without a dotted
    suffix fall through to the empty-suffix entry (SPY by default).
    Unrecognised suffixes, including US tickers with dots like ``BRK.B``,
    also take the empty-suffix entry. Returns are compared as percentages,
    each in its own currency.
    """

    explicit = config.get("benchmark_ticker")
    if explicit:
        # Same alias mapping as the analyzed ticker; an unmapped alias finds
        # no prices, and the decision would stay pending for good.
        return normalize_symbol(explicit)
    benchmark_map = config.get("benchmark_map", {})
    ticker_upper = normalize_symbol(ticker)
    for suffix, benchmark in benchmark_map.items():
        if suffix and ticker_upper.endswith(suffix.upper()):
            return benchmark
    # An index has no listing suffix, but its exchange is known here.
    exchange = exchange_for_symbol(ticker_upper)
    if exchange is not None:
        return exchange.benchmark
    return benchmark_map.get("", "SPY")


def check_benchmark(benchmark: str, config: dict, as_of: str | None = None, seen: dict | None = None) -> str:
    """``benchmark`` as the Yahoo symbol decisions are measured against, once Yahoo is known to price it.

    A decision stored with a benchmark that has no prices could never be settled,
    and a volatility index (India VIX, VIX and the others in ``_NOT_A_BENCHMARK``)
    has no return to score it against: both are refused. An index Yahoo keeps no
    history for (NIFTY Midcap 100) is refused without asking. An index listed here
    with a daily history, or a ``benchmark_map`` value, is priced lately, and taken
    as it is unless ``as_of`` is older than 30 days: Yahoo's history of several NSE
    indices starts in 2010-2011. Any other symbol, and one of those at such a date,
    needs a Yahoo close in the 30 days to ``as_of``, the run's date (today when not
    given): a past-dated run needs prices at its own date. ``seen``, a dict the
    caller keeps, holds the closes already fetched, so a graph's later checks (a
    run's date, its decision once recorded, a backtest's next cell) are answered
    from them. Raises ValueError naming the choices, or VendorUnavailableError when
    Yahoo could not be asked.
    """
    symbol = normalize_symbol(benchmark)
    index = index_for(symbol)
    choices = ", ".join(i.name for i in INDICES if i.history and i.symbol not in _NOT_A_BENCHMARK)
    choose = f"Choose an index with a daily history ({choices}) or another symbol Yahoo prices, such as SPY."
    if symbol in _NOT_A_BENCHMARK:
        name = f"{index.name} ({symbol})" if index is not None else symbol
        raise ValueError(f"{name} is a volatility index, which has no return to measure a decision against, "
                         f"so it cannot be a benchmark. {choose}")
    if index is not None and not index.history:
        raise ValueError(f"Yahoo Finance keeps no daily history for {index.name} ({symbol}), so no decision "
                         f"could be settled against it as a benchmark. {choose}")
    today = _now().tz_localize(None).normalize()
    day = pd.Timestamp(as_of) if as_of else today
    listed = index is not None or symbol in set((config.get("benchmark_map") or {}).values())
    if listed and day >= today - timedelta(days=30):
        return symbol
    try:
        if _priced(symbol, day, seen):
            return symbol
    except VendorUnavailableError as exc:
        raise VendorUnavailableError(f"the benchmark {symbol} could not be checked: {exc}") from exc
    reason = (f"Yahoo Finance has no prices for {symbol!r} in "
              + (f"the 30 days to {as_of}" if as_of else "the last 30 days"))
    raise ValueError(f"{reason}, so no decision could be settled against it as a benchmark. {choose}")


def _priced(symbol: str, day: pd.Timestamp, seen: dict | None) -> bool:
    """Whether Yahoo has a close for ``symbol`` in the 30 days to ``day``.

    The closes are fetched from the window's start through today and kept in
    ``seen`` (when given) with the span asked for, so a later date (a backtest's
    next cell) or the same one (the run's decision, once recorded) is answered
    from them.
    """
    start, today = day - timedelta(days=30), _now().tz_localize(None).normalize()

    def found(closes):
        if closes.empty:
            return False
        dates = closes.index.tz_localize(None) if getattr(closes.index, "tz", None) else closes.index
        return bool(((dates >= start) & (dates < day + timedelta(days=1))).any())

    held = (seen or {}).get(symbol)
    if held is not None:
        span_start, span_end, closes = held
        if found(closes):
            return True
        if span_start <= start and day < span_end:
            return False    # that window was asked for: it has no close
    end = max(day, today) + timedelta(days=1)
    closes = get_closes(symbol, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
    closes = closes[closes > 0]
    # An answer with no closes at all is not kept: the next check asks again.
    if seen is not None and not closes.empty:
        seen[symbol] = (start, end, closes)
    return found(closes)


def _now() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC")


def _by_day(closes):
    """Final closes keyed by the calendar day of their bar, missing closes dropped.

    Yahoo stamps a daily bar at midnight in its market's time zone (UTC for a
    coin, New York for SPY), so two series meet on their dates, not their
    instants. A missing or non-positive close is no price at all, not a bar to
    score on. Today's bar, in its market's time zone, is the session still
    trading: its close is the live price, so it is not scored.
    """
    closes = closes[closes > 0]
    tz = getattr(closes.index, "tz", None)
    if not closes.empty:
        closes = closes[closes.index.date < _now().tz_convert(tz).date()]
    return closes if tz is None else closes.tz_localize(None)


def fetch_returns(
    ticker: str, trade_date: str, holding_days: int = 5,
    benchmark: str = "SPY",
) -> tuple[float | None, float | None, int | None, str | None]:
    """Fetch raw and alpha return for ticker over holding_days after trade_date.

    The position is entered at the first close after ``trade_date``: a run dated
    ``trade_date`` reads news published after that day's close (#1518).

    ``benchmark`` is the index used as the alpha baseline (resolved by the
    caller via ``resolve_benchmark``). Returns ``(raw_return, alpha_return,
    holding_days, resolution_date)`` — where ``resolution_date`` is the date
    of the last price bar used, i.e. when the outcome became known (#1251) —
    or ``(None, None, None, None)`` when the full holding window has not traded
    yet (#1169). A failure to get or read the prices, or a benchmark with no
    closes at all once the window has traded, or none on or before the entry
    session (a symbol listed since), is raised, for the caller to report.
    """
    start = datetime.strptime(trade_date, "%Y-%m-%d")
    today = _now().tz_localize(None).normalize()
    # The entry and holding_days more sessions all close after the decision date
    # and before today, with a day's slack for markets east of UTC: until then
    # the window cannot have traded and no prices are asked for.
    if (today - start).days < holding_days + 1:
        return None, None, None, None
    # holding_days counts trading days after the entry session, so ask for the
    # calendar span they occupy (about 7 for every 5) plus a week for holidays.
    end = start + timedelta(days=round((holding_days + 1) * 7 / 5) + 7)
    # From a week earlier, so the benchmark has a close on or before entry.
    bench_start = (start - timedelta(days=7)).strftime("%Y-%m-%d")

    def closes(until):
        # Closes for the instrument the analysis priced (XAUUSD -> GC=F, #984),
        # from the first session after the decision date.
        stock = _by_day(get_closes(ticker, trade_date, until.strftime("%Y-%m-%d")))
        return stock[stock.index > start], _by_day(get_closes(benchmark, bench_start, until.strftime("%Y-%m-%d")))

    def traded(stock, bench):
        return len(stock) > holding_days and not bench.empty and bench.index[-1] >= stock.index[holding_days]

    stock, bench = closes(end)
    # A window that has passed without trading through spans a closure longer than
    # the week allowed for holidays (a national holiday, a halt): ask through today.
    if not traded(stock, bench) and end < today:
        stock, bench = closes(today + timedelta(days=1))

    # Require the full holding window to have traded. A rerun before it has
    # leaves the entry pending to retry next run, rather than settling on a
    # premature partial return (#1169).
    if len(stock) <= holding_days:
        return None, None, None, None
    # A symbol Yahoo has no closes for (a mistyped --benchmark) will not trade
    # later: waiting would keep the decision pending for good, unexplained.
    if bench.empty:
        raise ValueError(f"no closes for the benchmark {benchmark}")
    entry, exit_ = stock.index[0], stock.index[holding_days]
    # The window is the stock's own sessions; the benchmark is valued at its
    # last close by each end, so both returns span the same dates even when
    # the calendars differ (a coin trades at weekends, an index does not).
    # Its closes from a week before the decision start after the entry: that
    # past will not change, so waiting would keep the decision pending for good.
    if bench.index[0] > entry:
        raise ValueError(f"no close for the benchmark {benchmark} on or before the entry session "
                         f"{entry:%Y-%m-%d}")
    # It must have traded through the exit, or its close there may yet move.
    if bench.index[-1] < exit_:
        return None, None, None, None

    raw = float((stock.iloc[holding_days] - stock.iloc[0]) / stock.iloc[0])
    bench_entry, bench_exit = bench.asof(entry), bench.asof(exit_)
    bench_ret = float((bench_exit - bench_entry) / bench_entry)
    alpha = raw - bench_ret
    # Every close used is known by the exit: the point-in-time cutoff for
    # injecting the lesson (#1251).
    resolution_date = exit_.strftime("%Y-%m-%d")
    return raw, alpha, holding_days, resolution_date


@dataclass
class Settlement:
    """What a settlement pass did: the decisions it settled and those it could not."""

    settled: list[tuple[str, str]] = field(default_factory=list)        # (ticker, date)
    failed: list[tuple[str, str, str]] = field(default_factory=list)    # (ticker, date, reason)


def settle_pending(ticker: str, memory_log, reflector, config: dict, wait: bool = True) -> Settlement:
    """Settle ``ticker``'s pending decisions whose holding window has traded.

    One pass at a time per log (``memory_log.settling()``), so runs sharing a log
    never pay for the same reflection twice. Each outcome is written as soon as
    its reflection is done, so an interrupted pass keeps what it finished.
    Entries whose window has not traded yet are skipped; an entry whose prices
    or reflection could not be had stays pending and is reported as failed.
    With ``wait=False`` a pass already running elsewhere is left to do the work.
    Each decision is measured against the benchmark recorded with it, the one
    its own run chose; one recorded without takes its listing's, never the
    settling run's ``benchmark_ticker``, which is for that run's decision.
    An intraday entry is replayed on its session's bars instead
    (``intraday_settlement``), once that session's close is final; one whose
    replay fails is reported the same way, and the rest still settle.
    """
    result = Settlement()
    with memory_log.settling(wait=wait) as held:
        if not held:
            return result
        pending = [e for e in memory_log.get_pending_entries() if e["ticker"] == ticker]
        if not pending:
            return result
        listing_benchmark = resolve_benchmark(ticker, {**config, "benchmark_ticker": None})
        for entry in pending:
            if entry.get("horizon") == "intraday":
                try:
                    status, reason = intraday_settlement.settle(ticker, entry, memory_log, reflector,
                                                                config=config)
                except Exception as exc:
                    # One plan that cannot be replayed leaves the others, and later tickers, to settle.
                    logger.warning("The intraday plan for %s on %s could not be settled: %s",
                                   ticker, entry["date"], exc)
                    status, reason = "failed", f"intraday settlement failed: {exc}"
                if status == "settled":
                    result.settled.append((ticker, entry["date"]))
                elif status == "failed":
                    result.failed.append((ticker, entry["date"], reason))
                continue
            benchmark = entry.get("benchmark") or listing_benchmark
            try:
                raw, alpha, days, resolution_date = fetch_returns(
                    ticker, entry["date"], config.get("holding_period_days", 5), benchmark=benchmark,
                )
            except Exception as exc:
                logger.warning("Prices for settling %s on %s were unavailable: %s", ticker, entry["date"], exc)
                result.failed.append((ticker, entry["date"], f"prices unavailable: {exc}"))
                continue
            if raw is None:
                continue  # the holding window has not traded yet: try again next run
            # An instrument measured against itself (NIFTY 50, SENSEX, SPY) has an alpha of zero by
            # construction; the reflection is told so, and to judge the call on its raw return.
            label = benchmark
            if normalize_symbol(benchmark) == normalize_symbol(ticker):
                label = (f"{benchmark} (the instrument itself, so this alpha is zero by construction; "
                         "judge the call on the raw return above)")
            try:
                reflection = reflector.reflect_on_final_decision(
                    final_decision=entry.get("decision", ""),
                    raw_return=raw,
                    alpha_return=alpha,
                    benchmark_name=label,
                    holding_days=days,
                )
            except Exception as exc:
                # Reflection calls a provider: a transient failure leaves the entry
                # pending for the next pass rather than stopping the analysis.
                logger.warning("Reflection failed for %s on %s: %s", ticker, entry["date"], exc)
                result.failed.append((ticker, entry["date"], f"reflection failed: {exc}"))
                continue
            # The benchmark goes with the outcome: one recorded before benchmarks were has it written, so
            # its lesson names it and a backtest can tell an instrument measured against itself.
            if memory_log.update_with_outcome(ticker, entry["date"], raw, alpha, days, reflection,
                                              resolution_date=resolution_date, benchmark=benchmark):
                result.settled.append((ticker, entry["date"]))
    return result


def settle_all_pending(memory_log, reflector, config: dict, wait: bool = True) -> Settlement:
    """Settle every ticker's pending decisions whose holding window has traded (#1445).

    A run settles its own ticker; a ticker no longer analysed would otherwise
    keep its decisions pending, and their lessons out of later runs.
    """
    result = Settlement()
    for ticker in dict.fromkeys(e["ticker"] for e in memory_log.get_pending_entries()):
        done = settle_pending(ticker, memory_log, reflector, config, wait=wait)
        result.settled.extend(done.settled)
        result.failed.extend(done.failed)
    return result
