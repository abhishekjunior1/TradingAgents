"""The book's risk, computed before a run, for the agents that size the trade.

Deterministic: daily prices up to the run date from the data layer, arithmetic
from ``markets.risk``, costs and tax from the configured ``markets.costs``
profiles. Whatever cannot be computed (a holding without prices, a book in
several currencies) is said to be unavailable, never estimated.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date

import pandas as pd

from tradingagents.dataflows.symbols import is_us_listing, normalize_symbol
from tradingagents.markets import costs, risk
from tradingagents.markets.exchanges import exchange_for_symbol
from tradingagents.markets.money import format_money, format_percent
from tradingagents.portfolio import PortfolioContext

logger = logging.getLogger(__name__)

ONE_YEAR = 250   # sessions


def _closes(frame: pd.DataFrame) -> pd.Series:
    index = pd.to_datetime(frame["Date"]) if "Date" in frame else frame.index
    return pd.Series(pd.to_numeric(frame["Close"], errors="coerce").to_numpy(), index=index).dropna()


def _benchmark_for(symbol: str, config: dict) -> tuple[str, str]:
    """(Yahoo symbol, name) of the index the book is measured against."""
    if config.get("benchmark_ticker"):
        return config["benchmark_ticker"], config["benchmark_ticker"]
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
) -> str:
    """The risk figures for ``portfolio`` as of ``trade_date``, and a sizing reference for ``ticker``.

    ``prices(symbol)`` returns daily OHLC rows up to the run date (raising when
    it has none); ``sector_of(symbol)`` a sector or None.
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

    holdings = [(normalize_symbol(p.ticker), p) for p in portfolio.positions]
    # Holdings are priced in their exchange's currency, cash in the book's label:
    # more than one currency between them and no total can be summed.
    label = (portfolio.currency or "").strip().upper() or None
    found = {(p.price_currency(label) or "").upper() for _, p in holdings} - {""}
    if label and portfolio.cash is not None:
        found.add(label)
    mixed = len(found) > 1
    analyzed_exchange = exchange_for_symbol(analyzed)
    currency = next(iter(found)) if len(found) == 1 else (
        None if mixed else (analyzed_exchange.currency if analyzed_exchange else label))

    def money(value: float | None) -> str:
        return format_money(value, currency)

    benchmark, benchmark_name = _benchmark_for(analyzed, config)
    bench_frame = frame_of(benchmark)
    bench = _closes(bench_frame) if bench_frame is not None else None

    lines = [f"Portfolio risk as of {trade_date} (computed from Yahoo Finance daily closes; "
             f"figures marked n/a could not be computed; amounts in {currency or 'the book currency'}):"]

    values, sectors, unpriced = {}, {}, []
    position_lines = []
    for symbol, position in holdings:
        frame = frame_of(symbol)
        if frame is None:
            unpriced.append(symbol)
            continue
        closes = _closes(frame)
        if closes.empty:
            unpriced.append(symbol)
            continue
        last = float(closes.iloc[-1])
        value = position.quantity * last
        values[symbol] = value
        sectors[symbol] = position.sector or sector_of(symbol)
        year = closes.iloc[-(ONE_YEAR + 1):]
        drawdown = risk.max_drawdown(year)
        details = [
            f"{position.quantity:,.4g} units at last close {format_money(last, position.price_currency(currency))} "
            f"({closes.index[-1].date()}) = {money(value)}",
            f"1Y volatility {format_percent(risk.annualized_volatility(year), signed=False)}",
            f"1Y max drawdown {format_percent(drawdown.depth if drawdown else None)}",
        ]
        if bench is not None:
            beta = risk.beta(year, bench.iloc[-(ONE_YEAR + 1):])
            details.append(f"beta to {benchmark_name} {'n/a' if beta is None else f'{beta:.2f}'}")
            gap = risk.relative_return(closes, bench, ONE_YEAR)
            details.append(f"1Y return vs {benchmark_name} {'n/a' if gap is None else f'{gap * 100:+.1f} pts'}")
        if position.average_price is not None and position.average_price > 0:
            pnl = (last - position.average_price) * position.quantity
            details.append(f"unrealized P/L {money(pnl)} "
                           f"({format_percent(last / position.average_price - 1)})")
            details.append(_tax_line(position, pnl, trade_date, config))
        position_lines.append(f"- {symbol}: " + "; ".join(d for d in details if d))

    invested = sum(v for v in values.values() if v > 0)
    cash = portfolio.cash
    if mixed:
        lines.append("- The book holds several currencies, so totals, weights and concentration are not computed.")
    else:
        total = invested + (cash or 0)
        lines.append(f"- Holdings {money(invested)}; cash {money(cash) if cash is not None else 'not given'}; "
                     f"total {money(total)}" + (f" (cash {format_percent(cash / total, signed=False)})"
                                                 if cash is not None and total else ""))
    lines += position_lines
    if unpriced:
        lines.append(f"- No prices up to {trade_date} for: {', '.join(unpriced)} (left out of the figures).")

    if not mixed and values:
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
        lines += _sizing_lines(analyzed, frame_of(analyzed), invested + (cash or 0), currency, config)
    return "\n".join(lines)


def _listing_currency(symbol: str) -> str | None:
    """The currency a listing trades in, where known: its exchange's, or USD for a plain US listing."""
    exchange = exchange_for_symbol(symbol)
    if exchange is not None:
        return exchange.currency
    return "USD" if is_us_listing(symbol) else None


def book_value(portfolio: PortfolioContext, ticker: str, prices: Callable[[str], pd.DataFrame]) -> float | None:
    """The book's value to size a paper trade in ``ticker`` against: its long holdings
    at their last close plus cash, as the risk figures total them.

    None when it cannot stand for capital in the listing's currency: nothing to
    value, a holding without prices, or a holding or the cash in another currency
    than the listing's (a holding off NSE/BSE and the US, and the cash, are in the
    book's label; unlabelled cash counts as the listing's). A listing whose
    currency is not known here gets None.
    """
    currency = _listing_currency(normalize_symbol(ticker))
    if currency is None:
        return None
    label = (portfolio.currency or "").strip().upper() or None
    total = 0.0
    for position in portfolio.positions:
        symbol = normalize_symbol(position.ticker)
        if (_listing_currency(symbol) or label) != currency:
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


def _tax_line(position, pnl: float, trade_date: str, config: dict) -> str:
    rules = costs.get_capital_gains_rules(config.get("capital_gains_rules"), config.get("capital_gains_overrides"))
    if rules is None or exchange_for_symbol(normalize_symbol(position.ticker)) is None:
        return ""
    if not position.acquired:
        return "tax on a sale depends on the holding period (purchase date not given)"
    days = (date.fromisoformat(trade_date) - date.fromisoformat(position.acquired)).days
    tax = costs.capital_gains_tax(rules, pnl, days)
    term = "long-term" if days > rules.long_term_after_months * 365 / 12 else "short-term"
    return (f"held {days} days ({term}); estimated tax if sold now {format_money(tax, 'INR')} "
            f"under the '{rules.name}' example rules (as of {rules.as_of}; before surcharge, cess and any "
            f"exemption already used this year)")


def _sizing_lines(symbol: str, frame, capital: float, currency: str | None, config: dict) -> list[str]:
    if frame is None or not capital or capital <= 0:
        return ["- Sizing reference: n/a (no prices for the instrument, or no capital to size against)."]
    closes = _closes(frame)
    entry = float(closes.iloc[-1])
    atr = risk.average_true_range(frame)
    risk_pct = float(config.get("risk_per_trade_pct") or 1.0)
    max_pct = float(config.get("max_position_pct") or 20.0)
    if atr is None:
        return [f"- Sizing reference: n/a (too little history for an ATR stop on {symbol})."]
    stop = entry - 2 * atr
    size = risk.position_size(capital, risk_pct / 100, entry, stop, max_pct / 100)
    if size is None or size.quantity <= 0:
        return [f"- Sizing reference: n/a (the risk budget buys no whole share of {symbol} at {format_money(entry, currency)})."]
    lines = [
        f"- Sizing reference for {symbol} (a rule of thumb, not a recommendation): risking {risk_pct:g}% of the "
        f"book ({format_money(capital * risk_pct / 100, currency)}) with a stop 2 x ATR(14) below the last close "
        f"{format_money(entry, currency)} (stop {format_money(stop, currency)}) allows about {size.quantity:,} "
        f"shares, {format_money(size.position_value, currency)}"
        + (f", held to the {max_pct:g}% maximum position" if size.capped_by else "") + "."
    ]
    listing = exchange_for_symbol(symbol)
    if listing is not None:
        profile = costs.get_cost_profile(config.get("cost_profile"), config.get("cost_overrides"))
        if profile.name != "none":
            buy = costs.order_costs(profile, "buy", size.position_value, listing.code)
            trip = costs.round_trip_costs(profile, size.position_value, venue=listing.code)
            lines.append(
                f"- Estimated charges on that trade under the '{profile.name}' example profile (rates as of "
                f"{profile.as_of}; verify with your broker): buying {format_money(buy.total, 'INR')} "
                f"({format_percent(buy.total / size.position_value, signed=False, decimals=3)}), a round trip at "
                f"the same price {format_money(trip, 'INR')} ({format_percent(trip / size.position_value, signed=False, decimals=3)}). "
                "Capital-gains tax is extra."
            )
    return lines
