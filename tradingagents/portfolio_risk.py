"""The book's risk, computed before a run, for the agents that size the trade.

Deterministic: daily prices up to the run date from the data layer, arithmetic
from ``markets.risk``, costs and tax from the configured ``markets.costs``
profiles. Whatever cannot be computed (a holding without prices, a book in
several currencies or in one not known here) is said to be unavailable, never
estimated.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date

import pandas as pd

from tradingagents.dataflows import date_window
from tradingagents.dataflows.symbols import normalize_symbol
from tradingagents.markets import costs, risk
from tradingagents.markets.exchanges import SessionStatus, exchange_for_symbol, session_status
from tradingagents.markets.india import INDEX_BY_SYMBOL
from tradingagents.markets.money import format_money, format_percent, format_price, format_quantity
from tradingagents.portfolio import PortfolioContext, listing_currency

logger = logging.getLogger(__name__)

ONE_YEAR = 250   # sessions
_TRADING = ("pre-open", "open", "closing")


def _closes(frame: pd.DataFrame) -> pd.Series:
    index = pd.to_datetime(frame["Date"]) if "Date" in frame else frame.index
    return pd.Series(pd.to_numeric(frame["Close"], errors="coerce").to_numpy(), index=index).dropna()


def _forming(symbol: str, closes: pd.Series) -> SessionStatus | None:
    """The exchange's session status when ``closes`` ends on its session still trading, whose
    Close is the latest price, not a close; None otherwise (only NSE and BSE have a calendar here)."""
    exchange = exchange_for_symbol(symbol)
    if exchange is None or closes.empty:
        return None
    status = session_status(exchange)
    return status if status.state in _TRADING and closes.index[-1].date() == status.local_time.date() else None


def _price_at(closes: pd.Series, status: SessionStatus | None, currency: str | None) -> str:
    """The newest price and what it is: a day's close, or the latest price of a session still trading."""
    price = format_price(float(closes.iloc[-1]), currency)
    if status is None:
        return f"last close {price} ({closes.index[-1].date()})"
    ex = status.exchange
    return (f"latest price {price} ({status.local_time:%Y-%m-%d %H:%M} {ex.tz_label}, session still trading; "
            f"not a close, which is final after {ex.close_final:%H:%M} {ex.tz_label})")


def _benchmark_for(symbol: str, config: dict) -> tuple[str, str]:
    """(Yahoo symbol, name) of the index the book is measured against: a swing run's own
    (``benchmark_ticker``), else the listing's."""
    # An intraday plan has no benchmark: its run checks none, and its agents are told the
    # listing's index (agents.context._own_benchmark), so benchmark_ticker is not read.
    intraday = str(config.get("decision_horizon") or "").strip().lower() == "intraday"
    if config.get("benchmark_ticker") and not intraday:
        # TRADINGAGENTS_BENCHMARK_TICKER reaches the config as written ("NIFTY IT").
        own = normalize_symbol(config["benchmark_ticker"])
        index = INDEX_BY_SYMBOL.get(own)
        return own, index.name if index else own
    exchange = exchange_for_symbol(symbol)
    if exchange is not None:
        return exchange.benchmark, exchange.benchmark_name
    benchmark_map = config.get("benchmark_map", {})
    suffix = next((s for s in sorted(benchmark_map, key=len, reverse=True) if s and symbol.endswith(s)), "")
    found = benchmark_map.get(suffix, "SPY")
    return found, found


def portfolio_risk_block(
    portfolio: PortfolioContext,
    ticker: str,
    trade_date: str,
    prices: Callable[[str], pd.DataFrame],
    sector_of: Callable[[str], str | None],
    config: dict,
    historical: bool = True,
) -> str:
    """The risk figures for ``portfolio`` as of ``trade_date``, and a sizing reference for ``ticker``.

    ``prices(symbol)`` returns daily OHLC rows up to the run date (raising when
    it has none); ``sector_of(symbol)`` a sector or None. A past-dated run
    (``historical``, ``date_window.is_historical``) leaves out the lots acquired
    after ``trade_date`` and estimates tax on a sale that day; a current run counts
    every lot, the book being today's, and estimates tax on a sale today.
    """
    analyzed = normalize_symbol(ticker)
    frames: dict[str, pd.DataFrame | None] = {}

    def frame_of(symbol: str) -> pd.DataFrame | None:
        if symbol not in frames:
            try:
                frame = prices(symbol)
                frames[symbol] = frame if frame is not None and len(frame) else None
            except Exception as exc:  # noqa: BLE001 — one holding's prices must not sink the block
                logger.warning("No prices for %s: %s", symbol, exc)
                frames[symbol] = None
        return frames[symbol]

    # A lot acquired after a past run date was not held on it (a backtest with today's book): it
    # adds nothing, and its price and date stay out of the text. In a current run a lot can carry
    # a later date than the run's, written on a calendar ahead of the listing's (an IST date on a
    # US run dated by New York's day), and is held all the same.
    held_then = portfolio.held_on(trade_date) if historical else portfolio
    later = len(portfolio.positions) - len(held_then.positions)
    holdings = [(normalize_symbol(p.ticker), p) for p in held_then.positions]
    # Holdings are priced in their listing's currency (the book's label off NSE/BSE, the US and
    # crypto pairs), cash in the label: more than one currency between them and no total can be summed.
    label = (portfolio.currency or "").strip().upper() or None
    found = {(p.price_currency(label) or "").upper() for _, p in holdings} - {""}
    if label and portfolio.cash is not None:
        found.add(label)
    mixed = len(found) > 1
    # With no label, a holding off those listings is in a currency not known here, which no
    # total can include, as in book_value.
    unknown = list(dict.fromkeys(s for s, p in holdings if p.price_currency(label) is None))
    totals = not mixed and not unknown
    quote = listing_currency(analyzed)
    # Unlabelled cash, with nothing else in a known currency, counts as the listing's, as in book_value.
    currency = (next(iter(found)) if found else (label or quote)) if totals else None

    def money(value: float | None) -> str:
        return format_money(value, currency)

    benchmark, benchmark_name = _benchmark_for(analyzed, config)
    bench_frame = frame_of(benchmark)
    bench = _closes(bench_frame) if bench_frame is not None else None
    if bench is not None and _forming(benchmark, bench):
        bench = bench.iloc[:-1]

    unit = currency or ("the book currency" if totals else "each holding's currency")
    lines = [f"Portfolio risk as of {trade_date} (computed from Yahoo Finance daily closes; "
             f"figures marked n/a could not be computed; amounts in {unit}):"]

    values, sectors, unpriced = {}, {}, []
    position_lines = []
    forming = False
    for symbol, position in holdings:
        frame = frame_of(symbol)
        closes = _closes(frame) if frame is not None else None
        if closes is None or closes.empty:
            if symbol not in unpriced:
                unpriced.append(symbol)
            continue
        status = _forming(symbol, closes)
        forming = forming or status is not None
        # The statistics use closes through the last complete session.
        complete = closes.iloc[:-1] if status else closes
        last = float(closes.iloc[-1])
        own = position.price_currency(label) or currency
        value = position.quantity * last
        # Lots of one instrument add up; a short lot adds nothing to the long side, as in book_value.
        values[symbol] = values.get(symbol, 0.0) + max(value, 0.0)
        sectors[symbol] = sectors.get(symbol) or position.sector or sector_of(symbol)
        year = complete.iloc[-(ONE_YEAR + 1):]
        # Under a year of history the figures say the span they cover.
        full = len(complete) > ONE_YEAR
        span = "" if full or year.empty else f" over the {len(year)} sessions since {year.index[0].date()}"
        prefix = "1Y " if full else ""
        drawdown = risk.max_drawdown(year)
        details = [
            f"{format_quantity(position.quantity)} units at {_price_at(closes, status, own)} = {format_money(value, own)}",
            f"{prefix}volatility {format_percent(risk.annualized_volatility(year), signed=False)}{span}",
            f"{prefix}max drawdown {format_percent(drawdown.depth if drawdown else None)}{span}",
        ]
        if bench is not None:
            beta = risk.beta(year, bench.iloc[-(ONE_YEAR + 1):])
            details.append(f"beta to {benchmark_name} {'n/a' if beta is None else f'{beta:.2f}'}")
            gap = risk.relative_return(complete, bench, ONE_YEAR)
            details.append(f"1Y return vs {benchmark_name} {'n/a' if gap is None else f'{gap * 100:+.1f} pts'}")
        if position.average_price is not None and position.average_price > 0:
            pnl = (last - position.average_price) * position.quantity
            # Against the entry value, so a short in profit reads as a gain.
            basis = abs(position.quantity) * position.average_price
            details.append(f"unrealized P/L {format_money(pnl, own)} "
                           f"({format_percent(pnl / basis if basis else None)})")
            details.append(_tax_line(position, pnl, trade_date, config, historical))
        position_lines.append(f"- {symbol}: " + "; ".join(d for d in details if d))

    invested = sum(v for v in values.values() if v > 0)
    cash = portfolio.cash
    if mixed:
        lines.append("- The book holds several currencies, so totals, weights and concentration are not computed.")
    elif unknown:
        lines.append(f"- The book names no currency, and none is known here for the prices of {', '.join(unknown)}, "
                     "so totals, weights and concentration are not computed.")
    else:
        total = invested + (cash or 0)
        notes = ["negative: money owed"] if cash is not None and cash < 0 else []
        if cash is not None and label is None and currency:
            # An assumption, not a fact the caller gave.
            notes.append(f"the book names no currency, so taken as {currency}")
        cash_text = "not given" if cash is None else money(cash) + (f" ({'; '.join(notes)})" if notes else "")
        if cash is not None and cash >= 0 and total > 0:
            share = f" (cash {format_percent(cash / total, signed=False)})"
        else:
            # A share of the total means nothing for money owed, or for a book worth nothing.
            share = " (the book owes more than its holdings are worth)" if total < 0 else ""
        lines.append(f"- Holdings {money(invested)}; cash {cash_text}; total {money(total)}{share}")
    lines += position_lines
    if forming:
        lines.append("- Where a holding shows its latest price, its volatility, drawdown, beta and return use "
                     "closes through the last complete session, without the session still trading.")
    if unpriced:
        lines.append(f"- No prices up to {trade_date} for: {', '.join(unpriced)} (left out of the figures).")
    if later:
        lines.append(f"- Left out: {later} lot(s) acquired after {trade_date}, not held yet on that date.")

    if totals and values:
        found = risk.concentration(values)
        if found:
            lines.append(f"- Concentration: largest position {found.largest} at "
                         f"{format_percent(found.largest_weight, signed=False)} of holdings; effective number of "
                         f"holdings {found.effective_holdings:.1f} (HHI {found.hhi:.2f})")
        exposure = risk.exposure_by(values, sectors)
        if exposure:
            lines.append("- Sector exposure (share of holdings): " + ", ".join(
                f"{name} {format_percent(share, signed=False, decimals=1)}" for name, share in exposure.items()))

    if not mixed:
        lines += _sizing_lines(analyzed, frame_of(analyzed), invested + (cash or 0), currency, quote,
                               values.get(analyzed, 0.0), config, unknown)
    else:
        lines.append(f"- Sizing reference: n/a (the book holds several currencies and no exchange rate is applied "
                     f"here, so it has no single value to size {analyzed} against).")
    return "\n".join(lines)


def book_value(portfolio: PortfolioContext, ticker: str, prices: Callable[[str], pd.DataFrame]) -> float | None:
    """The book's value to size a paper trade in ``ticker`` against: its long holdings
    at their last close plus cash, as the risk figures total them.

    None when it cannot stand for capital in the listing's currency: nothing to
    value, a holding without prices, or a holding or the cash in another currency
    than the listing's (a holding off NSE/BSE, the US and crypto pairs, and the
    cash, are in the book's label; unlabelled cash counts as the listing's). A
    listing whose currency is not known here gets None.
    """
    currency = listing_currency(normalize_symbol(ticker))
    if currency is None:
        return None
    label = (portfolio.currency or "").strip().upper() or None
    total = 0.0
    for position in portfolio.positions:
        symbol = normalize_symbol(position.ticker)
        if position.price_currency(label) != currency:
            return None
        try:
            closes = _closes(prices(symbol))
        except Exception as exc:  # noqa: BLE001 — a holding without prices leaves the book unvalued
            logger.warning("No prices for %s, so the book is not valued: %s", symbol, exc)
            return None
        if closes.empty:
            return None
        total += max(position.quantity * float(closes.iloc[-1]), 0.0)
    if portfolio.cash is not None:
        if (label or currency) != currency:
            return None
        total += portfolio.cash
    return total if total > 0 else None


def _tax_line(position, pnl: float, trade_date: str, config: dict, historical: bool) -> str:
    # The rules are Indian, so a misconfigured rule fails only a book holding NSE/BSE listings.
    symbol = normalize_symbol(position.ticker)
    listing = exchange_for_symbol(symbol)
    if listing is None:
        return ""
    rules = costs.get_capital_gains_rules(config.get("capital_gains_rules"), config.get("capital_gains_overrides"))
    if rules is None:
        return ""
    if not position.acquired:
        return "tax on a sale depends on the holding period (purchase date not given)"
    acquired, sold = date.fromisoformat(position.acquired), date.fromisoformat(trade_date)
    if not historical:
        # A current run's sale "now" is today's on the holding's exchange, whose date need not be the
        # run's (India's is a day ahead on a US run dated by New York's day, or on a machine whose date
        # is behind India's): every lot is counted to it, whichever ticker the run analyses.
        sold = date.fromisoformat(date_window.listing_date(symbol))
    if acquired > sold:
        # Only a current run gets here (a past-dated one leaves such a lot out): a purchase dated
        # after today says nothing about how long the lot has been held.
        today = "" if historical else f" (today in {listing.tz_label})"
        return f"purchase date {position.acquired} is after {sold}{today}, so no holding period or tax is estimated"
    days = (sold - acquired).days
    # The term is counted in calendar months: 12 months are 365 or 366 days.
    long_term = costs.is_long_term(rules, acquired, sold)
    tax = costs.capital_gains_tax(rules, pnl, days, long_term=long_term)
    term = "long-term" if long_term else "short-term"
    return (f"held {days} days ({term}); estimated tax if sold now {format_money(tax, 'INR')} "
            f"under the '{rules.name}' example rules (as of {rules.as_of}; before surcharge, cess and any "
            f"exemption already used this year)")


def _sizing_lines(symbol: str, frame, capital: float, currency: str | None, quote: str | None, held: float,
                  config: dict, unknown: list[str]) -> list[str]:
    """A rule-of-thumb size for ``symbol`` (priced in ``quote``) against a book worth ``capital`` in
    ``currency`` that already holds ``held`` of it; ``unknown`` are holdings of an unlabelled book
    whose currency is not known here, which leave it without a value."""
    if symbol.startswith("^") or symbol.upper() in INDEX_BY_SYMBOL:
        return [f"- Sizing reference: n/a ({symbol} is an index, which is not traded as shares; "
                "size an ETF or a futures lot on it instead)."]
    if quote is None:
        return [f"- Sizing reference: n/a (the currency {symbol} trades in is not known here, so its price "
                "is not set against the book)."]
    if unknown:
        return [f"- Sizing reference: n/a (the book names no currency, and none is known here for the prices of "
                f"{', '.join(unknown)}, so the book has no value to size {symbol} against)."]
    if quote != currency:
        return [f"- Sizing reference: n/a (the book is in {currency} and {symbol} trades in {quote}; "
                "no exchange rate is applied)."]
    closes = _closes(frame) if frame is not None else None
    if closes is None or closes.empty or not capital or capital <= 0:
        return ["- Sizing reference: n/a (no prices for the instrument, or no capital to size against)."]
    status = _forming(symbol, closes)
    entry = float(closes.iloc[-1])
    # The stop's ATR uses complete sessions: a session still trading has no final range yet.
    atr = risk.average_true_range(frame.iloc[:-1] if status else frame)
    risk_pct = float(config.get("risk_per_trade_pct") or 1.0)
    max_pct = float(config.get("max_position_pct") or 20.0)
    if atr is None:
        return [f"- Sizing reference: n/a (too little history for an ATR stop on {symbol})."]
    stop = entry - 2 * atr
    budget = risk.position_size(capital, risk_pct / 100, entry, stop)
    if budget is None or budget.quantity <= 0:
        return [f"- Sizing reference: n/a (the risk budget buys no whole share of {symbol} at {format_price(entry, currency)})."]
    # The maximum position caps the holding after the trade, so what is already held counts.
    room = capital * (max_pct / 100) - held
    quantity = min(budget.quantity, int(room / entry) if room > 0 else 0)
    if quantity <= 0:
        why = (f"{symbol} is already {format_percent(held / capital, signed=False)} of the book, which leaves no "
               f"room under the {max_pct:g}% maximum position for a share at {format_price(entry, currency)}"
               if held > 0 else
               f"the {max_pct:g}% maximum position ({format_money(capital * max_pct / 100, currency)}) buys no "
               f"whole share of {symbol} at {format_price(entry, currency)}")
        return [f"- Sizing reference: n/a ({why})."]
    value = quantity * entry
    if status is None:
        below = f"the last close {format_price(entry, currency)} (stop {format_price(stop, currency)})"
    else:
        below = (f"the latest price {format_price(entry, currency)} ({status.local_time:%H:%M} "
                 f"{status.exchange.tz_label}, session still trading; stop {format_price(stop, currency)})")
    lines = [
        f"- Sizing reference for {symbol} (a rule of thumb, not a recommendation): risking {risk_pct:g}% of the "
        f"book ({format_money(capital * risk_pct / 100, currency)}) with a stop 2 x ATR(14) below {below} "
        f"allows about {quantity:,} shares, {format_money(value, currency)}"
        + (f"; with the {format_money(held, currency)} already held, the position would be "
           f"{format_percent((held + value) / capital, signed=False)} of the book" if held > 0 else "")
        + (f", held to the {max_pct:g}% maximum position" if quantity < budget.quantity else "") + "."
    ]
    listing = exchange_for_symbol(symbol)
    if listing is not None:
        profile = costs.get_cost_profile(config.get("cost_profile"), config.get("cost_overrides"))
        if profile.name != "none":
            buy = costs.order_costs(profile, "buy", value, listing.code)
            trip = costs.round_trip_costs(profile, value, venue=listing.code)
            lines.append(
                f"- Estimated charges on that trade under the '{profile.name}' example profile (rates as of "
                f"{profile.as_of}; verify with your broker): buying {format_money(buy.total, 'INR')} "
                f"({format_percent(buy.total / value, signed=False, decimals=3)}), a round trip at "
                f"the same price {format_money(trip, 'INR')} ({format_percent(trip / value, signed=False, decimals=3)}). "
                "Capital-gains tax is extra."
            )
    return lines
