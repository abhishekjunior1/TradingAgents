"""Indian portfolios: INR holdings, risk figures, trading costs and tax, watchlists, and vendor spellings.

Prices are synthetic and injected; no test reaches a vendor.
"""

import json

import numpy as np
import pandas as pd
import pytest

from cli.prompts import parse_vendors
from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.symbols import resolve_symbol
from tradingagents.dataflows.vendors import fred, reddit, stocktwits
from tradingagents.dataflows.vendors.alpha_vantage.common import alpha_vantage_symbol
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.markets import costs
from tradingagents.markets.watchlists import load_watchlists, watchlist_symbols
from tradingagents.portfolio import PortfolioContext, load_portfolio
from tradingagents.portfolio_risk import portfolio_risk_block

TRADE_DATE = "2026-10-28"


# --- costs and tax ---------------------------------------------------------------

@pytest.mark.unit
def test_delivery_charges_on_a_one_lakh_nse_order():
    profile = costs.get_cost_profile("india_equity_delivery")
    buy = costs.order_costs(profile, "buy", 100_000)
    assert buy.stt == pytest.approx(100.0)            # 0.1%
    assert buy.exchange == pytest.approx(3.07)        # 0.00307%
    assert buy.sebi == pytest.approx(0.10)            # Rs 10 a crore; 1 lakh is 0.01 crore
    assert buy.stamp == pytest.approx(15.0)           # 0.015%, buyer only
    assert buy.gst == pytest.approx((3.07 + 0.10) * 0.18)
    assert buy.brokerage == 0 and buy.dp == 0
    sell = costs.order_costs(profile, "sell", 100_000)
    assert (sell.stamp, sell.dp) == (0.0, 15.34)
    assert costs.round_trip_costs(profile, 100_000) == pytest.approx(buy.total + sell.total)
    assert costs.order_costs(profile, "buy", 100_000, "BSE").exchange == pytest.approx(3.75)


@pytest.mark.unit
def test_intraday_brokerage_is_the_lower_of_the_rate_and_the_cap():
    profile = costs.get_cost_profile("india_equity_intraday")
    assert costs.order_costs(profile, "buy", 10_000).brokerage == pytest.approx(3.0)        # 0.03%
    assert costs.order_costs(profile, "buy", 1_000_000).brokerage == pytest.approx(20.0)    # capped
    assert costs.order_costs(profile, "buy", 1_000_000).stt == 0.0                          # sell side only


@pytest.mark.unit
def test_every_rate_is_overridable_and_a_typo_is_refused():
    profile = costs.get_cost_profile("india_equity_delivery", {"brokerage_rate": 0.001, "brokerage_cap": 20.0})
    assert costs.order_costs(profile, "buy", 100_000).brokerage == pytest.approx(20.0)
    with pytest.raises(ValueError, match="brokerage_rte"):
        costs.get_cost_profile("india_equity_delivery", {"brokerage_rte": 0.001})
    with pytest.raises(ValueError, match="india_equity_delivery"):
        costs.get_cost_profile("zerodha")
    assert costs.get_cost_profile(None).name == "none"


@pytest.mark.unit
def test_capital_gains_by_holding_period():
    rules = costs.get_capital_gains_rules("india_listed_equity")
    assert costs.capital_gains_tax(rules, 100_000, holding_days=200) == pytest.approx(20_000)     # 20% short-term
    assert costs.capital_gains_tax(rules, 225_000, holding_days=400) == pytest.approx(12_500)     # 12.5% over 1.25 lakh
    assert costs.capital_gains_tax(rules, 225_000, 400, exemption_used=125_000) == pytest.approx(28_125)
    assert costs.capital_gains_tax(rules, -5_000, 400) == 0.0
    assert costs.get_capital_gains_rules(None) is None


# --- portfolio risk --------------------------------------------------------------

def _prices(rng_seed: int, start: float, drift: float, sessions: int = 300) -> pd.DataFrame:
    rng = np.random.default_rng(rng_seed)
    closes = start * np.cumprod(1 + drift + rng.normal(0, 0.012, sessions))
    dates = pd.bdate_range(end=TRADE_DATE, periods=sessions)
    return pd.DataFrame({"Date": dates, "Open": closes, "High": closes * 1.01, "Low": closes * 0.99,
                         "Close": closes, "Volume": 1_000})


PRICES = {"RELIANCE.NS": _prices(1, 1300, 0.0004), "TCS.NS": _prices(2, 3000, -0.0002), "^NSEI": _prices(3, 22000, 0.0003)}


def _block(book: dict, ticker: str = "RELIANCE.NS", **config):
    def prices(symbol):
        if symbol not in PRICES:
            raise NoMarketDataError(symbol, detail="no rows")
        return PRICES[symbol]

    sectors = {"RELIANCE.NS": "Energy", "TCS.NS": "Technology"}
    return portfolio_risk_block(PortfolioContext.model_validate(book), ticker, TRADE_DATE, prices,
                                sectors.get, {**DEFAULT_CONFIG, **config})


@pytest.mark.unit
def test_an_inr_book_reads_in_rupees_with_risk_figures_per_holding():
    text = _block({"cash": 500_000, "currency": "INR", "positions": [
        {"ticker": "RELIANCE.NS", "quantity": 100, "average_price": 1200, "acquired": "2025-01-15"},
        {"ticker": "TCS.NS", "quantity": 50},
        {"ticker": "GHOST.NS", "quantity": 10}]})
    assert "amounts in INR" in text and "cash ₹5,00,000.00" in text
    reliance = next(line for line in text.splitlines() if line.startswith("- RELIANCE.NS"))
    for part in ("100 units at last close ₹", "1Y volatility", "1Y max drawdown", "beta to NIFTY 50",
                 "1Y return vs NIFTY 50", "unrealized P/L", "long-term", "india_listed_equity"):
        assert part in reliance
    assert "No prices up to 2026-10-28 for: GHOST.NS" in text
    exposure = next(line for line in text.splitlines() if line.startswith("- Sector exposure (share of holdings):"))
    assert "Energy" in exposure and "Technology" in exposure
    assert "effective number of holdings" in text
    assert "Sizing reference for RELIANCE.NS" in text and "2 x ATR(14)" in text
    assert "under the 'india_equity_delivery' example profile (rates as of 2026-10-29; verify with your broker)" in text


@pytest.mark.unit
def test_the_sizing_reference_follows_the_configured_risk_budget():
    small = _block({"cash": 1_000_000, "currency": "INR"}, risk_per_trade_pct=0.5)
    large = _block({"cash": 1_000_000, "currency": "INR"}, risk_per_trade_pct=2.0, max_position_pct=100)
    shares = [int(t.split("allows about ")[1].split(" shares")[0].replace(",", "")) for t in (small, large)]
    assert shares[1] == pytest.approx(4 * shares[0], rel=0.01)
    assert "risking 0.5% of the book (₹5,000.00)" in small


@pytest.mark.unit
def test_a_book_in_two_currencies_sums_nothing_across_them():
    text = _block({"cash": 10_000, "currency": "USD", "positions": [{"ticker": "RELIANCE.NS", "quantity": 10}]})
    assert "several currencies" in text and "Concentration" not in text and "allows about" not in text
    assert "Sizing reference: n/a (the book holds several currencies" in text


@pytest.mark.unit
def test_without_a_purchase_date_the_tax_line_says_so():
    text = _block({"currency": "INR", "positions": [{"ticker": "RELIANCE.NS", "quantity": 10, "average_price": 1000}]})
    assert "purchase date not given" in text


@pytest.mark.unit
def test_an_inr_book_renders_with_indian_grouping_and_a_bad_date_is_refused():
    book = PortfolioContext.model_validate({"cash": 1234567.5, "currency": "INR",
                                            "positions": [{"ticker": "TCS.NS", "quantity": 5, "average_price": 3456.7}]})
    text = book.render("TCS.NS")
    assert "average price ₹3,456.70" in text and "Cash available: ₹12,34,567.50" in text
    with pytest.raises(ValueError):
        PortfolioContext.model_validate({"positions": [{"ticker": "TCS.NS", "quantity": 1, "acquired": "15/01/2025"}]})


@pytest.mark.unit
def test_a_portfolio_file_reads_bare_tickers_on_the_default_exchange(tmp_path):
    path = tmp_path / "book.json"
    path.write_text(json.dumps({"positions": [{"ticker": "RELIANCE", "quantity": 1}, {"ticker": "INFY.BO", "quantity": 2}]}))
    book = load_portfolio(path, "NSE")
    assert [p.ticker for p in book.positions] == ["RELIANCE.NS", "INFY.BO"]


@pytest.mark.unit
def test_the_graph_gives_the_agents_the_risk_figures_and_degrades_to_the_holdings(monkeypatch, tmp_path):
    from tradingagents.graph import trading_graph

    graph = object.__new__(trading_graph.TradingAgentsGraph)
    graph.config = {**DEFAULT_CONFIG}
    book = PortfolioContext.model_validate({"cash": 100_000, "currency": "INR", "positions": [
        {"ticker": "RELIANCE.NS", "quantity": 10}]})
    monkeypatch.setattr(trading_graph, "load_ohlcv", lambda symbol, date: PRICES[symbol])
    monkeypatch.setattr(trading_graph, "resolve_instrument_identity", lambda s: {"sector": "Energy"})
    text = graph._portfolio_context("RELIANCE.NS", TRADE_DATE, book)
    assert text.startswith("Portfolio at the analysis date:") and "Portfolio risk as of 2026-10-28" in text

    monkeypatch.setattr(trading_graph, "portfolio_risk_block", lambda *a, **k: 1 / 0)
    assert graph._portfolio_context("RELIANCE.NS", TRADE_DATE, book) == book.render("RELIANCE.NS")


# --- watchlists and vendor choice ------------------------------------------------

@pytest.mark.unit
def test_watchlists_come_from_config_and_file_and_resolve_on_the_exchange(tmp_path):
    path = tmp_path / "watchlists.json"
    path.write_text(json.dumps({"IT": ["TCS", "INFY", "tcs.ns"]}))
    config = {"watchlists": {"banks": ["HDFCBANK.NS", "SBIN"]}, "watchlists_file": str(path)}
    assert set(load_watchlists(config)) == {"banks", "it"}
    resolve = lambda s: resolve_symbol(s, "NSE")  # noqa: E731
    assert watchlist_symbols("it", config, resolve) == ["TCS.NS", "INFY.NS"]          # deduplicated
    assert watchlist_symbols("Banks", config, resolve) == ["HDFCBANK.NS", "SBIN.NS"]
    assert watchlist_symbols("RELIANCE, NIFTY 50", config, resolve) == ["RELIANCE.NS", "^NSEI"]
    path.write_text(json.dumps({"bad": "TCS"}))
    with pytest.raises(ValueError, match="list of symbols"):
        load_watchlists(config)


@pytest.mark.unit
def test_a_vendor_choice_is_checked_before_the_run():
    assert parse_vendors(["core_stock_apis=alpha_vantage", "news_data = yfinance,alpha_vantage"]) == {
        "core_stock_apis": "alpha_vantage", "news_data": "yfinance,alpha_vantage"}
    assert parse_vendors(None) == {}
    for bad, message in (("core_stock_apis", "category=vendor"), ("prices=yfinance", "unknown data category"),
                         ("core_stock_apis=nse_scraper", "no vendor nse_scraper")):
        with pytest.raises(ValueError, match=message):
            parse_vendors([bad])


# --- vendor spellings for Indian listings ----------------------------------------

@pytest.mark.unit
def test_each_vendor_gets_the_indian_listing_in_its_own_spelling():
    assert stocktwits._stocktwits_symbol("RELIANCE.NS") == "RELIANCE.NSE"
    assert stocktwits._stocktwits_symbol("BTC-USD") == "BTC.X" and stocktwits._stocktwits_symbol("aapl") == "AAPL"
    assert alpha_vantage_symbol("RELIANCE.BO") == "RELIANCE.BSE" and alpha_vantage_symbol("IBM") == "IBM"
    with pytest.raises(NoMarketDataError, match="does not cover NSE"):
        alpha_vantage_symbol("RELIANCE.NS")
    assert reddit.subreddits_for("TCS.NS") == reddit.INDIA_SUBREDDITS
    assert reddit.subreddits_for("AAPL") == reddit.DEFAULT_SUBREDDITS
    assert (reddit.search_term("RELIANCE.NS"), reddit.search_term("TCS.BO"), reddit.search_term("BTC-USD")) == (
        "RELIANCE", "TCS", "BTC")


@pytest.mark.unit
def test_india_macro_aliases_name_only_series_fred_still_updates():
    assert fred._resolve_series_id("usd_inr") == "DEXINUS"
    assert fred._resolve_series_id("india_10y") == "INDIRLTLT01STM"
    assert fred._resolve_series_id("India Call Rate") == "IRSTCI01INM156N"
    # The stale India CPI and central-bank-rate series are not aliased.
    assert "INDCPIALLMINMEI" not in fred.MACRO_SERIES.values()
    assert "IRSTCB01INM156N" not in fred.MACRO_SERIES.values()
