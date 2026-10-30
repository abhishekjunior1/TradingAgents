"""Yahoo statements, insider rows and corporate actions say what their numbers are.

- A statement names its source, the currency the company reports in and its unit,
  and warns when that currency is not the one the listing quotes in (INFY.NS quotes
  in INR and reports to Yahoo in USD). "Quarterly" columns that are not consecutive
  quarters are flagged.
- Insider prices on a BSE listing Yahoo has no daily history for are checked against
  its NSE listing, and rows that cannot be checked still carry the currency caveat.
- A results date on or before the run date is not called the next one.
- Dividend amounts say they are at today's share count, as Yahoo gives them; a past
  run is told they are on the present-day share basis instead.

yf.Ticker and load_ohlcv are stubbed and the clock is fixed; no test reaches Yahoo.
"""

from datetime import date
from types import SimpleNamespace
from unittest import mock

import pandas as pd
import pytest

from tradingagents.dataflows import date_window
from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.vendors.yahoo import common, corporate_actions, fundamentals

TODAY = "2026-10-30"


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    """The machine's date is TODAY; the listing's own date is never earlier than it.
    Each test starts with no profile read (they are kept for the day)."""
    for module in (date_window, fundamentals, corporate_actions):
        monkeypatch.setattr(module, "get_current_date", lambda: TODAY)
    monkeypatch.setattr(date_window, "listing_date", lambda symbol, now=None: None)
    monkeypatch.setattr(common, "vendor_reachable", lambda url: True)
    fundamentals._currencies.cache_clear()
    yield
    fundamentals._currencies.cache_clear()


# --- statements: source, currency, unit -----------------------------------------------------

def _statement(*columns, rows=("Diluted EPS", "Total Revenue")):
    return pd.DataFrame({pd.Timestamp(day): [1.0] * len(rows) for day in columns}, index=list(rows))


def _yahoo(monkeypatch, info, **statements):
    """yf.Ticker answering ``statements`` by attribute and ``info`` (a dict, or an exception to raise)."""
    asked = []

    class Ticker:
        def __init__(self, symbol):
            self.symbol = symbol

        def __getattr__(self, name):
            asked.append(name)
            if name == "info":
                if isinstance(info, Exception):
                    raise info
                return info
            if name in statements:
                return statements[name]
            raise AttributeError(name)

    monkeypatch.setattr(fundamentals.yf, "Ticker", Ticker)
    return asked


def _header(out: str) -> str:
    return "\n".join(line for line in out.splitlines() if line.startswith("#"))


@pytest.mark.unit
def test_a_statement_in_another_currency_than_the_listing_says_so(monkeypatch):
    frame = pd.DataFrame({pd.Timestamp("2026-03-31"): [0.8, 20158000000.0],
                          pd.Timestamp("2025-03-31"): [0.76, 19277000000.0]},
                         index=["Diluted EPS", "Total Revenue"])
    _yahoo(monkeypatch, {"currency": "INR", "financialCurrency": "USD"}, income_stmt=frame)
    out = fundamentals.get_income_statement("INFY.NS", "annual", TODAY)

    assert out.startswith("# Income Statement data for INFY.NS (annual)\n")
    assert out.endswith(frame.to_csv())
    header = _header(out)
    assert "Yahoo Finance" in header and "in USD" in header and "full units" in header
    assert "INFY.NS quotes in INR" in header and "convert" in header


@pytest.mark.unit
def test_a_statement_in_the_listing_s_currency_needs_no_conversion(monkeypatch):
    _yahoo(monkeypatch, {"currency": "INR", "financialCurrency": "INR"},
           balance_sheet=_statement("2026-03-31", "2025-03-31"))
    header = _header(fundamentals.get_balance_sheet("RELIANCE.NS", "annual", TODAY))
    assert "Yahoo Finance" in header and "in INR" in header and "full units" in header
    assert "convert" not in header


@pytest.mark.unit
def test_london_pence_against_pounds_is_a_different_currency(monkeypatch):
    _yahoo(monkeypatch, {"currency": "GBp", "financialCurrency": "GBP"}, income_stmt=_statement("2026-03-31"))
    header = _header(fundamentals.get_income_statement("VOD.L", "annual", TODAY))
    assert "in GBP" in header and "VOD.L quotes in GBp" in header


@pytest.mark.unit
def test_an_unstated_reporting_currency_is_not_guessed_from_the_quote(monkeypatch):
    """The quote currency is not evidence of the reporting currency (INFY.NS quotes in
    INR and reports in USD), so it is not assumed."""
    _yahoo(monkeypatch, {"currency": "INR"}, income_stmt=_statement("2026-03-31"))
    header = _header(fundamentals.get_income_statement("INFY.NS", "annual", TODAY))
    assert "Yahoo Finance" in header and "does not state" in header
    assert "in INR" not in header and "not assume" in header and "INR" in header


@pytest.mark.unit
def test_a_failed_profile_request_still_serves_the_statement(monkeypatch):
    frame = _statement("2026-03-31")
    _yahoo(monkeypatch, RuntimeError("socket closed"), income_stmt=frame)
    out = fundamentals.get_income_statement("RELIANCE.NS", "annual", TODAY)
    assert out.endswith(frame.to_csv())
    # The request failed: that is not Yahoo saying the company states no currency.
    assert "could not be retrieved" in _header(out) and "does not state" not in _header(out)
    assert "INR" in _header(out)


@pytest.mark.unit
def test_an_empty_statement_is_reported_without_asking_for_the_profile(monkeypatch):
    asked = _yahoo(monkeypatch, {"financialCurrency": "INR"}, income_stmt=pd.DataFrame())
    with pytest.raises(NoMarketDataError):
        fundamentals.get_income_statement("RELIANCE.NS", "annual", TODAY)
    assert "info" not in asked


# --- statements: quarterly columns ----------------------------------------------------------

@pytest.mark.unit
def test_quarterly_columns_that_skip_a_quarter_are_flagged(monkeypatch):
    # RELIANCE.NS on 30 Oct 2026: no 2025-09-30 column.
    _yahoo(monkeypatch, {"currency": "INR", "financialCurrency": "INR"},
           quarterly_income_stmt=_statement("2026-06-30", "2026-03-31", "2025-12-31", "2025-06-30", "2025-03-31"))
    header = _header(fundamentals.get_income_statement("RELIANCE.NS", "quarterly", TODAY))
    assert "not consecutive quarters" in header
    assert "2025-06-30 to 2025-12-31 (6 months)" in header
    assert "2025-12-31 to 2026-03-31" not in header and 'freq="annual"' in header


@pytest.mark.unit
def test_half_yearly_balance_sheet_columns_are_flagged(monkeypatch):
    _yahoo(monkeypatch, {"currency": "INR", "financialCurrency": "INR"},
           quarterly_balance_sheet=_statement("2026-03-31", "2025-09-30", "2025-03-31", rows=("Total Assets",)))
    header = _header(fundamentals.get_balance_sheet("RELIANCE.NS", "quarterly", TODAY))
    assert "not consecutive quarters" in header
    assert "2025-03-31 to 2025-09-30 (6 months)" in header and "2025-09-30 to 2026-03-31 (6 months)" in header


@pytest.mark.unit
@pytest.mark.parametrize("columns", [
    ("2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30"),
    ("2025-12-27", "2025-09-27", "2025-06-28", "2025-03-22"),   # 52/53-week quarters, one of 14 weeks
    ("2026-06-30",),
])
def test_consecutive_quarters_are_not_flagged(monkeypatch, columns):
    _yahoo(monkeypatch, {"currency": "USD", "financialCurrency": "USD"}, quarterly_cashflow=_statement(*columns))
    header = _header(fundamentals.get_cashflow("AAPL", "quarterly", TODAY))
    assert "consecutive" not in header and "Yahoo Finance" in header


@pytest.mark.unit
def test_annual_columns_are_not_read_as_quarters(monkeypatch):
    _yahoo(monkeypatch, {"currency": "INR", "financialCurrency": "INR"},
           income_stmt=_statement("2026-03-31", "2025-03-31", "2024-03-31"))
    assert "consecutive" not in fundamentals.get_income_statement("RELIANCE.NS", "annual", TODAY)


# --- insider prices on a BSE listing ---------------------------------------------------------

def _insider(prices=("15.69", "15.70"), days_ago=(48, 49)):
    today = pd.Timestamp(TODAY)
    return pd.DataFrame({
        "Shares": [25000, 481905][:len(prices)],
        "Value": [392252, 7563595][:len(prices)],
        "Text": [f"Sale at price {p} per share." for p in prices],
        "Start Date": [today - pd.Timedelta(days=d) for d in days_ago[:len(prices)]],
    })


def _closes(low=1370.0, high=1400.0, end=TODAY, periods=120):
    dates = pd.bdate_range(end=end, periods=periods)
    return pd.DataFrame({"Date": dates, "Close": [low + (high - low) * i / (periods - 1) for i in range(periods)]})


TODAY_ONLY = pd.DataFrame({"Date": [pd.Timestamp(TODAY)], "Close": [1155.0]})   # RELIANCE.BO on 30 Oct 2026


def _insider_report(ticker, frame, closes):
    """``closes`` maps a symbol to its frame, or to an exception load_ohlcv raises."""
    def load(symbol, as_of_date, fill_gaps=True, **kwargs):
        found = closes.get(symbol, NoMarketDataError(symbol, detail="no price rows"))
        if isinstance(found, Exception):
            raise found
        return found

    with mock.patch.object(fundamentals.yf, "Ticker", return_value=mock.Mock(insider_transactions=frame)), \
            mock.patch.object(fundamentals, "load_ohlcv", side_effect=load) as loaded:
        out = fundamentals.get_insider_transactions(ticker, TODAY)
    return out, [c.args[0] for c in loaded.call_args_list]


@pytest.mark.unit
@pytest.mark.parametrize("own", [TODAY_ONLY, NoMarketDataError("RELIANCE.BO", detail="no price rows")])
def test_a_bse_listing_without_closes_is_checked_against_its_nse_listing(own):
    frame = _insider()
    out, loaded = _insider_report("RELIANCE.BO", frame, {"RELIANCE.BO": own, "RELIANCE.NS": _closes()})
    assert loaded == ["RELIANCE.BO", "RELIANCE.NS"]
    assert out.startswith("# Insider Transactions data for RELIANCE.BO\n" + frame.to_csv())
    assert "Yahoo does not state the currency" in out and "prices (15.69-15.70)" in out
    assert "RELIANCE.NS" in out and "not rupee amounts" in out


@pytest.mark.unit
def test_a_bse_listing_with_closes_of_its_own_is_not_checked_against_nse():
    out, loaded = _insider_report("RELIANCE.BO", _insider(), {"RELIANCE.BO": _closes(), "RELIANCE.NS": _closes()})
    assert loaded == ["RELIANCE.BO"]
    assert "RELIANCE.BO's INR closes" in out and "RELIANCE.NS" not in out


@pytest.mark.unit
def test_bse_prices_that_fit_the_nse_closes_get_no_note():
    frame = _insider(prices=("1,385.10", "1390"))
    out, loaded = _insider_report("RELIANCE.BO", frame, {"RELIANCE.BO": TODAY_ONLY, "RELIANCE.NS": _closes()})
    assert loaded == ["RELIANCE.BO", "RELIANCE.NS"]
    assert out == "# Insider Transactions data for RELIANCE.BO\n" + frame.to_csv()


@pytest.mark.unit
@pytest.mark.parametrize("ticker, sources", [("RELIANCE.BO", ["RELIANCE.BO", "RELIANCE.NS"]),
                                             ("RELIANCE.NS", ["RELIANCE.NS"])])
def test_rows_that_cannot_be_checked_still_carry_the_currency_caveat(ticker, sources):
    frame = _insider()
    out, loaded = _insider_report(ticker, frame, {})
    assert loaded == sources
    assert out.startswith(f"# Insider Transactions data for {ticker}\n" + frame.to_csv())
    assert "Yahoo does not state the currency" in out and "not assume they are rupee amounts" in out
    assert "do not match" not in out and "SEBI's insider-trading (PIT) regulations" in out


# --- corporate actions: results dates -----------------------------------------------------

def _series(points):
    index = pd.DatetimeIndex([pd.Timestamp(d, tz="Asia/Kolkata") for d, _ in points])
    return pd.Series([v for _, v in points], index=index, dtype=float)


def _actions(monkeypatch, symbol, as_of, calendar=None, dividends=(), splits=(), historical=False):
    def ticker(s):
        if historical:
            return SimpleNamespace(dividends=_series(dividends), splits=_series(splits))
        return SimpleNamespace(dividends=_series(dividends), splits=_series(splits), calendar=calendar or {})

    monkeypatch.setattr(corporate_actions.yf, "Ticker", ticker)
    monkeypatch.setattr(corporate_actions, "is_historical", lambda d, symbol=None: historical)
    return corporate_actions.get_corporate_actions(symbol, as_of)


@pytest.mark.unit
def test_a_results_date_before_the_run_date_is_the_latest_not_the_next(monkeypatch):
    # TCS.BO on 30 Oct 2026: Yahoo kept the 8 Oct date after the results were out.
    out = _actions(monkeypatch, "TCS.BO", TODAY,
                   {"Earnings Date": [date(2026, 10, 8)], "Ex-Dividend Date": date(2026, 10, 14)})
    assert "- Latest results date (already reported" in out and "2026-10-08" in out
    assert "Next results date" not in out
    assert "- Latest ex-dividend date: 2026-10-14" in out


@pytest.mark.unit
@pytest.mark.parametrize("days", [[date(2026, 10, 30)], [date(2026, 10, 28), date(2026, 11, 2)]])
def test_a_results_date_on_the_run_date_may_not_be_out_yet(monkeypatch, days):
    out = _actions(monkeypatch, "RELIANCE.NS", TODAY, {"Earnings Date": days})
    line = next(line for line in out.splitlines() if "esults date" in line)
    assert all(str(day) in line for day in days)
    assert "may not be out yet" in line
    assert "Next results date" not in line and "already reported" not in line


@pytest.mark.unit
def test_a_results_date_after_the_run_date_is_the_next(monkeypatch):
    out = _actions(monkeypatch, "RELIANCE.NS", TODAY, {"Earnings Date": [date(2027, 1, 16)]})
    assert "- Next results date (Yahoo estimate or announcement): 2027-01-16" in out
    assert "Latest results date" not in out


# --- corporate actions: dividend amounts ------------------------------------------------

RELIANCE_DIVIDENDS = [("2022-08-18", 4.0), ("2023-08-21", 4.5), ("2024-08-19", 5.0),
                      ("2025-08-14", 5.5), ("2026-06-05", 6.0)]
RELIANCE_BONUS = [("2024-10-28", 2.0)]   # 1:1 bonus; declared ₹8, ₹9 and ₹10 before it


@pytest.mark.unit
def test_dividend_amounts_say_they_are_at_today_s_share_count(monkeypatch):
    out = _actions(monkeypatch, "RELIANCE.NS", TODAY, {}, RELIANCE_DIVIDENDS, RELIANCE_BONUS)
    assert "- 2024-08-19: 5.00" in out and "- 2025-08-14: 5.50" in out     # Yahoo's amounts, unchanged
    assert "- 2024-10-28: 2-for-1" in out
    label = next(line for line in out.splitlines() if line.startswith("Dividends"))
    assert "today's share count" in label and "split or bonus issue" in label and "declared" in label


@pytest.mark.unit
def test_a_past_run_is_told_the_basis_without_learning_of_a_later_bonus(monkeypatch):
    out = _actions(monkeypatch, "RELIANCE.NS", "2024-09-01", None, RELIANCE_DIVIDENDS, RELIANCE_BONUS,
                   historical=True)
    assert "- 2022-08-18: 4.00" in out and "- 2024-08-19: 5.00" in out
    # "today" would read as the run date, whose share count these amounts are not at.
    assert "today's share count" not in out and "present-day share basis" in out
    assert "2024-10-28" not in out and "2-for-1" not in out and "2025-08-14" not in out
