"""Day-trading arithmetic: the session a plan is for, the levels a day trader reads, and paper sizing.

Pure, like the rest of ``markets``: the data layer hands over bars and daily rows
already cut off at the analysis time, and nothing here fetches. A figure that
cannot be computed from what was given (too few bars, a session that has not
opened) is None, so the snapshot says it is unavailable instead of printing a
number made from nothing.

Times are the exchange's. Yahoo labels a bar by its start, so a bar ends at its
start plus the interval, cut at the session's close where the exchange is known
(a special session can end part-way through a 15-minute bar).
"""

import math
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo

import pandas as pd

from tradingagents.markets import costs, risk
from tradingagents.markets.exchanges import (
    Exchange,
    exchange_for_symbol,
    holiday_name,
    is_trading_day,
    next_trading_day,
)
from tradingagents.markets.money import format_money

# The bar sizes offered, in minutes. Not 30m: yfinance builds it from 15m bars on
# clock half-hours, so NSE's 09:15 start leaves the first bar 15 minutes long.
INTERVAL_MINUTES: dict[str, int] = {"1m": 1, "5m": 5, "15m": 15}

# Brokers' MIS auto square-off for stocks with derivatives, checked 2026-10-29:
# Upstox and Angel One 15:10, Zerodha and ICICI Direct 15:12 (other stocks
# 15:20-15:25); Groww about 15:05 (it blocks new intraday orders after 15:05;
# unverified, and its help pages still say 15:20). The default is Upstox's and
# Angel One's, not the earliest; set intraday_square_off to your broker's.
DEFAULT_SQUARE_OFF = "15:10"

OPENING_RANGE_MINUTES = (15, 30)
ATR_PERIODS = 14
RECENT_BARS = 12

_PRICE_COLUMNS = ("Open", "High", "Low", "Close")


def tz_label(zone) -> str:
    """``IST`` for India, else the zone's name (``America/New_York``)."""
    name = getattr(zone, "key", None) or str(zone or "")
    return "IST" if name == "Asia/Kolkata" else name


def _zone(tz) -> tzinfo | None:
    if tz is None or isinstance(tz, tzinfo):
        return tz
    return ZoneInfo(str(tz))


def _local(now: datetime, exchange: Exchange | None, tz=None) -> datetime:
    """``now`` on the listing's clock; a naive time is taken as UTC."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    zone = exchange.tz if exchange is not None else _zone(tz)
    return now.astimezone(zone) if zone is not None else now


def _trades_on(exchange: Exchange | None, day: date) -> bool:
    return is_trading_day(exchange, day) if exchange is not None else day.weekday() < 5


def _next_session(exchange: Exchange | None, day: date) -> date:
    if exchange is not None:
        return next_trading_day(exchange, day)
    day += timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def _special(exchange: Exchange | None, day: date) -> str:
    special = exchange.special_sessions.get(day) if exchange is not None else None
    return f" ({special[0]}, {special[2]:%H:%M}-{special[3]:%H:%M} {exchange.tz_label})" if special else ""


def _other_hours(exchange: Exchange | None, day: date) -> bool:
    """Whether ``day``'s session keeps other hours than the exchange's usual (Muhurat trading)."""
    return exchange is not None and exchange.hours_on(day)[1:] != exchange.regular


def target_session(exchange: Exchange | None, as_of: date, now: datetime, *, tz=None,
                   hours: tuple[time, time] | None = None) -> tuple[date, str]:
    """The session a plan made at ``now``, in a run dated ``as_of``, is for, and why.

    A past date plans the next trading day after it: the run knows that day's
    close and nothing later. Today plans today's session while it is open or
    still to open, else the next trading day. NSE and BSE follow their calendars,
    special sessions included; any other listing counts weekdays in ``tz`` (the
    bars' time zone; default ``now``'s own), with ``hours`` its regular session
    when known.
    """
    local = _local(now, exchange, tz)
    today = local.date()
    label = exchange.tz_label if exchange is not None else tz_label(local.tzinfo)
    if as_of < today:
        day = _next_session(exchange, as_of)
        return day, f"the next trading day after the analysis date {as_of:%a %d %b %Y}{_special(exchange, day)}"
    if not _trades_on(exchange, today):
        day = _next_session(exchange, today)
        holiday = holiday_name(exchange, today) if exchange is not None else None
        closed = f"an {exchange.code} holiday ({holiday})" if holiday else "a weekend"
        return day, f"the next trading day{_special(exchange, day)}; today ({today:%a %d %b %Y}) is {closed}"
    if exchange is not None:
        _, open_time, close_time = exchange.hours_on(today)
    elif hours is not None:
        open_time, close_time = hours
    else:
        return today, "today's session (its hours are not known for this listing)"
    special = _special(exchange, today)
    if local.time() < open_time:
        return today, f"today's session{special}, which opens at {open_time:%H:%M} {label}"
    if local.time() < close_time:
        return today, f"the rest of today's session{special}, open until {close_time:%H:%M} {label}"
    day = _next_session(exchange, today)
    return day, f"the next trading day{_special(exchange, day)}; today's session closed at {close_time:%H:%M} {label}"


def square_off_time(exchange: Exchange | None, config: Mapping | None) -> time | None:
    """When intraday positions are closed: NSE/BSE the configured broker square-off, else None (the session close)."""
    if exchange is None:
        return None
    raw = (config or {}).get("intraday_square_off") or DEFAULT_SQUARE_OFF
    if isinstance(raw, time):
        return raw
    try:
        return time.fromisoformat(str(raw).strip())
    except ValueError:
        raise ValueError(f"intraday_square_off must be a time as HH:MM, got {raw!r}") from None


def _since_midnight(clock: time) -> timedelta:
    return timedelta(hours=clock.hour, minutes=clock.minute, seconds=clock.second)


def _session_clock(bars: pd.DataFrame, exchange: Exchange, which: int) -> pd.Series:
    """Each bar's session open (``which`` 1) or close (2) on its own day, in the bars' time zone."""
    offsets = bars["Datetime"].dt.date.map(lambda day: _since_midnight(exchange.hours_on(day)[which]))
    return bars["Datetime"].dt.normalize() + pd.to_timedelta(offsets)


def in_regular_session(bars: pd.DataFrame, exchange: Exchange) -> pd.Series:
    """Whether each bar starts inside its day's regular session (special sessions included)."""
    if bars.empty:
        return pd.Series(dtype=bool, index=bars.index)
    return (bars["Datetime"] >= _session_clock(bars, exchange, 1)) & (bars["Datetime"] < _session_clock(bars, exchange, 2))


def bar_ends(bars: pd.DataFrame, interval: str | int, exchange: Exchange | None = None) -> pd.Series:
    """When each bar ends: its start plus the interval, cut at its session's close where the exchange is known."""
    minutes = INTERVAL_MINUTES[interval] if isinstance(interval, str) else int(interval)
    ends = bars["Datetime"] + pd.Timedelta(minutes=minutes)
    if exchange is None or bars.empty:
        return ends
    closes = _session_clock(bars, exchange, 2)
    return ends.where(ends <= closes, closes)


def infer_session_hours(bars: pd.DataFrame, interval: str) -> tuple[time, time] | None:
    """The regular session the bars show: the usual first bar's start and the latest bar's end.

    For a listing without a known exchange. Pass completed sessions only: a
    session still trading has not reached its close.
    """
    if bars is None or len(bars) == 0:
        return None
    starts = pd.Series(pd.to_datetime(bars["Datetime"]))
    by_day = starts.groupby(starts.dt.date)
    first = by_day.min().dt.time.mode()
    last = (by_day.max() + pd.Timedelta(minutes=INTERVAL_MINUTES[interval])).dt.time.max()
    return (first.iloc[0], last) if len(first) else None


def _clean(bars: pd.DataFrame | None) -> pd.DataFrame:
    """Bars with numeric prices in time order; a row missing a price is dropped, missing volume reads 0."""
    if bars is None or len(bars) == 0 or not {"Datetime", *_PRICE_COLUMNS} <= set(bars.columns):
        return pd.DataFrame(columns=["Datetime", *_PRICE_COLUMNS, "Volume"])
    frame = bars.copy()
    frame["Datetime"] = pd.to_datetime(frame["Datetime"])
    for column in (*_PRICE_COLUMNS, "Volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce") if column in frame else 0.0
    frame["Volume"] = frame["Volume"].fillna(0.0)
    frame = frame.dropna(subset=["Datetime", *_PRICE_COLUMNS])
    frame = frame.sort_values("Datetime").drop_duplicates("Datetime", keep="last")
    return frame[["Datetime", *_PRICE_COLUMNS, "Volume"]].reset_index(drop=True)


def _minutes(frame: pd.DataFrame, interval: str | None) -> int | None:
    """The bar length: the interval given, else the usual gap between bars of one session."""
    if interval:
        return INTERVAL_MINUTES[interval]
    gaps = frame.groupby(frame["Datetime"].dt.date)["Datetime"].diff().dropna()
    gaps = gaps[gaps > pd.Timedelta(0)]
    return int(gaps.mode().iloc[0].total_seconds() // 60) if len(gaps) else None


def _official_gap(daily: pd.DataFrame | None, target: date, before: pd.DataFrame | None) -> dict | None:
    """``target``'s opening price from its daily bar against the daily close before it; None without both."""
    if daily is None or before is None or len(daily) == 0 or "Open" not in daily.columns:
        return None
    dates = pd.to_datetime(daily["Date"]) if "Date" in daily.columns else pd.to_datetime(pd.Series(daily.index))
    opens = pd.to_numeric(daily["Open"], errors="coerce").reset_index(drop=True)[list(dates.dt.date == target)]
    previous = float(before["Close"].iloc[-1])
    if opens.empty or not math.isfinite(float(opens.iloc[0])) or float(opens.iloc[0]) <= 0 or not previous > 0:
        return None
    return {"open": float(opens.iloc[0]), "previous_close": previous, "pct": (float(opens.iloc[0]) / previous - 1) * 100}


def _daily_rows(daily: pd.DataFrame | None, before: date) -> pd.DataFrame | None:
    """Daily rows dated before ``before``, with a ``Day`` column; None when there are none."""
    if daily is None or len(daily) == 0 or not {"High", "Low", "Close"} <= set(daily.columns):
        return None
    dates = pd.to_datetime(daily["Date"]) if "Date" in daily.columns else pd.to_datetime(pd.Series(daily.index))
    rows = daily.reset_index(drop=True).copy()
    rows["Day"] = list(dates.dt.date)
    for column in (*_PRICE_COLUMNS, "Volume"):
        if column in rows:
            rows[column] = pd.to_numeric(rows[column], errors="coerce")
    rows = rows[rows["Day"] < before].dropna(subset=["High", "Low", "Close"]).sort_values("Day")
    return rows.reset_index(drop=True) if len(rows) else None


def _session_summary(rows: pd.DataFrame, day: date, source: str) -> dict:
    return {"date": day, "open": float(rows["Open"].iloc[0]), "high": float(rows["High"].max()),
            "low": float(rows["Low"].min()), "close": float(rows["Close"].iloc[-1]),
            "volume": float(rows["Volume"].sum()), "source": source}


def _pivots(session: dict | None) -> dict | None:
    """Classic floor pivots from a session's high, low and close."""
    if not session:
        return None
    high, low, close = session["high"], session["low"], session["close"]
    pivot = (high + low + close) / 3
    return {"P": pivot, "R1": 2 * pivot - low, "S1": 2 * pivot - high,
            "R2": pivot + (high - low), "S2": pivot - (high - low),
            "R3": high + 2 * (pivot - low), "S3": low - 2 * (high - pivot), "from": session["date"]}


def _minute_of_day(stamps: pd.Series) -> pd.Series:
    return stamps.dt.hour * 60 + stamps.dt.minute


def intraday_levels(bars: pd.DataFrame, daily: pd.DataFrame | None, target: date, now: datetime, *,
                    interval: str | None = None, exchange: Exchange | None = None,
                    cutoff: datetime | None = None) -> dict:
    """What a day trader reads before planning ``target``, from bars already cut off at the analysis time.

    Keys (None where it cannot be computed): ``last_session`` (date, open, high,
    low, close, volume, source) is the latest session before ``target``, from the
    bars or else the daily rows, and ``last_session_through`` its last bar's end
    when the bars' ``cutoff`` (no bar ending later was served) fell before its
    close, so it is not complete; ``current`` (open, high, low, last, vwap, volume,
    bars, through) is ``target`` so far once it has a bar; ``opening_range`` maps
    15 and 30 minutes to their high and low once the bars cover them; ``pivots``
    are classic floor pivots from ``last_session``; ``gap_pct`` is the first bar's
    open against the previous close, and ``official_gap`` (open, previous_close,
    pct) the daily bar's open, the exchange's opening price, against the previous
    daily close; ``relative_volume`` compares the volume so far with the prior
    sessions' average to the same time of day, and ``last_session_relative_volume``
    the last session's with the sessions before it (to the same time, for one not
    complete). Both averages leave out a special session on other hours, such as
    Muhurat trading, which gets no ratio of its own: the usual sessions trade at
    its times of day too, but on other hours its volume cannot be compared like
    for like with theirs. ``atr_intraday`` and ``atr_daily`` are Wilder's ATR(14)
    on the bars and on the daily rows before ``target``; ``last_vs_vwap_pct``;
    ``reference_price`` is the newest close, at ``data_through``. Percentages are
    in percent.
    """
    frame = _clean(bars)
    minutes = _minutes(frame, interval) if len(frame) else (INTERVAL_MINUTES[interval] if interval else None)
    zone = frame["Datetime"].dt.tz if len(frame) else (exchange.tz if exchange is not None else None)
    levels: dict = {
        "interval": interval or (f"{minutes}m" if minutes else None),
        "timezone": getattr(zone, "key", None) or (str(zone) if zone is not None else None),
        "tz_label": tz_label(zone) if zone is not None else "",
        "as_of": _local(now, exchange, zone),
        "target_session": target,
        "target_started": False,
        "sessions": [],
        "bars": len(frame),
        "data_through": None,
        "last_session": None,
        "last_session_through": None,
        "current": None,
        "opening_range": dict.fromkeys(OPENING_RANGE_MINUTES),
        "pivots": None,
        "gap_pct": None,
        "official_gap": None,
        "relative_volume": None,
        "relative_volume_sessions": 0,
        "last_session_relative_volume": None,
        "atr_intraday": None,
        "atr_daily": None,
        "atr_daily_through": None,
        "last_vs_vwap_pct": None,
        "reference_price": None,
    }

    daily_rows = _daily_rows(daily, target)
    if daily_rows is not None:
        levels["atr_daily"] = risk.average_true_range(daily_rows, ATR_PERIODS)
        levels["atr_daily_through"] = daily_rows["Day"].iloc[-1] if levels["atr_daily"] is not None else None

    if len(frame) == 0 or minutes is None:
        if daily_rows is not None:
            levels["last_session"] = _session_summary(daily_rows.tail(1), daily_rows["Day"].iloc[-1], "daily bar")
            levels["pivots"] = _pivots(levels["last_session"])
            levels["reference_price"] = levels["last_session"]["close"]
        return levels

    ends = bar_ends(frame, minutes, exchange)
    end_minutes = _minute_of_day(ends)
    days = frame["Datetime"].dt.date
    session_days = sorted(set(days))
    earlier = [day for day in session_days if day < target]
    # The volume averages leave out a session on other hours than the exchange's usual
    # (Muhurat trading, about an hour in the afternoon): it would read as one that traded
    # little, or nothing by the morning. Nor is such a session given a ratio of its own.
    usual = [day for day in earlier if not _other_hours(exchange, day)]
    levels.update(sessions=session_days, data_through=ends.max(), reference_price=float(frame["Close"].iloc[-1]),
                  atr_intraday=risk.average_true_range(frame, ATR_PERIODS))

    def volume_to(day: date, minute: int | None) -> float:
        """``day``'s volume in the bars ending by ``minute`` of the day (all of them for None)."""
        rows = days == day
        if minute is not None:
            rows &= end_minutes <= minute
        return float(frame.loc[rows, "Volume"].sum())

    source = f"{levels['interval']} bars"
    if earlier:
        last_day = earlier[-1]
        last_rows = frame[days == last_day]
        levels["last_session"] = _session_summary(last_rows, last_day, source)
        # The cut-off can fall before that session's close (a delayed feed just after it):
        # it is then not complete, and its volume is set against the others' to the same time.
        to = None
        if cutoff is not None and exchange is not None:
            close = pd.Timestamp(datetime.combine(last_day, exchange.hours_on(last_day)[2]), tz=zone)
            if _local(cutoff, exchange) < close:
                levels["last_session_through"] = ends[last_rows.index].max()
                to = int(end_minutes[last_rows.index].max())
        totals = [volume_to(day, to) for day in usual if day != last_day]
        average = sum(totals) / len(totals) if totals else 0.0
        if average > 0 and not _other_hours(exchange, last_day):
            levels["last_session_relative_volume"] = levels["last_session"]["volume"] / average
    elif daily_rows is not None:
        levels["last_session"] = _session_summary(daily_rows.tail(1), daily_rows["Day"].iloc[-1], "daily bar")
    levels["pivots"] = _pivots(levels["last_session"])

    today = frame[days == target]
    if len(today) == 0:
        return levels
    through = ends[today.index].max()
    volume = float(today["Volume"].sum())
    typical = (today["High"] + today["Low"] + today["Close"]) / 3
    vwap = float((typical * today["Volume"]).sum() / volume) if volume > 0 else None
    last = float(today["Close"].iloc[-1])
    current = {"date": target, "open": float(today["Open"].iloc[0]), "high": float(today["High"].max()),
               "low": float(today["Low"].min()), "last": last, "vwap": vwap, "volume": volume,
               "bars": len(today), "through": through}
    levels.update(target_started=True, current=current)
    if vwap:
        levels["last_vs_vwap_pct"] = (last / vwap - 1) * 100
    if levels["last_session"]:
        levels["gap_pct"] = (current["open"] / levels["last_session"]["close"] - 1) * 100
    # NSE and BSE open in a pre-open auction, so the first bar (from 09:15) can start
    # away from the opening price the daily bar carries.
    levels["official_gap"] = _official_gap(daily, target, daily_rows)

    opened = (pd.Timestamp(datetime.combine(target, exchange.hours_on(target)[1]), tz=zone)
              if exchange is not None else today["Datetime"].iloc[0])
    for span in OPENING_RANGE_MINUTES:
        window_end = opened + pd.Timedelta(minutes=span)
        window = today[today["Datetime"] < window_end]
        if through >= window_end and len(window):
            levels["opening_range"][span] = {"high": float(window["High"].max()), "low": float(window["Low"].min()),
                                             "start": opened, "end": window_end}

    # Relative volume: the volume so far against what the prior sessions had traded by
    # the same time of day; none for a session on other hours, not like for like with them.
    if _other_hours(exchange, target):
        return levels
    so_far = [volume_to(day, int(_minute_of_day(pd.Series([through])).iloc[0])) for day in usual]
    average = sum(so_far) / len(so_far) if so_far else 0.0
    if average > 0:
        levels.update(relative_volume=volume / average, relative_volume_sessions=len(so_far))
    return levels


def _cost_profile(profile) -> costs.CostProfile:
    return profile if isinstance(profile, costs.CostProfile) else costs.get_cost_profile(profile)


# The charges intraday plans on NSE/BSE are sized and scored with, unless the config names others.
INTRADAY_COST_PROFILE = "india_equity_intraday"


def configured_intraday_costs(config: Mapping | None = None) -> costs.CostProfile:
    """The charges for intraday plans on NSE/BSE: the ``intraday_cost_profile`` that ``config``
    names (default ``india_equity_intraday``) with its ``intraday_cost_overrides`` applied.

    Raises ValueError for an unknown profile or field, as ``costs.get_cost_profile`` does.
    """
    config = config or {}
    return costs.get_cost_profile(config.get("intraday_cost_profile", INTRADAY_COST_PROFILE),
                                  config.get("intraday_cost_overrides"))


def intraday_cost_profile(exchange: Exchange | None, config: Mapping | None = None) -> costs.CostProfile:
    """The charges an intraday plan on ``exchange`` is sized and scored with: the configured
    ones for NSE/BSE, none for other listings, whose charges are not modelled."""
    return configured_intraday_costs(config) if exchange is not None else costs.NO_COSTS


def trade_charges(profile, long: bool, entry_price: float, exit_price: float, shares: int,
                  venue: str | None = None) -> float:
    """Round-trip charges on ``shares`` entered at ``entry_price`` and closed at ``exit_price``.

    A long buys first and sells to close; a short sells first and buys back.
    ``profile`` is a ``CostProfile`` or a profile name (None: no charges).
    """
    if shares <= 0:
        return 0.0
    entry_value, exit_value = entry_price * shares, exit_price * shares
    buy, sell = (entry_value, exit_value) if long else (exit_value, entry_value)
    return costs.round_trip_costs(_cost_profile(profile), buy, sell, venue or "NSE")


def intraday_position(capital: float, risk_pct: float, entry: float, stop: float, *, leverage_cap: float,
                      profile, venue: str | None) -> dict | None:
    """Paper shares for an intraday trade entered at ``entry`` with its stop at ``stop``, and what they tie up.

    The most shares whose stop-out loses at most ``risk_pct`` percent of
    ``capital`` with the round trip's charges included, i.e. floor(risk amount /
    (stop distance + round-trip charges per share)), capped at what ``capital`` x
    ``leverage_cap`` buys. A stop below the entry is a long, above it a short.
    ``profile`` is a ``CostProfile`` or a name (``intraday_cost_profile`` gives
    the configured one). None when the inputs cannot size a trade.
    """
    if leverage_cap is None or not leverage_cap > 0:
        raise ValueError(f"leverage_cap must be positive, got {leverage_cap!r}")
    numbers = (capital, risk_pct, entry, stop)
    if any(value is None or not math.isfinite(value) for value in numbers):
        return None
    if capital <= 0 or risk_pct <= 0 or entry <= 0 or stop <= 0 or entry == stop:
        return None
    cost_profile = _cost_profile(profile)
    long = stop < entry
    risk_amount = capital * risk_pct / 100
    per_share = abs(entry - stop)

    def loss_at_stop(shares: int) -> float:
        return shares * per_share + trade_charges(cost_profile, long, entry, stop, shares, venue)

    # The loss at the stop grows with the share count, so search for the largest
    # count that fits; the tolerance keeps an exact fit from failing on rounding.
    low, high = 0, math.floor(risk_amount / per_share)
    while low < high:
        middle = (low + high + 1) // 2
        if loss_at_stop(middle) <= risk_amount * (1 + 1e-12):
            low = middle
        else:
            high = middle - 1
    by_leverage = math.floor(capital * leverage_cap / entry)
    shares = min(low, by_leverage)
    charges = trade_charges(cost_profile, long, entry, stop, shares, venue)
    exposure = shares * entry
    return {
        "direction": "long" if long else "short",
        "entry": entry,
        "stop": stop,
        "shares": shares,
        "shares_by_risk": low,
        "shares_by_leverage": by_leverage,
        "capped_by": "leverage cap" if by_leverage < low else None,
        "capital": capital,
        "risk_pct": risk_pct,
        "risk_amount": risk_amount,
        "per_share_risk": per_share,
        "exposure": exposure,
        "leverage_cap": leverage_cap,
        "margin": exposure / leverage_cap,
        "price_risk": shares * per_share,
        "charges": charges,            # the round trip out at the stop
        "money_at_risk": shares * per_share + charges,
        "profile": cost_profile.name,
        "venue": venue,
    }


def _price(value) -> str:
    """A price with six significant digits and at least two decimals; n/a when missing."""
    if value is None or not math.isfinite(value):
        return "n/a"
    decimals = 2 if value == 0 else max(2, 5 - math.floor(math.log10(abs(value))))
    whole, _, fraction = f"{value:.{decimals}f}".partition(".")
    return f"{whole}.{fraction.rstrip('0').ljust(2, '0')}"


def _pct(value) -> str:
    return "n/a" if value is None else f"{value:+.2f}%"


def _volume(value) -> str:
    return "n/a" if value is None else f"{value:,.0f}"


def _clock(stamp) -> str:
    return f"{stamp:%H:%M}" if stamp is not None else "n/a"


def _day(value) -> str:
    return f"{value:%a %d %b %Y}" if value is not None else "n/a"


def _bar_table(bars: pd.DataFrame, limit: int) -> list[str]:
    recent = _clean(bars).tail(limit)
    several_days = recent["Datetime"].dt.date.nunique() > 1
    rows = ["| Start | Open | High | Low | Close | Volume |", "|---|---:|---:|---:|---:|---:|"]
    for bar in recent.itertuples(index=False):
        start = f"{bar.Datetime:%d %b %H:%M}" if several_days else f"{bar.Datetime:%H:%M}"
        rows.append(f"| {start} | {_price(bar.Open)} | {_price(bar.High)} | {_price(bar.Low)} | "
                    f"{_price(bar.Close)} | {_volume(bar.Volume)} |")
    return rows


def format_intraday_snapshot(symbol: str, levels: Mapping, target: tuple[date, str], square_off: time | None,
                             sizing_examples: Sequence[Mapping | None], notes: Sequence[str], *,
                             recent_bars: pd.DataFrame | None = None, recent_limit: int = RECENT_BARS,
                             source: str = "Yahoo Finance", fetched_at: datetime | None = None,
                             daily_adjusted: bool = False) -> str:
    """The intraday snapshot as markdown: every number with the bars it came from, then the source-of-truth line.

    ``fetched_at`` is shown only when given: a run dated in the past is not told
    when the data was fetched. ``daily_adjusted`` says the daily rows are
    dividend-adjusted (Yahoo's are, for an instrument that pays dividends), so a
    price taken from them, and the pivots and gaps computed from one, are
    labelled so: on an ex-dividend day they are not the ones that traded.
    """
    adjusted = ", dividend-adjusted" if daily_adjusted else ""
    exchange = exchange_for_symbol(symbol)
    currency = exchange.currency if exchange is not None else None
    label = levels.get("tz_label") or ""
    interval = levels.get("interval") or "intraday"
    bars_name = f"{source} {interval} bars"
    through = levels.get("data_through")
    day, why = target

    data_line = f"- Data: {bars_name} (regular session), through {_day(through)} {_clock(through)} {label}"
    if fetched_at is not None:
        fetched = fetched_at.astimezone(through.tzinfo) if through is not None and through.tzinfo else fetched_at
        data_line += f" (fetched {fetched:%H:%M:%S} {label})"
    sessions = levels.get("sessions") or []
    if sessions:
        data_line += f"; sessions in the bars: {sessions[0]:%d %b} to {sessions[-1]:%d %b %Y} ({len(sessions)})"
    lines = [f"# Intraday snapshot for {symbol}", "", data_line + ".",
             f"- Plan for: {_day(day)}, {why}."]
    if square_off is not None:
        lines.append(f"- Exit by: {square_off:%H:%M} {label}, the configured broker square-off "
                     "(intraday_square_off): intraday positions are closed the same day.")
    else:
        lines.append("- Exit by: the session close (no square-off time is configured for this listing).")

    last = levels.get("last_session")
    partial = levels.get("last_session_through") if last else None
    # Read from the daily rows (the bars hold no earlier session), it carries their adjustment,
    # and so do the pivots from it and the gap at the first bar's open measured against its close.
    last_adjusted = adjusted if last and last["source"] == "daily bar" else ""
    if partial is not None:
        closes = f" {exchange.hours_on(last['date'])[2]:%H:%M}" if exchange is not None else ""
        compared = (f"; its volume is set against the sessions before it to {_clock(partial)}"
                    if levels.get("last_session_relative_volume") is not None else "")
        lines += ["", f"## Last session: {_day(last['date'])}, through {_clock(partial)} {label} only "
                      f"(from the {last['source']})", "",
                  f"The data was cut before its{closes} close, so its bars after {_clock(partial)} are missing and "
                  f"its high, low, close and volume are not final{compared}."]
    else:
        lines += ["", f"## Last complete session: {_day(last['date']) if last else 'n/a'}"
                  + (f" (from the {last['source']}{last_adjusted})" if last else "")]
    if last:
        relative = levels.get("last_session_relative_volume")
        ratio = (f"{relative:.2f}x" if relative is not None
                 else "n/a (a session on other hours)" if _other_hours(exchange, last["date"]) else "n/a")
        lines += ["", "| Open | High | Low | Close | Volume | Volume vs the sessions before it |",
                  "|---:|---:|---:|---:|---:|---:|",
                  f"| {_price(last['open'])} | {_price(last['high'])} | {_price(last['low'])} | "
                  f"{_price(last['close'])} | {_volume(last.get('volume'))} | {ratio} |"]
    else:
        lines.append("Unavailable: no completed session in the bars or the daily rows.")

    current = levels.get("current")
    if current:
        lines += ["", f"## The plan's session so far: {_day(current['date'])}, through "
                      f"{_clock(current['through'])} {label} (from the {bars_name})", "",
                  "| Field | Value |", "|---|---:|",
                  f"| Open | {_price(current['open'])} |", f"| High | {_price(current['high'])} |",
                  f"| Low | {_price(current['low'])} |", f"| Last | {_price(current['last'])} |",
                  f"| VWAP | {_price(current['vwap'])} |",
                  f"| Last vs VWAP | {_pct(levels.get('last_vs_vwap_pct'))} |",
                  f"| Volume | {_volume(current['volume'])} |",
                  f"| Gap at the first bar's open vs the previous close{last_adjusted} | "
                  f"{_pct(levels.get('gap_pct'))} |"]
        official = levels.get("official_gap")
        if official:
            lines.append(f"| Gap at the opening price (daily bar, {_price(official['open'])}) vs the previous daily "
                         f"close{adjusted} ({_price(official['previous_close'])}) | {_pct(official['pct'])} |")
        relative = levels.get("relative_volume")
        if relative is not None:
            lines.append(f"| Relative volume (vs the {levels['relative_volume_sessions']} prior sessions' average "
                         f"to {_clock(current['through'])}) | {relative:.2f}x |")
        elif _other_hours(exchange, current["date"]):
            # The usual sessions trade at these times of day too (Muhurat's hours fall inside
            # theirs); what sets the volumes apart is the hours kept, so the row gives both.
            # "Usual", not "regular": the session line calls a special session's hours its regular ones.
            _, opens, closes = exchange.hours_on(current["date"])
            usual_open, usual_close = exchange.regular
            lines.append(f"| Relative volume | n/a (a special session on other hours: it trades {opens:%H:%M}-"
                         f"{closes:%H:%M}, the usual sessions {usual_open:%H:%M}-{usual_close:%H:%M}, so its "
                         "volume so far cannot be compared like for like with theirs) |")
        else:
            lines.append("| Relative volume | n/a (no prior session with volume in the bars) |")
        for span, found in (levels.get("opening_range") or {}).items():
            value = (f"high {_price(found['high'])}, low {_price(found['low'])}" if found else "not yet formed")
            lines.append(f"| Opening range, first {span} minutes | {value} |")
    else:
        lines += ["", f"## The plan's session ({_day(day)}) has no completed bar yet",
                  "Its open, VWAP, gap, opening range and relative volume do not exist until it trades."]

    pivots = levels.get("pivots")
    lines += ["", "## Classic floor pivots" + (f" from the {pivots['from']:%d %b %Y} high, low and close{last_adjusted}"
                                                 if pivots else "")
              + (f" through {_clock(partial)} (not final)" if pivots and partial is not None else "")]
    if pivots:
        lines += ["", "| R3 | R2 | R1 | P | S1 | S2 | S3 |", "|---:|---:|---:|---:|---:|---:|---:|",
                  "| " + " | ".join(_price(pivots[k]) for k in ("R3", "R2", "R1", "P", "S1", "S2", "S3")) + " |"]
    else:
        lines.append("Unavailable: no completed session to compute them from.")

    daily_through = levels.get("atr_daily_through")
    daily_label = f" through {daily_through:%d %b %Y}" if daily_through else ""
    lines += ["", "## Volatility", "", "| Measure | Value |", "|---|---:|",
              f"| ATR(14) on the {interval} bars | {_price(levels.get('atr_intraday'))} |",
              f"| ATR(14) on daily bars{daily_label} | {_price(levels.get('atr_daily'))} |"]

    sized = [example for example in sizing_examples if example]
    lines += ["", "## Paper sizing examples (not a recommendation; no order is placed)"]
    if sized:
        first = sized[0]
        lines += ["", "| Example | Entry | Stop | Shares | Exposure | Margin | At risk, charges included | "
                      "Round-trip charges |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
        for example in sized:
            capped = " (leverage cap)" if example.get("capped_by") else ""
            lines.append(f"| {example.get('label', example['direction'])} | {_price(example['entry'])} | "
                         f"{_price(example['stop'])} | {example['shares']:,}{capped} | "
                         f"{format_money(example['exposure'], currency)} | {format_money(example['margin'], currency)} | "
                         f"{format_money(example['money_at_risk'], currency)} | "
                         f"{format_money(example['charges'], currency)} |")
        lines.append("")    # a line straight after a table would be read as one of its rows
        lines.append(
            f"Paper capital {format_money(first['capital'], currency)}, risking {first['risk_pct']:g}% a trade "
            f"({format_money(first['risk_amount'], currency)}) with a leverage cap of {first['leverage_cap']:g}x "
            f"(margin = exposure / cap). Shares = floor(risk / (stop distance + round-trip charges per share)), "
            f"capped at capital x leverage cap / entry. Charges are estimates under the '{first['profile']}' "
            f"example profile; verify them with your broker.")
    else:
        lines.append("Unavailable: too few bars for an ATR-based stop, or no price to size from.")

    if recent_bars is not None and len(recent_bars):
        shown = min(recent_limit, len(recent_bars))
        lines += ["", f"## Recent {interval} bars (last {shown}, {label}; labelled by their start)", "",
                  *_bar_table(recent_bars, recent_limit)]

    if notes:
        lines += ["", "## Notes", *[f"- {note}" for note in notes]]

    lines += ["",
              "Use this snapshot as the source of truth for intraday prices, levels, volumes and paper sizes. If "
              "another tool's output conflicts with it, flag the discrepancy rather than inventing a reconciled "
              "number, and do not state an intraday price, level or size that is neither here nor in another "
              "tool's output."]
    return "\n".join(lines)
