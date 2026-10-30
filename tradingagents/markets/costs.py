"""Trading costs and capital-gains tax for Indian listed equities, as configurable, dated profiles.

The bundled rates are an example, checked on the date each profile states,
against the sources it names. Exchanges, SEBI, the government and every broker
change them, and a broker's own brokerage differs from the example's, so every
rate is a field the caller overrides (config ``cost_overrides``,
``capital_gains_overrides``). Nothing here says what a real trade will cost.
"""

import calendar
import numbers
from collections.abc import Mapping
from dataclasses import dataclass, fields, replace
from datetime import date

CRORE = 10_000_000


@dataclass(frozen=True)
class CostProfile:
    """Per-order charges, each a fraction of the order's value unless named per order or per crore."""

    name: str
    as_of: str
    source: str
    brokerage_rate: float = 0.0           # of order value
    brokerage_cap: float | None = None    # per order, the lower of rate x value and this
    stt_buy: float = 0.0                  # securities transaction tax
    stt_sell: float = 0.0
    exchange_txn_nse: float = 0.0         # exchange transaction charges, by venue
    exchange_txn_bse: float = 0.0
    sebi_per_crore: float = 0.0           # SEBI turnover fee, per crore of value
    stamp_buy: float = 0.0                # stamp duty, buyer pays
    gst: float = 0.0                      # on brokerage + exchange charges + SEBI fee
    dp_per_sell: float = 0.0              # depository charge per sell order, GST included


@dataclass(frozen=True)
class OrderCosts:
    brokerage: float
    stt: float
    exchange: float
    sebi: float
    stamp: float
    gst: float
    dp: float

    @property
    def total(self) -> float:
        return self.brokerage + self.stt + self.exchange + self.sebi + self.stamp + self.gst + self.dp


def order_costs(profile: CostProfile, side: str, value: float, venue: str = "NSE") -> OrderCosts:
    """The charges on one ``side`` ("buy" or "sell") order of ``value`` on ``venue`` (NSE or BSE)."""
    if side not in ("buy", "sell"):
        raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")
    value = abs(value)
    brokerage = value * profile.brokerage_rate
    if profile.brokerage_cap is not None:
        brokerage = min(brokerage, profile.brokerage_cap)
    exchange = value * (profile.exchange_txn_bse if venue.upper() == "BSE" else profile.exchange_txn_nse)
    sebi = value * profile.sebi_per_crore / CRORE
    return OrderCosts(
        brokerage=brokerage,
        stt=value * (profile.stt_buy if side == "buy" else profile.stt_sell),
        exchange=exchange,
        sebi=sebi,
        stamp=value * profile.stamp_buy if side == "buy" else 0.0,
        gst=(brokerage + exchange + sebi) * profile.gst,
        dp=profile.dp_per_sell if side == "sell" and value else 0.0,
    )


def round_trip_costs(profile: CostProfile, buy_value: float, sell_value: float | None = None,
                     venue: str = "NSE") -> float:
    """Buying ``buy_value`` and selling at ``sell_value`` (the same value by default)."""
    sell_value = buy_value if sell_value is None else sell_value
    return (order_costs(profile, "buy", buy_value, venue).total
            + order_costs(profile, "sell", sell_value, venue).total)


@dataclass(frozen=True)
class CapitalGainsRules:
    """Tax on a listed-equity gain by holding period. Surcharge and cess are not included."""

    name: str
    as_of: str
    source: str
    long_term_after_months: int = 12      # held longer than this is long-term
    short_term_rate: float = 0.0
    long_term_rate: float = 0.0
    long_term_exemption: float = 0.0      # long-term gains exempt each financial year


def _months_after(day: date, months: int) -> date:
    """``day`` moved on ``months`` calendar months, kept within the month (29 Feb + 12 months is 28 Feb)."""
    index = day.month - 1 + months
    year, month = day.year + index // 12, index % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def is_long_term(rules: CapitalGainsRules, acquired: date, sold: date) -> bool:
    """Whether shares bought on ``acquired`` and sold on ``sold`` were held longer than
    ``long_term_after_months`` calendar months: exactly 12 months (15 Jun 2023 to 15 Jun 2024,
    366 days) is still short-term."""
    return sold > _months_after(acquired, rules.long_term_after_months)


def capital_gains_tax(rules: CapitalGainsRules, gain: float, holding_days: int,
                      exemption_used: float = 0.0, *, long_term: bool | None = None) -> float:
    """Estimated tax on ``gain`` from a sale after ``holding_days``; a loss owes none here.

    Pass ``long_term`` (``is_long_term`` on the purchase and sale dates) when the dates are
    known: a count of days alone cannot tell an exact 12 months across 29 Feb from a longer hold.
    """
    if gain <= 0:
        return 0.0
    if long_term is None:
        long_term = holding_days > rules.long_term_after_months * 365 / 12
    if long_term:
        taxable = max(0.0, gain - max(0.0, rules.long_term_exemption - exemption_used))
        return taxable * rules.long_term_rate
    return gain * rules.short_term_rate


NO_COSTS = CostProfile(name="none", as_of="", source="no charges applied")

_CHARGES_SOURCE = (
    "STT: NSE/FATAX/73524 (31 Mar 2026); NSE transaction charge 0.00307%: NSE/FA/73061 (from 1 Mar 2026); "
    "SEBI fee Rs 10/crore: NSE/FA/40632; stamp duty: Indian Stamp Act Sch. I (from 1 Jul 2020); BSE A/B-group "
    "charge, GST, DP and brokerage: a discount broker's published charges (Zerodha, Oct 2026)"
)

COST_PROFILES: dict[str, CostProfile] = {
    "none": NO_COSTS,
    # Equity delivery at a discount broker that charges no delivery brokerage.
    "india_equity_delivery": CostProfile(
        name="india_equity_delivery", as_of="2026-10-29", source=_CHARGES_SOURCE,
        brokerage_rate=0.0, stt_buy=0.001, stt_sell=0.001,
        exchange_txn_nse=0.0000307, exchange_txn_bse=0.0000375,
        sebi_per_crore=10.0, stamp_buy=0.00015, gst=0.18, dp_per_sell=15.34,
    ),
    # Intraday (bought and sold the same day), brokerage the lower of 0.03% and Rs 20 an order.
    "india_equity_intraday": CostProfile(
        name="india_equity_intraday", as_of="2026-10-29", source=_CHARGES_SOURCE,
        brokerage_rate=0.0003, brokerage_cap=20.0, stt_buy=0.0, stt_sell=0.00025,
        exchange_txn_nse=0.0000307, exchange_txn_bse=0.0000375,
        sebi_per_crore=10.0, stamp_buy=0.00003, gst=0.18, dp_per_sell=0.0,
    ),
}

CAPITAL_GAINS_RULES: dict[str, CapitalGainsRules] = {
    # Listed equity shares sold on an exchange with STT paid, for a resident individual.
    "india_listed_equity": CapitalGainsRules(
        name="india_listed_equity", as_of="2026-10-29",
        source=("Income-tax Act 2025 s.196 (short-term, 20%) and s.198 (long-term, 12.5% above Rs 1.25 lakh "
                "a year), formerly s.111A / s.112A at these rates for transfers from 23 Jul 2024; unchanged by "
                "Budget 2026. Surcharge (capped at 15% for these gains) and 4% cess are not included."),
        long_term_after_months=12, short_term_rate=0.20, long_term_rate=0.125, long_term_exemption=125_000.0,
    ),
}


def _overridden(template, overrides: Mapping | None):
    if not overrides:
        return template
    known = {f.name for f in fields(template)}
    unknown = set(overrides) - known
    if unknown:
        raise ValueError(f"unknown {type(template).__name__} field(s): {', '.join(sorted(unknown))}")
    return replace(template, **dict(overrides))


def get_cost_profile(name: str | None, overrides: Mapping | None = None) -> CostProfile:
    """The named profile (``none`` when unnamed) with ``overrides`` applied."""
    key = (name or "none").strip().lower()
    if key not in COST_PROFILES:
        raise ValueError(f"unknown cost profile {name!r}; choose from {', '.join(COST_PROFILES)}")
    return _overridden(COST_PROFILES[key], overrides)


def get_capital_gains_rules(name: str | None, overrides: Mapping | None = None) -> CapitalGainsRules | None:
    """The named rules with ``overrides`` applied, or None when no rules are named.

    ``long_term_after_months`` counts whole calendar months: 12.0 (say, years x 12) is 12,
    and 12.5 is refused.
    """
    if not name:
        return None
    key = name.strip().lower()
    if key not in CAPITAL_GAINS_RULES:
        raise ValueError(f"unknown capital-gains rules {name!r}; choose from {', '.join(CAPITAL_GAINS_RULES)}")
    rules = _overridden(CAPITAL_GAINS_RULES[key], overrides)
    months = rules.long_term_after_months
    whole = isinstance(months, numbers.Integral) or (isinstance(months, float) and months.is_integer())
    if isinstance(months, bool) or not whole:
        raise ValueError(f"long_term_after_months must be a whole number of months, got {months!r}")
    return rules if type(months) is int else replace(rules, long_term_after_months=int(months))
