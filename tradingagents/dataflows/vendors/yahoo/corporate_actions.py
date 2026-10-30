"""Dividends, splits and the next scheduled company events, from Yahoo Finance.

Past actions are served up to the run date. Yahoo's upcoming-events calendar
(next results date, ex-dividend date) is a present-day snapshot, so a run dated
in the past has it withheld rather than learn a date it could not have known.
Yahoo keeps an Indian company's actions under its NSE listing; a BSE listing
answers with none, which is said rather than read as "no dividends".
"""

import pandas as pd
import yfinance as yf

from tradingagents.dataflows.date_window import get_current_date, is_historical
from tradingagents.dataflows.errors import VendorError
from tradingagents.dataflows.symbols import normalize_symbol
from tradingagents.dataflows.vendors.yahoo.common import price_text, yf_retry
from tradingagents.markets.exchanges import exchange_for_symbol

LOOKBACK_YEARS = 5


def _up_to(series, as_of: pd.Timestamp) -> pd.Series:
    """The series' entries dated from LOOKBACK_YEARS before ``as_of`` through ``as_of``."""
    if series is None or len(series) == 0:
        return pd.Series(dtype=float)
    dates = pd.to_datetime(series.index)
    naive = dates.tz_localize(None) if dates.tz is not None else dates
    keep = (naive <= as_of) & (naive > as_of - pd.DateOffset(years=LOOKBACK_YEARS))
    return pd.Series(series.to_numpy()[keep], index=naive[keep].date)


def _upcoming(canonical: str, as_of: pd.Timestamp) -> list[str]:
    try:
        calendar = yf_retry(lambda: yf.Ticker(canonical).calendar) or {}
    except VendorError as exc:
        return [f"- Upcoming events: unavailable ({exc})"]
    lines = []
    earnings = calendar.get("Earnings Date") or []
    earnings = [d for d in (earnings if isinstance(earnings, list) else [earnings]) if d]
    if earnings:
        lines.append("- Next results date (Yahoo estimate or announcement): "
                     + ", ".join(str(d) for d in earnings))
    ex_dividend = calendar.get("Ex-Dividend Date")
    if ex_dividend:
        label = "Next ex-dividend date" if pd.Timestamp(ex_dividend) > as_of else "Latest ex-dividend date"
        lines.append(f"- {label}: {ex_dividend}")
    return lines or ["- Upcoming events: none listed by Yahoo"]


def get_corporate_actions(symbol: str, as_of_date: str | None = None) -> str:
    """Dividends and splits over the last five years up to ``as_of_date``, then upcoming events."""
    as_of_date = as_of_date or get_current_date()
    canonical = normalize_symbol(symbol)
    as_of = pd.Timestamp(as_of_date)
    exchange = exchange_for_symbol(canonical)
    currency = exchange.currency if exchange else "the quote currency"

    dividends = _up_to(yf_retry(lambda: yf.Ticker(canonical).dividends), as_of)
    splits = _up_to(yf_retry(lambda: yf.Ticker(canonical).splits), as_of)

    lines = [f"# Corporate actions for {canonical} up to {as_of_date}",
             f"Source: Yahoo Finance (ex-dates; dividends per share in {currency}). Window: "
             f"{LOOKBACK_YEARS} years to the run date.", ""]
    if len(dividends):
        lines.append("Dividends (ex-date: amount per share):")
        lines += [f"- {day}: {price_text(float(amount))}" for day, amount in dividends.items()]
        yearly = dividends.groupby([d.year for d in dividends.index]).sum()
        lines.append("Total per calendar year: " + ", ".join(
            f"{year} {price_text(float(total))}" for year, total in yearly.items()))
    else:
        lines.append("Dividends: none recorded by Yahoo in the window.")
    if len(splits):
        lines.append("Splits and bonus issues (ex-date: ratio, new shares per old):")
        lines += [f"- {day}: {float(ratio):g}-for-1" for day, ratio in splits.items()]
    else:
        lines.append("Splits / bonus issues: none recorded by Yahoo in the window.")
    if not len(dividends) and not len(splits) and canonical.endswith(".BO"):
        lines.append(f"Yahoo records an Indian company's actions under its NSE listing; "
                     f"{canonical[:-3]}.NS may have them.")

    lines.append("")
    if is_historical(as_of_date, canonical):
        lines.append(f"- Upcoming events: withheld. Yahoo's event calendar is a present-day snapshot, "
                     f"so it would put dates announced after {as_of_date} into this analysis.")
    else:
        lines += _upcoming(canonical, as_of)
    return "\n".join(lines)
