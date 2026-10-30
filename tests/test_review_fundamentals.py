"""Yahoo corporate actions, statements and insider rows: second-round review fixes.

- A past run's dividends are on Yahoo's present-day share basis, which can take in
  splits or bonus issues after the run date that the run is not shown. Its label
  says so and gives no rule for the declared amounts, which only a current run,
  shown every later split, can work out.
- A listing's reporting currency is read from Yahoo's profile once a day, not for
  every statement, and a profile that fails does not hold up a statement that has
  arrived; the failure is not kept, so the next statement asks again.
- Insider rows whose closes could not be retrieved are not told there were none.

yf.Ticker and load_ohlcv are stubbed and the clock is fixed; no test reaches Yahoo.
"""

from types import SimpleNamespace
from unittest import mock

import pandas as pd
import pytest
from yfinance.exceptions import YFRateLimitError

from tradingagents.dataflows import date_window
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.vendors.yahoo import common, corporate_actions, fundamentals

TODAY = "2026-10-30"


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    """The machine's date is TODAY; the listing's own date is never earlier than it.
    Each test starts with no profile read, whatever another test's Yahoo answered."""
    for module in (date_window, fundamentals, corporate_actions):
        monkeypatch.setattr(module, "get_current_date", lambda: TODAY)
    monkeypatch.setattr(date_window, "listing_date", lambda symbol, now=None: None)
    monkeypatch.setattr(common, "vendor_reachable", lambda url: True)
    fundamentals._currencies.cache_clear()
    yield
    fundamentals._currencies.cache_clear()


# --- dividends: the basis a past run is told ----------------------------------------------

def _series(points):
    index = pd.DatetimeIndex([pd.Timestamp(d, tz="Asia/Kolkata") for d, _ in points])
    return pd.Series([v for _, v in points], index=index, dtype=float)


# RELIANCE.NS as Yahoo gave it on 30 Oct 2026, amounts before the 1:1 bonus of 28 Oct 2024
# halved: the 4.00 / 4.50 / 5.00 of 2022-2024 were declared as ₹8 / ₹9 / ₹10.
RELIANCE_DIVIDENDS = [("2020-07-02", 3.25), ("2021-06-11", 3.5), ("2022-08-18", 4.0),
                      ("2023-08-21", 4.5), ("2024-08-19", 5.0), ("2025-08-14", 5.5)]
RELIANCE_SPLITS = [("1997-10-27", 2.0), ("2009-11-26", 2.0), ("2017-09-07", 2.0), ("2024-10-28", 2.0)]


def _actions(monkeypatch, as_of):
    """RELIANCE.NS's corporate actions up to ``as_of``, and their dividends line."""
    monkeypatch.setattr(corporate_actions.yf, "Ticker", lambda symbol: SimpleNamespace(
        dividends=_series(RELIANCE_DIVIDENDS), splits=_series(RELIANCE_SPLITS), calendar={}))
    out = corporate_actions.get_corporate_actions("RELIANCE.NS", as_of)
    return out, next(line for line in out.splitlines() if line.startswith("Dividends"))


@pytest.mark.unit
def test_a_past_run_is_not_told_its_dividends_are_at_its_share_count_or_as_declared(monkeypatch):
    """The analyst is told to treat the run date as today, so "today's share count"
    reads as the 2024 one, and the rule offered for the declared amounts gave half of them."""
    out, label = _actions(monkeypatch, "2024-09-01")
    assert "- 2024-08-19: 5.00" in out and "2024 5.00" in out      # Yahoo's, on the basis of the run's prices
    assert "2024-10-28" not in out and "2-for-1" not in out         # the later bonus stays unseen
    assert "today's share count" not in label
    assert "times that ratio" not in label and "declared was" not in label
    assert "present-day share basis" in label and "this run's prices" in label
    assert "after 2024-09-01" in label and "not listed here" in label and "below the amounts declared" in label


@pytest.mark.unit
def test_a_current_run_keeps_the_rule_for_the_declared_amounts(monkeypatch):
    """A run dated today is shown every split after a dividend in its window, so the rule holds."""
    out, label = _actions(monkeypatch, TODAY)
    assert "- 2024-08-19: 5.00" in out and "- 2024-10-28: 2-for-1" in out
    assert "today's share count" in label
    assert "the amount declared was the amount shown times that ratio" in label
    assert "present-day" not in label


# --- statements: the profile their currency is read from -----------------------------------

FRAME = pd.DataFrame({pd.Timestamp("2026-03-31"): [0.8, 20158000000.0],
                      pd.Timestamp("2025-03-31"): [0.76, 19277000000.0]},
                     index=["Diluted EPS", "Total Revenue"])
INFY = {"currency": "INR", "financialCurrency": "USD"}
RELIANCE = {"currency": "INR", "financialCurrency": "INR"}


def _yahoo(monkeypatch, profiles):
    """yf.Ticker serving FRAME as every annual statement and each symbol's profile from
    ``profiles`` (a dict, or an exception to raise); returns the symbols whose profile was read."""
    asked = []

    class Ticker:
        income_stmt = balance_sheet = cashflow = FRAME

        def __init__(self, symbol):
            self.symbol = symbol

        @property
        def info(self):
            asked.append(self.symbol)
            answer = profiles[self.symbol]
            if isinstance(answer, Exception):
                raise answer
            return answer

    monkeypatch.setattr(fundamentals.yf, "Ticker", Ticker)
    return asked


def _header(out: str) -> str:
    return "\n".join(line for line in out.splitlines() if line.startswith("#"))


@pytest.mark.unit
def test_a_listing_s_profile_is_read_once_a_day_for_all_its_statements(monkeypatch):
    asked = _yahoo(monkeypatch, {"INFY.NS": INFY, "RELIANCE.NS": RELIANCE})
    infy = [fundamentals.get_income_statement("INFY.NS", "annual", TODAY),
            fundamentals.get_balance_sheet("INFY.NS", "annual", TODAY),
            fundamentals.get_cashflow("INFY.NS", "annual", TODAY)]
    reliance = fundamentals.get_income_statement("RELIANCE.NS", "annual", TODAY)

    assert asked == ["INFY.NS", "RELIANCE.NS"]
    for out in infy:
        assert out.endswith(FRAME.to_csv())
        assert "in USD" in _header(out) and "INFY.NS quotes in INR" in _header(out)
    assert "in INR" in _header(reliance) and "convert" not in _header(reliance)


@pytest.mark.unit
def test_the_profile_is_read_again_on_a_new_day(monkeypatch):
    asked = _yahoo(monkeypatch, {"RELIANCE.NS": RELIANCE})
    fundamentals.get_income_statement("RELIANCE.NS", "annual", TODAY)
    for module in (date_window, fundamentals):
        monkeypatch.setattr(module, "get_current_date", lambda: "2026-10-31")
    fundamentals.get_income_statement("RELIANCE.NS", "annual", "2026-10-31")
    assert asked == ["RELIANCE.NS", "RELIANCE.NS"]


def _not_found():
    """What yfinance raises when Yahoo answers that it has nothing (HTTP 404)."""
    exc = Exception("404 Not Found")
    exc.response = SimpleNamespace(status_code=404)
    return exc


@pytest.mark.unit
@pytest.mark.parametrize("failure", [YFRateLimitError(), RuntimeError("socket closed"), _not_found()],
                         ids=["rate_limited", "failed", "no_profile"])
def test_a_failed_profile_does_not_hold_up_the_statement_and_is_asked_again(monkeypatch, failure):
    slept = []
    monkeypatch.setattr(common, "time", SimpleNamespace(sleep=slept.append))
    profiles = {"INFY.NS": failure}
    asked = _yahoo(monkeypatch, profiles)

    out = fundamentals.get_income_statement("INFY.NS", "annual", TODAY)
    # Only Yahoo answering that it has no profile is Yahoo not stating the currency.
    said = "does not state" if failure.__class__ is Exception else "could not be retrieved"
    assert out.endswith(FRAME.to_csv()) and said in _header(out)
    assert slept == [] and asked == ["INFY.NS"]          # no backoff for a header line

    profiles["INFY.NS"] = INFY                           # Yahoo answers the next statement's read
    out = fundamentals.get_balance_sheet("INFY.NS", "annual", TODAY)
    assert "in USD" in _header(out) and asked == ["INFY.NS", "INFY.NS"]


# --- insider rows: closes that could not be retrieved ---------------------------------------

def _insider():
    today = pd.Timestamp(TODAY)
    return pd.DataFrame({
        "Shares": [25000, 481905],
        "Value": [392252, 7563595],
        "Text": ["Sale at price 15.69 per share.", "Sale at price 15.70 per share."],
        "Start Date": [today - pd.Timedelta(days=48), today - pd.Timedelta(days=49)],
    })


def _closes(low=1347.67, high=1497.28, periods=120):
    dates = pd.bdate_range(end=TODAY, periods=periods)
    return pd.DataFrame({"Date": dates, "Close": [low + (high - low) * i / (periods - 1) for i in range(periods)]})


TODAY_ONLY = pd.DataFrame({"Date": [pd.Timestamp(TODAY)], "Close": [1155.0]})   # RELIANCE.BO on 30 Oct 2026
THROTTLED = VendorUnavailableError("Yahoo Finance rate limited after 3 retries: Too Many Requests")


def _note(ticker, closes):
    """The note under ``ticker``'s insider rows. ``closes`` maps a symbol to its frame, or to
    the exception load_ohlcv raises; a symbol not in it has no price rows."""
    def load(symbol, as_of_date, fill_gaps=True, **kwargs):
        found = closes.get(symbol, NoMarketDataError(symbol, detail="no price rows"))
        if isinstance(found, Exception):
            raise found
        return found

    frame = _insider()
    with mock.patch.object(fundamentals.yf, "Ticker", return_value=mock.Mock(insider_transactions=frame)), \
            mock.patch.object(fundamentals, "load_ohlcv", side_effect=load):
        out = fundamentals.get_insider_transactions(ticker, TODAY)
    assert out.startswith(f"# Insider Transactions data for {ticker}\n" + frame.to_csv() + "\nNote: ")
    return out[out.index("\nNote: "):]


@pytest.mark.unit
@pytest.mark.parametrize("ticker, closes, sources", [
    ("RELIANCE.NS", {"RELIANCE.NS": THROTTLED}, "RELIANCE.NS"),
    ("RELIANCE.NS", {"RELIANCE.NS": OSError("cache file unreadable")}, "RELIANCE.NS"),
    ("RELIANCE.BO", {"RELIANCE.BO": THROTTLED, "RELIANCE.NS": THROTTLED}, "RELIANCE.BO or RELIANCE.NS"),
    ("RELIANCE.BO", {"RELIANCE.NS": THROTTLED}, "RELIANCE.BO or RELIANCE.NS"),     # none on BSE, NSE failed
], ids=["nse_throttled", "nse_failed", "both_throttled", "bse_none_nse_throttled"])
def test_closes_that_could_not_be_retrieved_are_not_said_to_be_absent(ticker, closes, sources):
    note = _note(ticker, closes)
    assert f"no INR closes of {sources} over the same period could be retrieved to check them against" in note
    assert "there were no" not in note and "do not match" not in note
    assert "Yahoo does not state the currency" in note and "not assume they are rupee amounts" in note


@pytest.mark.unit
@pytest.mark.parametrize("own", [TODAY_ONLY, NoMarketDataError("RELIANCE.BO", detail="no price rows")],
                         ids=["outside_the_span", "no_rows"])
def test_closes_yahoo_does_not_have_are_still_said_to_be_absent(own):
    note = _note("RELIANCE.BO", {"RELIANCE.BO": own})
    assert "there were no INR closes of RELIANCE.BO or RELIANCE.NS over the same period" in note
    assert "could be retrieved" not in note


@pytest.mark.unit
@pytest.mark.parametrize("own, said", [
    (TODAY_ONLY, "; Yahoo has none for RELIANCE.BO then), so"),
    (THROTTLED, "; those of RELIANCE.BO could not be retrieved), so"),
], ids=["none", "throttled"])
def test_a_bse_listing_is_said_to_have_no_closes_only_when_yahoo_has_none(own, said):
    note = _note("RELIANCE.BO", {"RELIANCE.BO": own, "RELIANCE.NS": _closes()})
    assert "do not match the INR closes of RELIANCE.NS, the NSE listing under the same symbol," in note
    assert said in note
