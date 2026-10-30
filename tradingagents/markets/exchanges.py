"""The exchanges whose rules the tool knows: listing suffix, time zone, currency, sessions and holidays.

Only an exchange written here gets session and holiday checks. Any other symbol
(a US listing, say) carries no exchange and is handled as before: nothing here
guesses the rules of a market it was not told about.

A day's close is final only when the exchange's closing session ends (NSE and
BSE: a closing auction for stocks with derivatives, then the post-close session,
until 16:00 IST), so a daily bar is complete only after that, not at 15:30.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from tradingagents.markets import india_calendar


@dataclass(frozen=True)
class Exchange:
    """One exchange's rules, as they apply to its equity cash market."""

    code: str
    name: str
    suffix: str                       # Yahoo Finance listing suffix
    yahoo_codes: tuple[str, ...]      # Yahoo's ``exchange`` field for its listings
    timezone: str
    currency: str
    benchmark: str                    # Yahoo symbol of the headline index
    benchmark_name: str
    pre_open: time                    # pre-open order session starts
    regular: tuple[time, time]        # regular (continuous) trading
    close_final: time                 # the day's close is final (closing and post-close sessions end)
    holidays: Mapping[date, str] = field(default_factory=dict)
    holiday_years: frozenset[int] = frozenset()   # years the holiday list covers
    special_sessions: Mapping[date, tuple[str, time, time, time]] = field(default_factory=dict)
    holiday_source: str = ""

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def tz_label(self) -> str:
        return "IST" if self.timezone == "Asia/Kolkata" else self.timezone

    def hours_on(self, day: date) -> tuple[time, time, time]:
        """(pre-open start, regular open, regular close) on ``day``: a special session's, else the usual."""
        special = self.special_sessions.get(day)
        return special[1:] if special else (self.pre_open, *self.regular)

    def session_hours(self, day: date | None = None) -> str:
        _, start, end = self.hours_on(day) if day else (None, *self.regular)
        return f"{start:%H:%M}-{end:%H:%M} {self.tz_label}"


NSE = Exchange(
    code="NSE",
    name="National Stock Exchange of India",
    suffix=".NS",
    yahoo_codes=("NSI", "NSE"),
    timezone="Asia/Kolkata",
    currency="INR",
    benchmark="^NSEI",
    benchmark_name="NIFTY 50",
    pre_open=time(9, 0),
    regular=(time(9, 15), time(15, 30)),
    close_final=time(16, 0),
    holidays=india_calendar.NSE_HOLIDAYS,
    holiday_years=india_calendar.HOLIDAY_YEARS,
    special_sessions=india_calendar.SPECIAL_SESSIONS,
    holiday_source=india_calendar.SOURCE,
)

BSE = Exchange(
    code="BSE",
    name="BSE (Bombay Stock Exchange)",
    suffix=".BO",
    yahoo_codes=("BSE", "BOM"),
    timezone="Asia/Kolkata",
    currency="INR",
    benchmark="^BSESN",
    benchmark_name="BSE SENSEX",
    pre_open=time(9, 0),
    regular=(time(9, 15), time(15, 30)),
    close_final=time(16, 0),
    holidays=india_calendar.BSE_HOLIDAYS,
    holiday_years=india_calendar.HOLIDAY_YEARS,
    special_sessions=india_calendar.SPECIAL_SESSIONS,
    holiday_source=india_calendar.SOURCE,
)

EXCHANGES: dict[str, Exchange] = {exchange.code: exchange for exchange in (NSE, BSE)}


def get_exchange(code: str | None) -> Exchange | None:
    """The exchange named ``code`` (``nse``, ``NSE``), None for no name; an unknown name raises."""
    if code is None or not str(code).strip():
        return None
    key = str(code).strip().upper()
    if key not in EXCHANGES:
        raise ValueError(f"unknown exchange {code!r}; choose from {', '.join(EXCHANGES)}")
    return EXCHANGES[key]


def exchange_for_symbol(symbol: str | None) -> Exchange | None:
    """The exchange a Yahoo symbol trades on, for the listings and indices known here."""
    if not isinstance(symbol, str) or not symbol.strip():
        return None
    s = symbol.strip().upper()
    for exchange in EXCHANGES.values():
        if s.endswith(exchange.suffix):
            return exchange
    from tradingagents.markets.india import INDEX_BY_SYMBOL

    index = INDEX_BY_SYMBOL.get(s)
    return EXCHANGES[index.exchange] if index else None


def holidays_of(exchange: Exchange, extra: Mapping | None = None) -> dict[date, str]:
    """The exchange's holidays, with any the caller adds (``{"2027-01-26": "Republic Day"}``)."""
    days = dict(exchange.holidays)
    for day, name in (extra or {}).items():
        days[day if isinstance(day, date) else date.fromisoformat(str(day))] = str(name)
    return days


def holiday_name(exchange: Exchange, day: date, extra: Mapping | None = None) -> str | None:
    return holidays_of(exchange, extra).get(day)


def is_trading_day(exchange: Exchange, day: date, extra: Mapping | None = None) -> bool:
    """A special session, or a weekday that is not a listed holiday. Outside the
    years the list covers only weekends are known, so a holiday there reads as a trading day."""
    if day in exchange.special_sessions:
        return True
    return day.weekday() < 5 and holiday_name(exchange, day, extra) is None


def previous_trading_day(exchange: Exchange, day: date, extra: Mapping | None = None) -> date:
    """The last trading day strictly before ``day``."""
    candidate = day - timedelta(days=1)
    while not is_trading_day(exchange, candidate, extra):
        candidate -= timedelta(days=1)
    return candidate


def next_trading_day(exchange: Exchange, day: date, extra: Mapping | None = None) -> date:
    """The first trading day strictly after ``day``."""
    candidate = day + timedelta(days=1)
    while not is_trading_day(exchange, candidate, extra):
        candidate += timedelta(days=1)
    return candidate


def local_now(exchange: Exchange, now: datetime | None = None) -> datetime:
    """``now`` (default: the clock) in the exchange's time zone; a naive time is taken as UTC."""
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return now.astimezone(exchange.tz)


@dataclass(frozen=True)
class SessionStatus:
    """Where an exchange's trading day stands at one moment."""

    exchange: Exchange
    local_time: datetime
    state: str                   # "closed", "pre-open", "open" or "closing"
    reason: str                  # why it is in that state
    session_complete: bool       # the day's close is final (a trading day only)
    last_complete_session: date  # the latest day whose close is final
    next_open: date              # the next day a regular session starts (today if still to come)
    calendar_known: bool         # the holiday list covers this year

    def describe(self) -> str:
        """One line for a person or a prompt."""
        ex = self.exchange
        today = self.local_time.date()
        when = f"{self.local_time:%a %d %b %Y, %H:%M} {ex.tz_label}"
        hours = ex.session_hours(today) if is_trading_day(ex, today) else ex.session_hours()
        line = f"{ex.code} is {self.state} ({self.reason}) at {when}; regular session {hours}."
        if self.state in ("pre-open", "open", "closing"):
            line += (" Today's daily bar is still forming, so today's prices are intraday, not a close; "
                     f"the close is final after {ex.close_final:%H:%M} {ex.tz_label}.")
        line += f" The latest complete session is {self.last_complete_session:%a %d %b %Y}."
        if not self.calendar_known:
            line += (f" The {ex.code} holiday list here does not cover {today.year}, so only weekends "
                     f"are known to be closed; check the exchange's holiday circular.")
        return line


def session_status(exchange: Exchange, now: datetime | None = None, extra: Mapping | None = None) -> SessionStatus:
    """The exchange's state at ``now``: weekend, holiday, before the open, pre-open, open, closing or after."""
    local = local_now(exchange, now)
    today, clock = local.date(), local.time()
    trading_day = is_trading_day(exchange, today, extra)
    pre_open, open_time, close_time = exchange.hours_on(today)
    special = exchange.special_sessions.get(today)
    if not trading_day:
        holiday = holiday_name(exchange, today, extra)
        state, reason = "closed", (f"exchange holiday: {holiday}" if holiday else "weekend")
    elif clock < pre_open:
        state, reason = "closed", "before the pre-open session"
    elif clock < open_time:
        state, reason = "pre-open", "pre-open order session"
    elif clock < close_time:
        state, reason = "open", special[0] if special else "regular session"
    elif clock < exchange.close_final:
        state, reason = "closing", "closing auction and post-close session"
    else:
        state, reason = "closed", "after the close"
    complete = trading_day and clock >= exchange.close_final
    last_complete = today if complete else previous_trading_day(exchange, today, extra)
    next_open = today if trading_day and clock < open_time else next_trading_day(exchange, today, extra)
    return SessionStatus(exchange, local, state, reason, complete, last_complete, next_open,
                         today.year in exchange.holiday_years)


def expected_last_session(exchange: Exchange, as_of: date, now: datetime | None = None,
                          extra: Mapping | None = None) -> date:
    """The session whose close a complete daily series should end on, for a run dated ``as_of``.

    A past date expects its own session (or the one before, if it did not
    trade); today expects today's only once its close is final.
    """
    status = session_status(exchange, now, extra)
    if as_of >= status.local_time.date():
        return status.last_complete_session
    return as_of if is_trading_day(exchange, as_of, extra) else previous_trading_day(exchange, as_of, extra)


def day_status(exchange: Exchange, day: date, extra: Mapping | None = None) -> str:
    """Whether ``day`` was a trading day, for a run dated then."""
    special = exchange.special_sessions.get(day)
    if special:
        return (f"{day:%a %d %b %Y} was an {exchange.code} special session ({special[0]}, "
                f"{special[2]:%H:%M}-{special[3]:%H:%M} {exchange.tz_label}).")
    if day.weekday() >= 5:
        return f"{day:%a %d %b %Y} was a weekend: {exchange.code} did not trade."
    holiday = holiday_name(exchange, day, extra)
    if holiday:
        return f"{day:%a %d %b %Y} was an {exchange.code} holiday ({holiday}): the exchange did not trade."
    known = day.year in exchange.holiday_years
    return (f"{day:%a %d %b %Y} was an {exchange.code} trading day."
            if known else
            f"{day:%a %d %b %Y} is a weekday; the {exchange.code} holiday list here does not cover {day.year}.")
