"""Risk arithmetic on daily closes and positions: volatility, drawdown, beta, sizing, concentration.

Pure functions of the numbers given; fetching prices is the data layer's job.
Each returns None when it was given too little to say, so a caller reports a
figure as unavailable rather than printing a number computed from nothing.
"""

import math
from collections.abc import Mapping
from dataclasses import dataclass

import pandas as pd

# NSE and BSE trade about 250 days a year; 252 is the usual convention.
TRADING_DAYS_PER_YEAR = 252


def _clean(closes: pd.Series) -> pd.Series:
    series = pd.to_numeric(pd.Series(closes), errors="coerce").dropna()
    return series[series > 0]


def daily_returns(closes: pd.Series) -> pd.Series:
    return _clean(closes).pct_change().dropna()


def annualized_volatility(closes: pd.Series, periods: int = TRADING_DAYS_PER_YEAR,
                          min_returns: int = 20) -> float | None:
    """Standard deviation of daily returns, annualized; None under ``min_returns`` returns."""
    returns = daily_returns(closes)
    if len(returns) < min_returns:
        return None
    return float(returns.std(ddof=1) * math.sqrt(periods))


@dataclass(frozen=True)
class Drawdown:
    depth: float           # the deepest fall from a running peak, as a negative fraction
    peak: object           # index label of the peak before it
    trough: object         # index label of its low


def max_drawdown(closes: pd.Series) -> Drawdown | None:
    """The deepest peak-to-trough fall in the series; None for fewer than two prices."""
    series = _clean(closes)
    if len(series) < 2:
        return None
    running_peak = series.cummax()
    drawdowns = series / running_peak - 1.0
    trough = drawdowns.idxmin()
    peak = series.loc[:trough].idxmax()
    return Drawdown(float(drawdowns.min()), peak, trough)


def total_return(closes: pd.Series, sessions: int | None = None) -> float | None:
    """The return over the last ``sessions`` sessions (all of them by default)."""
    series = _clean(closes)
    if sessions is not None:
        if len(series) <= sessions:
            return None
        series = series.iloc[-(sessions + 1):]
    if len(series) < 2:
        return None
    return float(series.iloc[-1] / series.iloc[0] - 1.0)


def beta(asset_closes: pd.Series, benchmark_closes: pd.Series, min_returns: int = 60) -> float | None:
    """The asset's beta to the benchmark on aligned daily returns; None on too few shared days."""
    joined = pd.concat([daily_returns(asset_closes), daily_returns(benchmark_closes)], axis=1, join="inner").dropna()
    if len(joined) < min_returns:
        return None
    asset, bench = joined.iloc[:, 0], joined.iloc[:, 1]
    variance = bench.var(ddof=1)
    if not variance:
        return None
    return float(asset.cov(bench) / variance)


def shared_closes(asset_closes: pd.Series, benchmark_closes: pd.Series) -> pd.DataFrame:
    """The two series' closes on the dates both have, as two columns; a repeated date keeps its last close."""
    columns = [_clean(closes) for closes in (asset_closes, benchmark_closes)]
    # A repeated date would make the join raise rather than align.
    columns = [series[~series.index.duplicated(keep="last")] for series in columns]
    return pd.concat(columns, axis=1, join="inner")


def relative_return(asset_closes: pd.Series, benchmark_closes: pd.Series, sessions: int) -> float | None:
    """The asset's return minus the benchmark's over the last ``sessions`` sessions both series have.

    Cut from each series' own rows instead, a row only one of them has (a holiday
    filler, a special session, a close published a day late) would start or end one
    window on a different day from the other.
    """
    shared = shared_closes(asset_closes, benchmark_closes)
    asset, bench = total_return(shared.iloc[:, 0], sessions), total_return(shared.iloc[:, 1], sessions)
    return None if asset is None or bench is None else asset - bench


def average_true_range(frame: pd.DataFrame, periods: int = 14) -> float | None:
    """Wilder's ATR over the last ``periods`` sessions of a High/Low/Close frame; None when too short."""
    if frame is None or len(frame) <= periods or not {"High", "Low", "Close"} <= set(frame.columns):
        return None
    high, low, close = (pd.to_numeric(frame[c], errors="coerce") for c in ("High", "Low", "Close"))
    previous = close.shift(1)
    true_range = pd.concat([high - low, (high - previous).abs(), (low - previous).abs()], axis=1).max(axis=1)
    atr = true_range.dropna().ewm(alpha=1 / periods, adjust=False).mean()
    return float(atr.iloc[-1]) if len(atr) and math.isfinite(atr.iloc[-1]) else None


def risk_reward(entry: float, stop: float, target: float) -> float | None:
    """Reward per unit of risk for a long (stop below entry) or short (stop above) trade."""
    if None in (entry, stop, target) or entry == stop:
        return None
    risk = abs(entry - stop)
    long_trade = stop < entry
    reward = (target - entry) if long_trade else (entry - target)
    return float(reward / risk) if reward > 0 else 0.0


@dataclass(frozen=True)
class PositionSize:
    quantity: int
    capital_at_risk: float      # quantity x distance to the stop
    position_value: float       # quantity x entry
    capped_by: str | None       # "max position" when that limit, not the risk budget, set it


def position_size(capital: float, risk_fraction: float, entry: float, stop: float,
                  max_position_fraction: float | None = None, lot_size: int = 1) -> PositionSize | None:
    """Shares to buy so a stop-out loses at most ``risk_fraction`` of ``capital``.

    Whole lots only (equities trade in lots of 1); a ``max_position_fraction``
    caps the position's value. None when the inputs cannot size a trade.
    """
    if not capital or capital <= 0 or not risk_fraction or risk_fraction <= 0 or entry is None or stop is None:
        return None
    per_share_risk = abs(entry - stop)
    if per_share_risk <= 0 or entry <= 0:
        return None
    quantity = int(capital * risk_fraction / per_share_risk) // lot_size * lot_size
    capped_by = None
    if max_position_fraction:
        cap = int(capital * max_position_fraction / entry) // lot_size * lot_size
        if cap < quantity:
            quantity, capped_by = cap, "max position"
    return PositionSize(quantity, quantity * per_share_risk, quantity * entry, capped_by)


@dataclass(frozen=True)
class Concentration:
    largest: str
    largest_weight: float
    hhi: float                  # Herfindahl index: 1 is one holding, 1/n is n equal holdings
    effective_holdings: float   # 1 / HHI


def concentration(values: Mapping[str, float]) -> Concentration | None:
    """How concentrated a book is, from each holding's market value (long side)."""
    positive = {name: value for name, value in values.items() if value and value > 0}
    total = sum(positive.values())
    if not total:
        return None
    weights = {name: value / total for name, value in positive.items()}
    hhi = sum(w * w for w in weights.values())
    largest = max(weights, key=weights.get)
    return Concentration(largest, weights[largest], hhi, 1.0 / hhi)


def exposure_by(values: Mapping[str, float], groups: Mapping[str, str | None]) -> dict[str, float]:
    """Each group's share of the book's total market value (``Unclassified`` for a holding with none)."""
    total = sum(v for v in values.values() if v and v > 0)
    if not total:
        return {}
    shares: dict[str, float] = {}
    for name, value in values.items():
        if value and value > 0:
            group = groups.get(name) or "Unclassified"
            shares[group] = shares.get(group, 0.0) + value / total
    return dict(sorted(shares.items(), key=lambda item: -item[1]))
