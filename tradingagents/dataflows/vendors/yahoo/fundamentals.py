import functools
import logging
import re
from itertools import pairwise
from typing import Annotated

import pandas as pd
import yfinance as yf

from tradingagents.dataflows.date_window import (
    get_current_date,
    withhold_live_profile,
    withhold_undated_statements,
    withhold_undisclosed_trades,
)
from tradingagents.dataflows.errors import NoMarketDataError, VendorError, VendorUnavailableError
from tradingagents.dataflows.net import vendor_reachable
from tradingagents.dataflows.symbols import normalize_symbol
from tradingagents.dataflows.vendors.yahoo.common import (
    YAHOO_HOST,
    price_text,
    raise_for_empty,
    yf_retry,
)
from tradingagents.dataflows.vendors.yahoo.ohlcv import HISTORY_BEFORE_AS_OF_DAYS, load_ohlcv
from tradingagents.markets.exchanges import exchange_for_symbol

logger = logging.getLogger(__name__)

# Quote currencies Yahoo gives in a minor unit, and the main unit of each.
_MAIN_UNIT = {"GBp": "GBP", "GBX": "GBP", "ZAc": "ZAR", "ILA": "ILS"}


def get_fundamentals(
    ticker: Annotated[str, "ticker symbol of the company"],
    as_of_date: Annotated[str, "analysis date in YYYY-MM-DD format"] = None
):
    """Get company fundamentals overview from yfinance.

    ``Ticker.info`` is a present-day snapshot with no historical vintage, so a
    past ``as_of_date`` withholds it through the shared point-in-time guard
    (``date_window.withhold_live_profile``, #1300).
    """
    canonical = normalize_symbol(ticker)

    # Guard before the request: the response would only be discarded, and the
    # answer does not depend on it.
    withheld = withhold_live_profile(as_of_date, canonical)
    if withheld:
        return withheld

    info = yf_retry(lambda: yf.Ticker(canonical).info)
    if not info:
        raise_for_empty(ticker, canonical, "fundamentals")

    # Yahoo gives these two in percent (dividendYield 0.41 is 0.41%; debtToEquity
    # 78.4 is 78.4%, a ratio of 0.78) but the margins and returns as fractions,
    # so each carries its unit.
    dividend_yield, debt_to_equity = info.get("dividendYield"), info.get("debtToEquity")
    # Money figures come in three currencies (#1456): price levels in the quote
    # currency, which can be a minor unit (London in pence, GBp); market cap and
    # per-share figures in that currency's main unit (GBP), as Yahoo's own P/E
    # and P/B are computed against them; income figures in the currency the
    # company reports in (an ADR quotes in USD, reports in TWD), or bare when
    # Yahoo does not say which.
    quote = info.get("currency")
    main = _MAIN_UNIT.get(quote, quote)
    reported = info.get("financialCurrency")

    def money(key, currency):
        value = info.get(key)
        return None if value is None else f"{value} {currency}" if currency else value

    fields = [
        ("Name", info.get("longName")),
        ("Sector", info.get("sector")),
        ("Industry", info.get("industry")),
        ("Market Cap", money("marketCap", main)),
        ("PE Ratio (TTM)", info.get("trailingPE")),
        ("Forward PE", info.get("forwardPE")),
        ("PEG Ratio", info.get("pegRatio")),
        ("Price to Book", info.get("priceToBook")),
        ("EPS (TTM)", money("trailingEps", main)),
        ("Forward EPS", money("forwardEps", main)),
        ("Dividend Yield", None if dividend_yield is None else f"{dividend_yield}%"),
        ("Beta", info.get("beta")),
        ("52 Week High", money("fiftyTwoWeekHigh", quote)),
        ("52 Week Low", money("fiftyTwoWeekLow", quote)),
        ("50 Day Average", money("fiftyDayAverage", quote)),
        ("200 Day Average", money("twoHundredDayAverage", quote)),
        ("Revenue (TTM)", money("totalRevenue", reported)),
        ("Gross Profit", money("grossProfits", reported)),
        ("EBITDA", money("ebitda", reported)),
        ("Net Income", money("netIncomeToCommon", reported)),
        ("Profit Margin", info.get("profitMargins")),
        ("Operating Margin", info.get("operatingMargins")),
        ("Return on Equity", info.get("returnOnEquity")),
        ("Return on Assets", info.get("returnOnAssets")),
        ("Debt to Equity", None if debt_to_equity is None
         else f"{debt_to_equity}% ({debt_to_equity / 100:.2f}x)"),
        ("Current Ratio", info.get("currentRatio")),
        ("Book Value", money("bookValue", main)),
        ("Free Cash Flow", money("freeCashflow", reported)),
    ]

    lines = [f"{label}: {v}" for label, v in fields if v is not None]

    # yfinance returns a stub dict (e.g. {"trailingPegRatio": None}) for
    # unknown symbols, so `info` is truthy but every field is empty. Treat
    # "no usable fields" as no data rather than emitting a bare header the
    # agent might fabricate around.
    if not lines:
        raise_for_empty(ticker, canonical, "fundamental fields")

    return f"# Company Fundamentals for {canonical}\n\n" + "\n".join(lines)


def _statement(ticker, freq, as_of_date, title, quarterly_attr, annual_attr) -> str:
    """One financial statement as CSV, for a run dated today, headed by its source and currency."""
    canonical = normalize_symbol(ticker)
    withheld = withhold_undated_statements(as_of_date, canonical, title)
    if withheld:
        return withheld
    what = title.lower()
    quarterly = freq.lower() == "quarterly"
    attr = quarterly_attr if quarterly else annual_attr
    data = yf_retry(lambda: getattr(yf.Ticker(canonical), attr))
    if data is None or data.empty:
        raise_for_empty(ticker, canonical, f"{what} data")
    notes = _statement_currency(canonical) + (_quarter_gaps(data.columns) if quarterly else [])
    return f"# {title} data for {canonical} ({freq})\n" + "".join(f"# {note}\n" for note in notes) + data.to_csv()


def _statement_currency(canonical: str) -> list[str]:
    """Header lines naming a statement's source, currency and unit.

    Yahoo gives statements in the currency the company reports in, which need not
    be the one its listing quotes in: INFY.NS quotes in INR and reports in USD, so
    its Diluted EPS of 0.80 is dollars. The profile states both (as in
    ``get_fundamentals``); without it the currency is said to be unknown, never
    guessed from the quote, and the statement is still served.
    """
    failed = False
    try:
        reported, quote = _currencies(canonical, get_current_date())
    except VendorError as exc:
        logger.debug("No profile to read %s's reporting currency from: %s", canonical, exc)
        reported = quote = None
        failed = not isinstance(exc, NoMarketDataError)     # a rate limit or an outage, not Yahoo's answer
    exchange = exchange_for_symbol(canonical)
    quote = quote or (exchange.currency if exchange else None)
    unit = "full units (not thousands, millions or crores), except per-share figures such as EPS"
    if not reported:
        lines = [f"Source: Yahoo Finance. The currency of these money amounts, which are in {unit}, could not be "
                 f"retrieved: Yahoo's company profile did not load." if failed else
                 f"Source: Yahoo Finance. Yahoo does not state the currency of these money amounts, "
                 f"which are in {unit}."]
        if quote:
            lines.append(f"Note: do not assume {quote}, the currency {canonical} quotes in; "
                         f"a company can report in another currency.")
        return lines
    lines = [f"Source: Yahoo Finance. Money amounts are in {reported}, the currency the company reports in, "
             f"as {unit}."]
    if quote and quote != reported:
        lines.append(f"Note: {canonical} quotes in {quote}, so convert these {reported} amounts, per-share ones "
                     f"included, before comparing them with its {quote} prices or other {quote} figures.")
    return lines


@functools.lru_cache(maxsize=256)
def _currencies(canonical: str, day: str) -> tuple[str | None, str | None]:
    """The reporting and quote currencies in ``canonical``'s Yahoo profile, read once a ``day``.

    A profile costs three Yahoo requests, which each of a listing's statements would
    otherwise repeat; ``day`` keys the cache, so a new day reads it afresh. A failed
    read raises, so it is not kept and the next statement asks again. It is not
    retried either: a header line must not hold up a statement that has arrived.
    """
    info = yf_retry(lambda: yf.Ticker(canonical).info, max_retries=0)
    if not isinstance(info, dict):
        raise NoMarketDataError(canonical, canonical, "no company profile")
    return info.get("financialCurrency"), info.get("currency")


def _months_text(days: int) -> str:
    """A gap of ``days`` in whole months, or in days when it is under two months."""
    months = round(days / 30.44)
    return f"{months} months" if months > 1 else f"{days} days"


def _quarter_gaps(columns) -> list[str]:
    """A note when a quarterly statement's columns are not consecutive quarters, else nothing.

    Indian companies publish a balance sheet only with half-year and annual results
    (SEBI LODR), and Yahoo's quarterly data can skip a quarter, so neighbouring
    columns can be six months apart. Quarters of 13 or 14 weeks (52/53-week years)
    round to three months and are not flagged.
    """
    ends = pd.DatetimeIndex(pd.to_datetime(pd.Index(columns), errors="coerce")).dropna().unique().sort_values()
    gaps = [(a, b) for a, b in pairwise(ends) if round((b - a).days / 30.44) != 3]
    if not gaps:
        return []
    shown = ", ".join(f"{a:%Y-%m-%d} to {b:%Y-%m-%d} ({_months_text((b - a).days)})" for a, b in gaps)
    return [f"Note: these quarterly columns are not consecutive quarters: {shown}. A change across such a gap "
            f'is not a quarter-on-quarter change; annual statements (freq="annual") cover each full year.']


def get_balance_sheet(
    ticker: Annotated[str, "ticker symbol of the company"],
    freq: Annotated[str, "frequency of data: 'annual' or 'quarterly'"] = "quarterly",
    as_of_date: Annotated[str, "current date in YYYY-MM-DD format"] = None
):
    """Get balance sheet data from yfinance."""
    return _statement(ticker, freq, as_of_date, "Balance Sheet", "quarterly_balance_sheet", "balance_sheet")


def get_cashflow(
    ticker: Annotated[str, "ticker symbol of the company"],
    freq: Annotated[str, "frequency of data: 'annual' or 'quarterly'"] = "quarterly",
    as_of_date: Annotated[str, "current date in YYYY-MM-DD format"] = None
):
    """Get cash flow data from yfinance."""
    return _statement(ticker, freq, as_of_date, "Cash Flow", "quarterly_cashflow", "cashflow")


def get_income_statement(
    ticker: Annotated[str, "ticker symbol of the company"],
    freq: Annotated[str, "frequency of data: 'annual' or 'quarterly'"] = "quarterly",
    as_of_date: Annotated[str, "current date in YYYY-MM-DD format"] = None
):
    """Get income statement data from yfinance."""
    return _statement(ticker, freq, as_of_date, "Income Statement", "quarterly_income_stmt", "income_stmt")


def get_insider_transactions(
    ticker: Annotated[str, "ticker symbol of the company"],
    as_of_date: Annotated[str | None, "analysis date, yyyy-mm-dd"] = None,
):
    """Get insider transactions data from yfinance, for a run dated today."""
    canonical = normalize_symbol(ticker)
    withheld = withhold_undisclosed_trades(as_of_date, canonical)
    if withheld:
        return withheld
    data = yf_retry(lambda: yf.Ticker(canonical).insider_transactions)

    # Empty is normal here (many valid symbols have no insider filings),
    # so report it plainly rather than treating the symbol as invalid.
    if data is None or data.empty:
        if not vendor_reachable(YAHOO_HOST):
            raise VendorUnavailableError("Yahoo Finance is unreachable; insider filings were not retrieved")
        return f"No insider transactions reported for symbol '{canonical}'"

    note = _insider_price_note(canonical, data, as_of_date)
    return f"# Insider Transactions data for {canonical}\n" + data.to_csv() + note


_PRICE_IN_TEXT = re.compile(r"at price ([\d,]*\.?\d+) per share", re.IGNORECASE)


def _insider_price_note(canonical: str, data: pd.DataFrame, as_of_date: str | None) -> str:
    """A note when an NSE/BSE listing's insider prices do not fit its closes or cannot be checked, else "".

    Yahoo gives no currency for these rows, and for Indian listings their
    per-share prices have been far below the stock's INR closes. The prices are
    read from the Text column ("Sale at price 15.69 per share.") and the median
    is held to half the lowest and twice the highest close over the same dates.
    Yahoo has no daily history for some BSE listings (RELIANCE.BO), which are
    held to the NSE listing under the same symbol instead; rows with no closes
    to check them against still carry the currency caveat, which tells closes
    Yahoo does not have from closes that could not be retrieved.
    """
    exchange = exchange_for_symbol(canonical)
    if exchange is None or not {"Text", "Start Date"} <= set(data.columns):
        return ""
    prices = pd.to_numeric(
        data["Text"].astype(str).str.extract(_PRICE_IN_TEXT)[0].str.replace(",", ""), errors="coerce")
    dates = pd.to_datetime(data["Start Date"], errors="coerce")
    if not pd.api.types.is_datetime64_any_dtype(dates):
        return ""
    if dates.dt.tz is not None:
        dates = dates.dt.tz_localize(None)
    rows = pd.DataFrame({"date": dates.dt.normalize(), "price": prices}).dropna()
    rows = rows[rows["price"] > 0]
    if rows.empty:
        return ""
    currency = exchange.currency
    sources = [canonical] + ([canonical[:-3] + ".NS"] if canonical.upper().endswith(".BO") else [])
    found = {}
    for source in sources:
        closes = found[source] = _closes_over(source, rows["date"], as_of_date)
        if closes is not None and not closes.empty:
            break
    else:
        listings = " or ".join(sources)
        # A request that failed says nothing about whether there were closes.
        if any(read is None for read in found.values()):
            unchecked = f"no {currency} closes of {listings} over the same period could be retrieved"
        else:
            unchecked = f"there were no {currency} closes of {listings} over the same period"
        return (
            f"\nNote: Yahoo does not state the currency of these prices and values, and {unchecked} to "
            f"check them against, so do not assume they are rupee amounts. Indian insider trades are "
            f"disclosed on NSE/BSE under SEBI's insider-trading (PIT) regulations.\n"
        )
    low, high = closes.min(), closes.max()
    if 0.5 * low <= rows["price"].median() <= 2 * high:
        return ""
    if source == canonical:
        whose, missing = f"{canonical}'s {currency} closes", ""
    else:
        whose = f"the {currency} closes of {source}, the NSE listing under the same symbol,"
        missing = (f"; Yahoo has none for {canonical} then" if found[canonical] is not None
                   else f"; those of {canonical} could not be retrieved")
    return (
        f"\nNote: Yahoo does not state the currency of these prices and values. Their per-share "
        f"prices ({price_text(rows['price'].min())}-{price_text(rows['price'].max())}) do not match "
        f"{whose} over the same period ({price_text(low)}-{price_text(high)}{missing}), so "
        f"they are not rupee amounts and must not be compared with {currency} prices or read as "
        f"rupee values. Indian insider trades are disclosed on NSE/BSE under SEBI's insider-trading "
        f"(PIT) regulations.\n"
    )


def _closes_over(symbol: str, dates: pd.Series, as_of_date: str | None) -> pd.Series | None:
    """``symbol``'s closes from a week before the first of ``dates`` to the last: empty when
    Yahoo has none then, None when they could not be retrieved.

    Only closes from the trades' span are kept, so no staleness test applies: a history
    that stops or pauses before the last trade, as a suspended listing's does, still gives
    the closes it has then. They are read as of the last of ``dates``, or of the first date
    the shared 5-year download serves when that is later and the download holds the whole
    span, and never after the run date.
    """
    # A trade dated on a holiday is read against the sessions just before it.
    start = dates.min() - pd.Timedelta(days=7)
    run = pd.Timestamp(as_of_date or get_current_date())
    until = min(run, dates.max())
    # load_ohlcv reads a date from the shared 5-year download only when the year of history
    # before it lies within that download (which opens at this time of day, five years back);
    # an earlier date gets a download of its own.
    window = pd.Timestamp.today().normalize() - pd.DateOffset(years=5)
    if start >= window:
        until = min(run, max(until, window + pd.Timedelta(days=HISTORY_BEFORE_AS_OF_DAYS + 1)))
    try:
        bars = load_ohlcv(symbol, f"{until:%Y-%m-%d}", fill_gaps=False, max_stale_days=None)
    except NoMarketDataError as exc:
        logger.debug("No closes of %s to check insider prices against: %s", symbol, exc)
        return pd.Series(dtype=float)
    except Exception as exc:  # without closes the rows cannot be checked, which the note then says
        logger.debug("Could not retrieve closes of %s to check insider prices against: %s", symbol, exc)
        return None
    span = (bars["Date"] >= start) & (bars["Date"] <= dates.max())
    return bars.loc[span, "Close"].dropna()


def get_company_profile(ticker: str) -> dict:
    """Yahoo's current profile for ``ticker``: name, sector, industry and the like."""
    return yf_retry(lambda: yf.Ticker(normalize_symbol(ticker)).info) or {}
