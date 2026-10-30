"""The Indian-market CLI and reports: exchange and benchmark choice, the market command, report disclaimers.

Vendor calls are stubbed; no test reaches the network or a model.
"""

import pytest
from typer.testing import CliRunner

import cli.main as cli_main
import cli.market as cli_market
from cli.prompts import parse_ticker
from cli.run import _build_run_config
from tradingagents.agents.schemas import PortfolioDecision, PortfolioRating, render_pm_decision
from tradingagents.reporting import write_report_tree

# --- the analyze command's Indian options ----------------------------------------

@pytest.mark.unit
def test_exchange_and_benchmark_reach_the_run(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli_main, "run_analysis", lambda **kw: seen.update(kw))
    result = CliRunner().invoke(cli_main.app, ["--exchange", "nse", "--ticker", "RELIANCE", "--benchmark",
                                               "BANKNIFTY", "--vendor", "news_data=yfinance"])
    assert result.exit_code == 0, result.output
    assert seen["flags"]["exchange"] == "NSE" and seen["flags"]["benchmark"] == "BANKNIFTY"
    assert seen["flags"]["vendors"] == {"news_data": "yfinance"}


@pytest.mark.unit
@pytest.mark.parametrize("args,message", [
    (["--exchange", "lse"], "unknown exchange"),
    (["--vendor", "prices=yfinance"], "unknown data category"),
])
def test_a_bad_exchange_or_vendor_stops_before_the_run(monkeypatch, args, message):
    monkeypatch.setattr(cli_main, "run_analysis", lambda **kw: pytest.fail("the run must not start"))
    result = CliRunner().invoke(cli_main.app, args)
    assert result.exit_code == 1 and message in result.output


@pytest.mark.unit
def test_a_bare_ticker_is_listed_on_the_chosen_exchange():
    assert parse_ticker("RELIANCE", "NSE") == "RELIANCE.NS"
    assert parse_ticker("TCS.BO", "NSE") == "TCS.BO"
    assert parse_ticker("nifty bank") == "^NSEBANK"
    with pytest.raises(ValueError, match="RELIANCE.BO"):
        parse_ticker("500325", "BSE")


@pytest.mark.unit
def test_benchmark_and_vendor_choices_shape_the_run_config():
    selections = {"research_depth": 1, "quick_think_llm": "q", "deep_think_llm": "d", "backend_url": None,
                  "llm_provider": "openai", "benchmark_ticker": "^NSEBANK",
                  "data_vendors": {"core_stock_apis": "alpha_vantage"}}
    config = _build_run_config(selections, checkpoint=None)
    assert config["benchmark_ticker"] == "^NSEBANK"
    assert config["data_vendors"]["core_stock_apis"] == "alpha_vantage"
    assert config["data_vendors"]["news_data"] == "yfinance"          # the rest kept


# --- the market command ----------------------------------------------------------

@pytest.mark.unit
def test_the_market_command_shows_the_session_and_context_without_a_model(monkeypatch):
    calls = []
    monkeypatch.setattr(cli_market, "route_to_vendor",
                        lambda method, *args: calls.append((method, args)) or f"# {method}")
    result = CliRunner().invoke(cli_main.app, ["market", "--exchange", "nse", "--watchlist", "RELIANCE, TCS",
                                               "--date", "2026-10-28"])
    assert result.exit_code == 0, result.output
    output = " ".join(result.output.split())   # rich wraps long lines
    assert "National Stock Exchange of India" in output and "NSE is" in output
    assert calls == [("get_market_context", ("^NSEI", "2026-10-28")),
                     ("get_watchlist_snapshot", (["RELIANCE.NS", "TCS.NS"], "2026-10-28"))]
    assert "No orders are placed" in output


@pytest.mark.unit
def test_the_market_command_refuses_an_unknown_exchange_or_date(monkeypatch):
    monkeypatch.setattr(cli_market, "route_to_vendor", lambda *a: pytest.fail("no request on bad input"))
    assert CliRunner().invoke(cli_main.app, ["market", "--exchange", "lse"]).exit_code == 1
    assert CliRunner().invoke(cli_main.app, ["market", "--date", "28-10-2026"]).exit_code == 1


# --- reports ---------------------------------------------------------------------

def _state():
    return {"trade_date": "2026-10-28", "market_report": "Market view.",
            "final_trade_decision": "**Rating**: Hold\n\n**Executive Summary**: Wait."}


@pytest.mark.unit
def test_an_indian_report_names_its_market_mode_and_that_it_is_not_sebi_registered_advice(tmp_path):
    report = write_report_tree(_state(), "RELIANCE.NS", tmp_path / "r").read_text(encoding="utf-8")
    assert "- Market: National Stock Exchange of India (NSE); prices in INR; exchange time IST" in report
    assert "- Mode: research and paper trading only; no orders are placed" in report
    assert "not investment advice" in report and "SEBI-registered" in report
    page = (tmp_path / "r" / "complete_report.html").read_text(encoding="utf-8")
    assert 'class="disclaimer"' in page and "SEBI-registered" in page and "<dt>Market</dt>" in page


@pytest.mark.unit
def test_a_us_report_keeps_the_general_disclaimer_only(tmp_path):
    report = write_report_tree(_state(), "AAPL", tmp_path / "r").read_text(encoding="utf-8")
    assert "- Market:" not in report and "SEBI" not in report and "not investment advice" in report


@pytest.mark.unit
def test_the_decision_carries_scenarios_risks_and_a_forgiving_confidence():
    decision = PortfolioDecision(rating=PortfolioRating.HOLD, executive_summary="s", investment_thesis="t",
                                 bull_case="Margins recover.", bear_case="Crude spikes.", confidence="Medium")
    assert decision.confidence == "medium"
    text = render_pm_decision(decision)
    for line in ("**Bull Case**: Margins recover.", "**Base Case**: not provided", "**Bear Case**: Crude spikes.",
                 "**Key Risks**: not provided", "**Confidence**: medium"):
        assert line in text
    assert text.startswith("**Rating**: Hold")
    assert PortfolioDecision(rating="Hold", executive_summary="s", investment_thesis="t",
                             confidence="very sure").confidence is None
