"""Intraday charges follow the config: the profile and per-field overrides apply to the
snapshot's sizing, the plan's size check and the later scoring, and a mistyped one is refused.

Offline: hand-built contexts and bars, a stub loader and reflector.
"""

from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from tradingagents.agents import intraday_plan
from tradingagents.agents.managers import portfolio_manager
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.markets import costs, intraday
from tradingagents.markets.exchanges import exchange_for_symbol
from tradingagents.memory import TradingMemoryLog, intraday_settlement, settlement

IST = ZoneInfo("Asia/Kolkata")
NSE = exchange_for_symbol("RELIANCE.NS")
NO_BROKERAGE = {"brokerage_rate": 0.0, "brokerage_cap": None}
# A broker charging 1% an order with no cap: far more than the example's Rs 20.
COSTLY = {"brokerage_rate": 0.01, "brokerage_cap": None}


@pytest.mark.unit
def test_nse_and_bse_plans_use_the_example_profile_unless_configured_and_others_none():
    example = costs.get_cost_profile("india_equity_intraday")
    assert intraday.intraday_cost_profile(NSE, {}) == example
    assert intraday.intraday_cost_profile(NSE, None) == example
    assert intraday.intraday_cost_profile(NSE, DEFAULT_CONFIG) == example
    assert intraday.intraday_cost_profile(None, {"intraday_cost_overrides": COSTLY}) is costs.NO_COSTS


@pytest.mark.unit
def test_overrides_and_another_profile_change_the_charges():
    # A Rs 1 lakh NSE round trip: Rs 82.68 under the example; without brokerage (Rs 40) and the
    # GST on it (Rs 7.20), Rs 35.48.
    example = intraday.intraday_cost_profile(NSE, {})
    no_brokerage = intraday.intraday_cost_profile(NSE, {"intraday_cost_overrides": NO_BROKERAGE})
    assert costs.round_trip_costs(example, 100_000) == pytest.approx(82.6812)
    assert costs.round_trip_costs(no_brokerage, 100_000) == pytest.approx(35.4812)
    assert no_brokerage.stt_sell == example.stt_sell and no_brokerage.name == example.name
    nothing = intraday.intraday_cost_profile(NSE, {"intraday_cost_profile": "none"})
    assert costs.round_trip_costs(nothing, 100_000) == 0.0


@pytest.mark.unit
def test_a_mistyped_profile_or_field_is_refused():
    with pytest.raises(ValueError, match="unknown cost profile 'zerodha'"):
        intraday.configured_intraday_costs({"intraday_cost_profile": "zerodha"})
    with pytest.raises(ValueError, match="brokerage_rte"):
        intraday.configured_intraday_costs({"intraday_cost_overrides": {"brokerage_rte": 0.0}})


@pytest.mark.unit
@pytest.mark.parametrize("bad", [{"intraday_cost_profile": "zerodha"},
                                 {"intraday_cost_overrides": {"brokerage_rte": 0.0}}])
def test_the_graph_refuses_mistyped_charges_before_it_starts(bad):
    # Raised before any LLM client is built: a plan's sizing or a later settlement never meets it.
    with pytest.raises(ValueError, match="zerodha|brokerage_rte"):
        TradingAgentsGraph(config={**DEFAULT_CONFIG, **bad})


# --- the plan's size check --------------------------------------------------------------

CONTEXT = {
    "symbol": "RELIANCE.NS", "session": "2026-10-26", "exit_by": "15:10", "timezone": "Asia/Kolkata",
    "reference_price": 100.5, "past_dated": True,
}
FIELDS = {"direction": "long", "entry_price": 101.0, "stop_loss": 99.5, "target_price": 104.0,
          "position_size_shares": 200}
SIZING = {"capital": 100_000, "risk_pct": 0.5, "leverage_cap": 1.0}


@pytest.mark.unit
def test_the_size_check_counts_the_configured_charges():
    # 500 at risk over a 1.50 stop allows 311 shares under the example's charges: 200 is within it.
    assert intraday_plan.plan_from_fields(FIELDS, CONTEXT, rating="Buy", sizing=SIZING)["notes"] == []
    costly = {**SIZING, "intraday_cost_overrides": COSTLY}
    allowed = intraday.intraday_position(100_000, 0.5, 101.0, 99.5, leverage_cap=1.0,
                                         profile=intraday.intraday_cost_profile(NSE, costly), venue="NSE")
    assert allowed["shares"] < 200
    notes = intraday_plan.plan_from_fields(FIELDS, CONTEXT, rating="Buy", sizing=costly)["notes"]
    assert notes == [f"the plan's 200 shares are more than the {allowed['shares']} that risking 0.5% of the "
                     "100,000 paper capital with this stop allows (leverage cap 1x)"]


@pytest.mark.unit
def test_the_portfolio_manager_sizes_with_the_configured_charges(monkeypatch):
    config = {"intraday_paper_capital": 50_000, "intraday_risk_pct": 1.0, "intraday_leverage_cap": 2.0,
              "intraday_cost_profile": "none", "intraday_cost_overrides": NO_BROKERAGE}
    monkeypatch.setattr(portfolio_manager, "get_config", lambda: config)
    assert portfolio_manager._paper_sizing({}) == {
        "capital": 50_000, "risk_pct": 1.0, "leverage_cap": 2.0,
        "intraday_cost_profile": "none", "intraday_cost_overrides": NO_BROKERAGE}
    # A config without the keys sizes with the example profile.
    monkeypatch.setattr(portfolio_manager, "get_config", lambda: {})
    assert "intraday_cost_profile" not in portfolio_manager._paper_sizing({})


# --- the scoring --------------------------------------------------------------------------

PLAN = {
    "direction": "long", "entry_price": 101.0, "stop_loss": 99.5, "target_price": 104.0, "exit_by": "15:10",
    "position_size_shares": 100, "invalidation": "a close under 99.5", "session": "2026-10-26",
    "reference_price": 100.5, "valid_from": None, "timezone": "Asia/Kolkata", "notes": [],
}


def _bars() -> pd.DataFrame:
    """Mon 26 Oct on NSE, flat at 100.2 with a breakout through 101 at 09:20 and 104 at 09:30."""
    rows = [{"Datetime": stamp, "Open": 100.2, "High": 100.4, "Low": 100.0, "Close": 100.2, "Volume": 1000}
            for stamp in pd.date_range("2026-10-26 09:15", "2026-10-26 15:25", freq="5min", tz=IST)]
    rows[1].update(High=101.2, Close=101.0)
    rows[3].update(Open=101.8, High=104.5, Low=101.5, Close=104.2)
    return pd.DataFrame(rows)


class _Reflector:
    def reflect_on_intraday_plan(self, decision, summary):
        return "The breakout held to the target."


@pytest.mark.unit
@pytest.mark.parametrize("config, charges", [
    # Rs 10,100 bought, Rs 10,400 sold: brokerage 3.03 + 3.12, STT 2.60, exchange 0.63,
    # SEBI 0.02, stamp duty 0.30 and GST 1.22; without the brokerage, and its GST, 3.67.
    ({}, pytest.approx(10.93, abs=0.01)),
    ({"intraday_cost_overrides": NO_BROKERAGE}, pytest.approx(3.67, abs=0.01)),
    ({"intraday_cost_profile": "none"}, 0.0),
], ids=["example", "no brokerage", "none"])
def test_a_plan_is_scored_with_the_configured_charges(tmp_path, monkeypatch, config, charges):
    log = TradingMemoryLog({"memory_log_path": str(tmp_path / "memory.md")})
    log.store_decision("RELIANCE.NS", "2026-10-23", "**Rating**: Buy", rating="Buy", intraday_plan=PLAN)
    monkeypatch.setattr(intraday_settlement, "load_intraday_with_meta",
                        lambda *a, **k: (_bars(), SimpleNamespace(live=False)))
    monkeypatch.setattr(intraday_settlement, "_now",
                        lambda: pd.Timestamp("2026-10-26 16:30", tz=IST).to_pydatetime())

    done = settlement.settle_pending("RELIANCE.NS", log, _Reflector(), config)

    assert done.settled == [("RELIANCE.NS", "2026-10-23")]
    (entry,) = log.load_entries()
    outcome = entry["intraday_outcome"]
    # 100 shares bought at 101 and sold at the 104 target: 300 gross, less the charges.
    assert (outcome["exit_reason"], outcome["gross_pnl"]) == ("target", pytest.approx(300.0))
    assert outcome["charges"] == charges
    assert outcome["net_pnl"] == pytest.approx(300.0 - outcome["charges"])
