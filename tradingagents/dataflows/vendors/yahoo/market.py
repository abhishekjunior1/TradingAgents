import logging
from datetime import date, datetime, timedelta
from typing import Annotated

import pandas as pd
import yfinance as yf
from dateutil.relativedelta import relativedelta
from stockstats import wrap

from tradingagents.dataflows.errors import NoMarketDataError, VendorError
from tradingagents.dataflows.symbols import normalize_symbol
from tradingagents.dataflows.vendors.yahoo.common import price_text, raise_for_empty, yf_retry
from tradingagents.dataflows.vendors.yahoo.ohlcv import _assert_ohlcv_not_stale, load_ohlcv
from tradingagents.markets.exchanges import (
    exchange_for_symbol,
    holiday_name,
    is_trading_day,
    session_status,
)

logger = logging.getLogger(__name__)

# Sessions of history each indicator needs before its value means anything (stockstats'
# default windows). With fewer, stockstats still returns a number: an RSI of 50 from one bar.
_MIN_SESSIONS = {
    "close_10_ema": 10, "close_50_sma": 50, "close_200_sma": 200,
    "macd": 26, "macds": 34, "macdh": 34, "rsi": 15,
    "boll": 20, "boll_ub": 20, "boll_lb": 20, "atr": 15, "vwma": 14, "mfi": 15,
}
_TRADING = ("pre-open", "open", "closing")


def _session_now(canonical: str):
    """The listing's exchange and where its trading day stands now; (None, None) for a
    listing without an exchange calendar here (only NSE and BSE have one)."""
    exchange = exchange_for_symbol(canonical)
    return (exchange, session_status(exchange)) if exchange is not None else (None, None)


def _forming(status, day: date) -> bool:
    """Whether ``day``'s bar is a session still trading: its Close is the latest price, not a close."""
    return status is not None and status.state in _TRADING and day == status.local_time.date()


def _short_history(indicator: str, rows: int) -> str | None:
    """Why ``indicator`` has no value from ``rows`` sessions of data, or None when they are enough."""
    need = _MIN_SESSIONS.get(indicator, 1)
    if rows >= need:
        return None
    return f"N/A: not enough price history ({indicator} needs {need} sessions; the data has {rows} through this date)"


def _no_value(day: date, exchange, status) -> str:
    """Why ``day`` has no row: not a trading day, today's bar not out yet, or a gap in the vendor's data."""
    if exchange is None:
        return ("N/A: Not a trading day (weekend)" if day.weekday() >= 5
                else "N/A: no data for this date (a market holiday or a gap in the vendor's data)")
    if day.year not in exchange.holiday_years and day.weekday() < 5 and day not in exchange.special_sessions:
        return (f"N/A: no data for this date (a holiday or a gap in the vendor's data; the {exchange.code} "
                f"holiday list here does not cover {day.year})")
    if not is_trading_day(exchange, day):
        return f"N/A: Not a trading day ({holiday_name(exchange, day) or 'weekend'})"
    if day >= status.local_time.date():
        return f"N/A: no bar yet for today's {exchange.code} session"
    return f"N/A: no price data from the vendor for this {exchange.code} trading day"


def _range_notes(canonical: str, days: set[date], start_date: str, end_date: str) -> list[str]:
    """Notes on an NSE/BSE listing's daily rows: a row of a session still trading, and
    trading days the vendor returned no row for."""
    exchange, status = _session_now(canonical)
    if exchange is None or not days:
        return []
    notes = []
    newest = max(days)
    if _forming(status, newest):
        notes.append(f"the {newest:%Y-%m-%d} row is {exchange.code}'s session still trading at "
                     f"{status.local_time:%H:%M} {exchange.tz_label}: its Close is the latest price, not a close "
                     f"(final after {exchange.close_final:%H:%M} {exchange.tz_label})")
    first = date.fromisoformat(start_date)
    last = min(date.fromisoformat(end_date), status.last_complete_session)
    expected = [day for day in (first + timedelta(days=n) for n in range((last - first).days + 1))
                if day.year in exchange.holiday_years and is_trading_day(exchange, day)]
    missing = [day for day in expected if day not in days]
    if missing:
        shown = ", ".join(f"{day:%d %b %Y}" for day in missing[:5]) + (", ..." if len(missing) > 5 else "")
        notes.append(f"no row for {len(missing)} of the {len(expected)} {exchange.code} trading days in this range "
                     f"({shown}): they are missing from Yahoo's data for this listing (or it had not listed yet), "
                     f"not holidays")
    return notes


def get_YFin_data_online(
    symbol: Annotated[str, "ticker symbol of the company"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
):

    datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")

    # Resolve broker/forex symbols to Yahoo's convention (XAUUSD+ -> GC=F).
    canonical = normalize_symbol(symbol)

    # yfinance treats ``end`` as EXCLUSIVE, so it would drop the requested
    # end_date row (and the current day when end_date is today). Request one day
    # past end_date so the requested range is actually inclusive (#986/#987).
    end_inclusive = (end_dt + relativedelta(days=1)).strftime("%Y-%m-%d")
    data = yf_retry(lambda: yf.Ticker(canonical).history(start=start_date, end=end_inclusive))

    # Empty result means the symbol is unknown/delisted. Raise a typed error
    # instead of returning prose: the routing layer turns it into a single
    # unambiguous "no data" signal so the agent never fabricates a price.
    if data is None or data.empty:
        raise_for_empty(symbol, canonical, f"rows between {start_date} and {end_date}")

    # Remove timezone info from index for cleaner output
    if data.index.tz is not None:
        data.index = data.index.tz_localize(None)

    # Reject a stale frame (e.g. a year-old partial response) before it is
    # formatted into the report. Raises NoMarketDataError, which the router
    # turns into one clear unavailable signal (#1021).
    _assert_ohlcv_not_stale(data, end_date, symbol, canonical)

    for col in ("Open", "High", "Low", "Close", "Adj Close"):
        if col in data.columns:
            data[col] = data[col].map(price_text, na_action="ignore")

    csv_string = data.to_csv()

    # Name the resolved symbol when it differs, so the reader sees which
    # instrument was priced.
    label = canonical if canonical == symbol.upper() else f"{canonical} (from {symbol})"
    header = f"# Stock data for {label} from {start_date} to {end_date}\n"
    header += f"# Total records: {len(data)}\n"
    days = {pd.Timestamp(stamp).date() for stamp in data.index}
    header += "".join(f"# Note: {note}\n" for note in _range_notes(canonical, days, start_date, end_date))

    return header + "\n" + csv_string


def get_stock_stats_indicators_window(
    symbol: Annotated[str, "ticker symbol of the company"],
    indicator: Annotated[str, "technical indicator to get the analysis and report of"],
    as_of_date: Annotated[
        str, "The current trading date you are trading on, YYYY-mm-dd"
    ],
    look_back_days: Annotated[int, "how many days to look back"],
) -> str:

    best_ind_params = {
        # Moving Averages
        "close_50_sma": (
            "50 SMA: A medium-term trend indicator. "
            "Usage: Identify trend direction and serve as dynamic support/resistance. "
            "Tips: It lags price; combine with faster indicators for timely signals."
        ),
        "close_200_sma": (
            "200 SMA: A long-term trend benchmark. "
            "Usage: Confirm overall market trend and identify golden/death cross setups. "
            "Tips: It reacts slowly; best for strategic trend confirmation rather than frequent trading entries."
        ),
        "close_10_ema": (
            "10 EMA: A responsive short-term average. "
            "Usage: Capture quick shifts in momentum and potential entry points. "
            "Tips: Prone to noise in choppy markets; use alongside longer averages for filtering false signals."
        ),
        # MACD Related
        "macd": (
            "MACD: Computes momentum via differences of EMAs. "
            "Usage: Look for crossovers and divergence as signals of trend changes. "
            "Tips: Confirm with other indicators in low-volatility or sideways markets."
        ),
        "macds": (
            "MACD Signal: An EMA smoothing of the MACD line. "
            "Usage: Use crossovers with the MACD line to trigger trades. "
            "Tips: Should be part of a broader strategy to avoid false positives."
        ),
        "macdh": (
            "MACD Histogram: Shows the gap between the MACD line and its signal. "
            "Usage: Visualize momentum strength and spot divergence early. "
            "Tips: Can be volatile; complement with additional filters in fast-moving markets."
        ),
        # Momentum Indicators
        "rsi": (
            "RSI: Measures momentum to flag overbought/oversold conditions. "
            "Usage: Apply 70/30 thresholds and watch for divergence to signal reversals. "
            "Tips: In strong trends, RSI may remain extreme; always cross-check with trend analysis."
        ),
        # Volatility Indicators
        "boll": (
            "Bollinger Middle: A 20 SMA serving as the basis for Bollinger Bands. "
            "Usage: Acts as a dynamic benchmark for price movement. "
            "Tips: Combine with the upper and lower bands to effectively spot breakouts or reversals."
        ),
        "boll_ub": (
            "Bollinger Upper Band: Typically 2 standard deviations above the middle line. "
            "Usage: Signals potential overbought conditions and breakout zones. "
            "Tips: Confirm signals with other tools; prices may ride the band in strong trends."
        ),
        "boll_lb": (
            "Bollinger Lower Band: Typically 2 standard deviations below the middle line. "
            "Usage: Indicates potential oversold conditions. "
            "Tips: Use additional analysis to avoid false reversal signals."
        ),
        "atr": (
            "ATR: Averages true range to measure volatility. "
            "Usage: Set stop-loss levels and adjust position sizes based on current market volatility. "
            "Tips: It's a reactive measure, so use it as part of a broader risk management strategy."
        ),
        # Volume-Based Indicators
        "vwma": (
            "VWMA: A moving average weighted by volume. "
            "Usage: Confirm trends by integrating price action with volume data. "
            "Tips: Watch for skewed results from volume spikes; use in combination with other volume analyses."
        ),
        "mfi": (
            "MFI: The Money Flow Index is a momentum indicator that uses both price and volume to measure buying and selling pressure. "
            "Usage: Identify overbought (>80) or oversold (<20) conditions and confirm the strength of trends or reversals. "
            "Tips: Use alongside RSI or MACD to confirm signals; divergence between price and MFI can indicate potential reversals."
        ),
    }

    if indicator not in best_ind_params:
        raise ValueError(
            f"Indicator {indicator} is not supported. Please choose from: {list(best_ind_params.keys())}"
        )

    end_date = as_of_date
    as_of_dt = datetime.strptime(as_of_date, "%Y-%m-%d")
    before = as_of_dt - relativedelta(days=look_back_days)

    # Optimized: Get stock data once and calculate indicators for all dates
    try:
        indicator_data = _get_stock_stats_bulk(symbol, indicator, as_of_date)
        exchange, status = _session_now(normalize_symbol(symbol))

        # Generate the date range we need
        current_dt = as_of_dt
        date_values = []

        while current_dt >= before:
            date_str = current_dt.strftime('%Y-%m-%d')

            # Look up the indicator value for this date
            if date_str in indicator_data:
                indicator_value = indicator_data[date_str]
                if _forming(status, current_dt.date()) and not indicator_value.startswith("N/A"):
                    indicator_value += (f" (provisional: {exchange.code} is still trading at "
                                        f"{status.local_time:%H:%M} {exchange.tz_label}; today's bar is not a close)")
            else:
                indicator_value = _no_value(current_dt.date(), exchange, status)

            date_values.append((date_str, indicator_value))
            current_dt = current_dt - relativedelta(days=1)

        ind_string = ""
        for date_str, value in date_values:
            ind_string += f"{date_str}: {value}\n"

    except VendorError:
        raise  # Unknown/delisted symbol — let the router emit the sentinel
    except Exception as e:
        logger.warning("Bulk stockstats fetch failed, falling back per-day: %s", e)
        # Fallback to original implementation if bulk method fails
        ind_string = ""
        as_of_dt = datetime.strptime(as_of_date, "%Y-%m-%d")
        while as_of_dt >= before:
            indicator_value = get_stockstats_indicator(
                symbol, indicator, as_of_dt.strftime("%Y-%m-%d")
            )
            ind_string += f"{as_of_dt.strftime('%Y-%m-%d')}: {indicator_value}\n"
            as_of_dt = as_of_dt - relativedelta(days=1)

    result_str = (
        f"## {indicator} values from {before.strftime('%Y-%m-%d')} to {end_date}:\n\n"
        + ind_string
        + "\n\n"
        + best_ind_params.get(indicator, "No description available.")
    )

    return result_str


def _get_stock_stats_bulk(
    symbol: Annotated[str, "ticker symbol of the company"],
    indicator: Annotated[str, "technical indicator to calculate"],
    as_of_date: Annotated[str, "current date for reference"]
) -> dict:
    """
    Optimized bulk calculation of stock stats indicators.
    Fetches data once and calculates indicator for all available dates.
    Returns dict mapping date strings to indicator values.
    """
    from stockstats import wrap

    data = load_ohlcv(symbol, as_of_date)
    df = wrap(data.sort_values("Date"))
    df["Date"] = df["Date"].dt.strftime("%Y-%m-%d")

    df[indicator]  # This triggers stockstats to calculate the indicator

    result_dict = {}
    for rows, (_, row) in enumerate(df.iterrows(), start=1):
        date_str = row["Date"]
        indicator_value = row[indicator]

        short = _short_history(indicator, rows)
        if short:
            result_dict[date_str] = short
        elif pd.isna(indicator_value):
            result_dict[date_str] = "N/A"
        else:
            result_dict[date_str] = str(indicator_value)

    return result_dict


def get_stockstats_indicator(
    symbol: Annotated[str, "ticker symbol of the company"],
    indicator: Annotated[str, "technical indicator to get the analysis and report of"],
    as_of_date: Annotated[
        str, "The current trading date you are trading on, YYYY-mm-dd"
    ],
) -> str:

    as_of_dt = datetime.strptime(as_of_date, "%Y-%m-%d")
    as_of_date = as_of_dt.strftime("%Y-%m-%d")

    try:
        indicator_value = get_stock_stats(
            symbol,
            indicator,
            as_of_date,
        )
    except VendorError:
        raise  # Unknown/delisted symbol — let the router emit the sentinel
    except Exception as e:
        # An empty string renders as "2026-05-08: " in the indicator table, which
        # reads as no value that day rather than a read that failed. Raise so the
        # router can try the next vendor or report the series unavailable.
        raise NoMarketDataError(
            symbol, symbol, f"{indicator} could not be read for {as_of_date}: {e}"
        ) from e

    return str(indicator_value)


def get_closes(symbol: str, start_date: str, end_date: str) -> pd.Series:
    """Daily closes from ``start_date`` up to, not including, ``end_date``."""
    canonical = normalize_symbol(symbol)
    history = yf_retry(lambda: yf.Ticker(canonical).history(start=start_date, end=end_date))
    return history["Close"] if history is not None and "Close" in history else pd.Series(dtype=float)


def get_stock_stats(
    symbol: Annotated[str, "ticker symbol for the company"],
    indicator: Annotated[
        str, "quantitative indicators based off of the stock data for the company"
    ],
    as_of_date: Annotated[
        str, "curr date for retrieving stock price data, YYYY-mm-dd"
    ],
):
    data = load_ohlcv(symbol, as_of_date)
    df = wrap(data)
    df["Date"] = df["Date"].dt.strftime("%Y-%m-%d")
    as_of_str = pd.to_datetime(as_of_date).strftime("%Y-%m-%d")

    df[indicator]  # trigger stockstats to calculate the indicator
    matching_rows = df[df["Date"].str.startswith(as_of_str)]

    if not matching_rows.empty:
        short = _short_history(indicator, int((df["Date"] <= as_of_str).sum()))
        return short if short else matching_rows[indicator].values[0]
    exchange, status = _session_now(normalize_symbol(symbol))
    return _no_value(pd.to_datetime(as_of_date).date(), exchange, status)
