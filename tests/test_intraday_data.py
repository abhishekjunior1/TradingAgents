"""Intraday bars from Yahoo, served only up to the analysis cut-off, and the cache behind them.

yfinance is stubbed: ``Ticker(...).history`` returns synthetic bars and records
what it was asked, and the clock is pinned, so no test reaches Yahoo or depends
on today.
"""

import os
from datetime import UTC, date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.vendors.yahoo import common, intraday as yahoo_intraday

IST = ZoneInfo("Asia/Kolkata")
NEW_YORK = ZoneInfo("America/New_York")


def _session(day: str, zone=IST, start: str = "09:15", end: str = "15:30", minutes: int = 5,
             price: float = 100.0, volume: int = 1000) -> pd.DataFrame:
    """One session of bars as yfinance returns them: a tz-aware ``Datetime`` index, OHLC, Adj Close, Volume."""
    index = pd.date_range(f"{day} {start}", f"{day} {end}", freq=f"{minutes}min", inclusive="left", tz=zone,
                          name="Datetime")
    steps = pd.Series(range(len(index)), dtype=float).to_numpy() * 0.01
    close = price + steps
    return pd.DataFrame({"Open": close, "High": close + 0.5, "Low": close - 0.5, "Close": close,
                         "Adj Close": close, "Volume": volume}, index=index)


def _daily(end: str = "2026-10-28", rows: int = 30) -> pd.DataFrame:
    dates = pd.bdate_range(end=end, periods=rows)
    return pd.DataFrame({"Date": dates, "Open": 100.0, "High": 102.0, "Low": 98.0, "Close": 100.0, "Volume": 1})


@pytest.fixture
def yahoo(tmp_path, monkeypatch):
    """Stubbed Yahoo: set ``frame`` (what history returns) and ``now`` (the clock); ``calls`` records requests."""
    state = SimpleNamespace(frame=None, now=None, calls=[], config={"data_cache_dir": str(tmp_path)},
                            cache=tmp_path / "intraday")

    def ticker(symbol):
        def history(**kwargs):
            state.calls.append((symbol, kwargs))
            return state.frame.copy()
        return SimpleNamespace(history=history)

    monkeypatch.setattr(yahoo_intraday.yf, "Ticker", ticker)
    monkeypatch.setattr(yahoo_intraday, "get_config", lambda: dict(state.config))
    monkeypatch.setattr(yahoo_intraday, "_now", lambda: state.now)
    monkeypatch.setattr(yahoo_intraday, "load_ohlcv", lambda symbol, as_of_date, fill_gaps=True: _daily())
    return state


def _ist(day: str, clock: str) -> datetime:
    return pd.Timestamp(f"{day} {clock}", tz=IST).to_pydatetime()


THREE_DAYS = ["2026-10-27", "2026-10-28"]


# --- the cut-off -------------------------------------------------------------------

@pytest.mark.unit
def test_the_bar_still_forming_is_left_out(yahoo):
    yahoo.now = _ist("2026-10-29", "10:07")
    yahoo.frame = pd.concat([*(_session(d) for d in THREE_DAYS), _session("2026-10-29", end="10:10")])
    bars = yahoo_intraday.load_intraday("RELIANCE.NS", "2026-10-29")

    assert list(bars.columns) == ["Datetime", "Open", "High", "Low", "Close", "Volume"]
    # The 10:05 bar ends at 10:10, after now.
    assert bars["Datetime"].iloc[-1] == pd.Timestamp("2026-10-29 10:00", tz=IST)
    assert bars.attrs["live"] is True and bars.attrs["timezone"] == "Asia/Kolkata"
    assert bars.attrs["cutoff"] == "2026-10-29T10:07:00+05:30"


@pytest.mark.unit
def test_a_past_date_is_cut_at_that_sessions_close_and_nothing_after_it_is_served(yahoo):
    yahoo.now = _ist("2026-10-29", "11:00")
    after_hours = _session("2026-10-28", start="15:30", end="16:00")      # never part of the regular session
    yahoo.frame = pd.concat([_session("2026-10-23"), *(_session(d) for d in THREE_DAYS), after_hours,
                             _session("2026-10-29", end="11:00")])
    bars = yahoo_intraday.load_intraday("RELIANCE.NS", "2026-10-28")

    assert bars["Datetime"].dt.date.max() == date(2026, 10, 28)
    assert bars["Datetime"].iloc[-1] == pd.Timestamp("2026-10-28 15:25", tz=IST)   # ends at the 15:30 close
    assert bars.attrs["live"] is False and bars.attrs["cutoff"] == "2026-10-28T15:30:00+05:30"
    # An earlier date never sees the bars after it, though the download holds them.
    friday = yahoo_intraday.load_intraday("RELIANCE.NS", "2026-10-23")
    assert set(friday["Datetime"].dt.date) == {date(2026, 10, 23)}
    assert friday["Datetime"].iloc[-1] == pd.Timestamp("2026-10-23 15:25", tz=IST)


@pytest.mark.unit
def test_rows_without_prices_are_dropped_and_volume_0_rows_are_kept(yahoo):
    yahoo.now = _ist("2026-10-29", "11:00")
    session = _session("2026-10-28")
    session.iloc[0, session.columns.get_loc("Volume")] = 0                    # NSE's 09:15 bar
    session.iloc[5, [session.columns.get_loc(c) for c in ("Open", "High", "Low", "Close")]] = float("nan")
    yahoo.frame = session
    bars = yahoo_intraday.load_intraday("RELIANCE.NS", "2026-10-28")
    assert len(bars) == 74 and bars["Volume"].iloc[0] == 0
    assert pd.Timestamp("2026-10-28 09:40", tz=IST) not in set(bars["Datetime"])


@pytest.mark.unit
def test_only_the_last_sessions_asked_for_are_served(yahoo):
    yahoo.now = _ist("2026-10-29", "11:00")
    yahoo.frame = pd.concat([_session(d) for d in ("2026-10-26", *THREE_DAYS)])
    bars = yahoo_intraday.load_intraday("RELIANCE.NS", "2026-10-28", sessions=2)
    assert sorted(set(bars["Datetime"].dt.date)) == [date(2026, 10, 27), date(2026, 10, 28)]


@pytest.mark.unit
def test_a_delayed_feeds_newest_bars_are_left_out(yahoo):
    # Yahoo lists BSE as 15 minutes delayed: at 10:30 the bars through 10:15 are complete.
    yahoo.now = _ist("2026-10-29", "10:30")
    yahoo.frame = _session("2026-10-29", end="10:20")
    bars = yahoo_intraday.load_intraday("RELIANCE.BO", "2026-10-29")
    assert bars["Datetime"].iloc[-1] == pd.Timestamp("2026-10-29 10:10", tz=IST)


# --- validation and Yahoo's window -------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("interval", ["30m", "1h", "1d", "../x"])
def test_only_the_offered_intervals_are_accepted(yahoo, interval):
    yahoo.now = _ist("2026-10-29", "11:00")
    with pytest.raises(ValueError, match="interval must be one of 1m, 5m, 15m"):
        yahoo_intraday.load_intraday("RELIANCE.NS", "2026-10-28", interval=interval)
    with pytest.raises(ValueError, match="sessions must be at least 1"):
        yahoo_intraday.load_intraday("RELIANCE.NS", "2026-10-28", sessions=0)
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        yahoo_intraday.load_intraday("RELIANCE.NS", "28/10/2026")
    assert yahoo.calls == []


@pytest.mark.unit
@pytest.mark.parametrize("interval,as_of,days", [("5m", "2026-08-30", 60), ("15m", "2026-06-01", 60),
                                                 ("1m", "2026-10-22", 7)])
def test_a_date_beyond_yahoos_window_is_unavailable_and_never_requested(yahoo, interval, as_of, days):
    yahoo.now = _ist("2026-10-29", "11:00")
    with pytest.raises(yahoo_intraday.IntradayWindowError) as raised:
        yahoo_intraday.load_intraday("RELIANCE.NS", as_of, interval=interval)
    assert (f"Yahoo serves {interval} bars for about the last {days} days; {as_of} is outside that window "
            "(unavailable, not an absence of trading)") in str(raised.value)
    assert isinstance(raised.value, NoMarketDataError) and yahoo.calls == []


@pytest.mark.unit
@pytest.mark.parametrize("interval,as_of,start", [("5m", "2026-08-31", "2026-08-31"), ("1m", "2026-10-23", "2026-10-23")])
def test_the_download_asks_for_the_whole_window_of_regular_session_bars(yahoo, interval, as_of, start):
    yahoo.now = _ist("2026-10-29", "11:00")
    yahoo.frame = _session(as_of, minutes=int(interval[:-1]))
    yahoo_intraday.load_intraday("RELIANCE.NS", as_of, interval=interval)
    symbol, kwargs = yahoo.calls[0]
    assert symbol == "RELIANCE.NS"
    assert kwargs == {"start": start, "end": "2026-10-30", "interval": interval, "prepost": False,
                      "auto_adjust": False, "actions": False}


# --- the cache ---------------------------------------------------------------------

@pytest.mark.unit
def test_one_cache_file_per_symbol_and_interval(yahoo):
    yahoo.now = _ist("2026-10-29", "11:00")
    yahoo.frame = _session("2026-10-28")
    yahoo_intraday.load_intraday("NSE:RELIANCE", "2026-10-28")
    yahoo.frame = _session("2026-10-28", minutes=15)
    yahoo_intraday.load_intraday("RELIANCE.NS", "2026-10-28", interval="15m")
    assert sorted(p.name for p in yahoo.cache.iterdir()) == ["RELIANCE.NS-YFin-15m.csv", "RELIANCE.NS-YFin-5m.csv"]
    written = datetime.fromtimestamp(os.path.getmtime(yahoo.cache / "RELIANCE.NS-YFin-5m.csv"), UTC)
    assert written == yahoo.now      # stamped with the moment of the download


@pytest.mark.unit
def test_a_current_session_request_refetches_once_the_file_is_older_than_the_ttl(yahoo):
    yahoo.frame = pd.concat([_session("2026-10-28"), _session("2026-10-29", end="10:10")])
    yahoo.now = _ist("2026-10-29", "10:07")
    yahoo_intraday.load_intraday("RELIANCE.NS", "2026-10-29")
    yahoo.now = _ist("2026-10-29", "10:07:40")
    yahoo_intraday.load_intraday("RELIANCE.NS", "2026-10-29")
    assert len(yahoo.calls) == 1, "a 40-second-old file serves the session still trading"
    yahoo.now = _ist("2026-10-29", "10:08:30")
    yahoo_intraday.load_intraday("RELIANCE.NS", "2026-10-29")
    assert len(yahoo.calls) == 2, "past the 60-second TTL the session still trading is refetched"
    yahoo.config["intraday_cache_ttl_seconds"] = 5
    yahoo.now = _ist("2026-10-29", "10:08:40")
    yahoo_intraday.load_intraday("RELIANCE.NS", "2026-10-29")
    assert len(yahoo.calls) == 3, "the TTL is read from config"


@pytest.mark.unit
def test_a_cached_forming_bar_is_not_served_once_it_would_have_ended(yahoo):
    # Downloaded at 10:09:30, when the 10:05 bar held only part of its trades.
    yahoo.frame = pd.concat([_session("2026-10-28"), _session("2026-10-29", end="10:10")])
    yahoo.now = _ist("2026-10-29", "10:09:30")
    yahoo_intraday.load_intraday("RELIANCE.NS", "2026-10-29")
    yahoo.now = _ist("2026-10-29", "10:10:10")
    bars = yahoo_intraday.load_intraday("RELIANCE.NS", "2026-10-29")
    assert len(yahoo.calls) == 1
    assert bars["Datetime"].iloc[-1] == pd.Timestamp("2026-10-29 10:00", tz=IST)


@pytest.mark.unit
def test_an_explicit_earlier_now_is_honoured_from_a_later_download(yahoo):
    yahoo.frame = pd.concat([_session("2026-10-28"), _session("2026-10-29", end="10:10")])
    yahoo.now = _ist("2026-10-29", "10:07")
    yahoo_intraday.load_intraday("RELIANCE.NS", "2026-10-29")
    bars = yahoo_intraday.load_intraday("RELIANCE.NS", "2026-10-29", now=_ist("2026-10-29", "09:31"))
    assert len(yahoo.calls) == 1
    assert bars["Datetime"].iloc[-1] == pd.Timestamp("2026-10-29 09:25", tz=IST)   # ended 09:30, by 09:31


@pytest.mark.unit
def test_a_past_date_reuses_a_file_written_after_its_close_for_the_rest_of_the_day(yahoo):
    yahoo.frame = pd.concat([*(_session(d) for d in THREE_DAYS), _session("2026-10-29", end="11:00")])
    yahoo.now = _ist("2026-10-29", "11:00")
    first = yahoo_intraday.load_intraday("RELIANCE.NS", "2026-10-28")
    yahoo.now = _ist("2026-10-29", "13:00")
    second = yahoo_intraday.load_intraday("RELIANCE.NS", "2026-10-28")
    assert len(yahoo.calls) == 1
    pd.testing.assert_frame_equal(first, second, check_dtype=False)
    yahoo.now = _ist("2026-10-30", "09:00")                                # a new day: refetched
    yahoo_intraday.load_intraday("RELIANCE.NS", "2026-10-28")
    assert len(yahoo.calls) == 2


@pytest.mark.unit
def test_before_the_open_the_previous_sessions_need_no_refetch(yahoo):
    yahoo.frame = pd.concat([_session(d) for d in THREE_DAYS])
    yahoo.now = _ist("2026-10-29", "08:00")
    bars = yahoo_intraday.load_intraday("RELIANCE.NS", "2026-10-29")
    assert bars["Datetime"].dt.date.max() == date(2026, 10, 28) and bars.attrs["live"] is False
    yahoo.now = _ist("2026-10-29", "09:10")                                # pre-open: nothing new to see
    yahoo_intraday.load_intraday("RELIANCE.NS", "2026-10-29")
    assert len(yahoo.calls) == 1


@pytest.mark.unit
def test_an_empty_download_is_never_cached(yahoo, monkeypatch):
    monkeypatch.setattr(common, "vendor_reachable", lambda url: True)
    yahoo.now = _ist("2026-10-29", "11:00")
    yahoo.frame = pd.DataFrame()
    with pytest.raises(NoMarketDataError, match="no 5m bars"):
        yahoo_intraday.load_intraday("RELIANCE.NS", "2026-10-28")
    assert not (yahoo.cache / "RELIANCE.NS-YFin-5m.csv").exists()
    # A poisoned (empty) file left behind is a miss, not an answer.
    (yahoo.cache / "RELIANCE.NS-YFin-5m.csv").write_text("")
    yahoo.frame = _session("2026-10-28")
    assert len(yahoo_intraday.load_intraday("RELIANCE.NS", "2026-10-28")) == 75 and len(yahoo.calls) == 2


@pytest.mark.unit
def test_a_price_of_zero_is_no_price(yahoo):
    # Yahoo sometimes prints 0 for a price (yfinance's repair is off): such a bar is dropped, downloaded or cached.
    yahoo.now = _ist("2026-10-29", "11:00")
    wednesday = _session("2026-10-28")
    wednesday.iloc[3, wednesday.columns.get_loc("Open")] = 0.0
    wednesday.iloc[-1, wednesday.columns.get_loc("Close")] = 0.0
    yahoo.frame = pd.concat([_session("2026-10-27"), wednesday])
    bars = yahoo_intraday.load_intraday("RELIANCE.NS", "2026-10-28")
    assert len(bars) == 148 and (bars[["Open", "High", "Low", "Close"]] > 0).all().all()
    assert pd.Timestamp("2026-10-28 09:30", tz=IST) not in set(bars["Datetime"])
    # A file written before 0 counted as missing: its 0 rows are dropped as it is read.
    path = yahoo.cache / "RELIANCE.NS-YFin-5m.csv"
    stamp = os.path.getmtime(path)
    cached = pd.read_csv(path)
    cached.loc[0, "Low"] = 0.0
    cached.to_csv(path, index=False)
    os.utime(path, (stamp, stamp))
    again = yahoo_intraday.load_intraday("RELIANCE.NS", "2026-10-28")
    assert len(yahoo.calls) == 1 and len(again) == 147 and (again["Low"] > 0).all()


@pytest.mark.unit
def test_a_previous_close_of_zero_does_not_cost_the_snapshot(yahoo):
    yahoo.now = _ist("2026-10-29", "10:07")
    wednesday = _session("2026-10-28")
    wednesday.iloc[-1, wednesday.columns.get_loc("Close")] = 0.0
    yahoo.frame = pd.concat([_session("2026-10-27"), wednesday, _session("2026-10-29", end="10:10")])
    text = yahoo_intraday.get_intraday_bars("RELIANCE.NS", "2026-10-29")     # was a division by zero
    assert text.startswith("# Intraday snapshot for RELIANCE.NS")
    assert "| Gap at the open vs the previous close |" in text


@pytest.mark.unit
def test_no_bars_up_to_the_cut_off_is_no_data(yahoo):
    yahoo.now = _ist("2026-10-29", "11:00")
    yahoo.frame = _session("2026-10-29", end="11:00")
    with pytest.raises(NoMarketDataError, match="no 5m bars on or before 2026-10-28"):
        yahoo_intraday.load_intraday("RELIANCE.NS", "2026-10-28")


# --- time zones --------------------------------------------------------------------

@pytest.mark.unit
def test_new_york_bars_keep_new_yorks_dates_for_a_run_dated_in_india(yahoo):
    # 08:00 IST on Friday 30 Oct is 22:30 on Thursday in New York.
    yahoo.now = _ist("2026-10-30", "08:00")
    yahoo.frame = pd.concat([_session(d, zone=NEW_YORK, start="09:30", end="16:00") for d in THREE_DAYS + ["2026-10-29"]])
    for as_of in ("2026-10-30", "2026-10-29"):
        bars = yahoo_intraday.load_intraday("AAPL", as_of)
        assert str(bars["Datetime"].dt.tz) == "America/New_York"
        assert bars["Datetime"].dt.date.max() == date(2026, 10, 29)
        assert bars["Datetime"].iloc[-1] == pd.Timestamp("2026-10-29 15:55", tz=NEW_YORK)
    assert len(yahoo.calls) == 1, "the second request is served from the cache, in New York time"
    past = yahoo_intraday.load_intraday("AAPL", "2026-10-28")
    assert past["Datetime"].iloc[-1] == pd.Timestamp("2026-10-28 15:55", tz=NEW_YORK)


@pytest.mark.unit
def test_the_cache_keeps_local_times_across_a_daylight_saving_change(yahoo):
    # New York leaves daylight saving on Sunday 1 Nov 2026.
    yahoo.frame = pd.concat([_session(d, zone=NEW_YORK, start="09:30", end="16:00") for d in ("2026-10-30", "2026-11-02")])
    yahoo.now = pd.Timestamp("2026-11-02 12:00", tz=NEW_YORK).to_pydatetime()
    downloaded = yahoo_intraday.load_intraday("AAPL", "2026-11-02")
    yahoo.now = pd.Timestamp("2026-11-02 12:00:30", tz=NEW_YORK).to_pydatetime()
    cached = yahoo_intraday.load_intraday("AAPL", "2026-11-02")
    assert len(yahoo.calls) == 1
    for bars in (downloaded, cached):
        firsts = bars.groupby(bars["Datetime"].dt.date)["Datetime"].min()
        assert [f"{stamp:%H:%M}" for stamp in firsts] == ["09:30", "09:30"]
        assert bars["Datetime"].iloc[-1] == pd.Timestamp("2026-11-02 11:55", tz=NEW_YORK)


# --- the snapshot ------------------------------------------------------------------

@pytest.mark.unit
def test_the_snapshot_of_a_session_in_progress(yahoo):
    yahoo.now = _ist("2026-10-29", "10:07")
    yahoo.frame = pd.concat([*(_session(d) for d in THREE_DAYS), _session("2026-10-29", end="10:10")])
    text = yahoo_intraday.get_intraday_bars("RELIANCE.NS", "2026-10-29")
    assert text.startswith("# Intraday snapshot for RELIANCE.NS")
    assert "through Thu 29 Oct 2026 10:05 IST (fetched 10:07:00 IST)" in text
    assert "Plan for: Thu 29 Oct 2026, the rest of today's session, open until 15:30 IST." in text
    assert "Exit by: 15:10 IST" in text and "NSE is open (regular session)" in text
    assert "Bars still forming are left out" in text
    assert "Long at the newest close, stop 1 x ATR below" in text and "'india_equity_intraday'" in text
    assert "source of truth for intraday" in text


@pytest.mark.unit
def test_a_run_dated_today_is_told_todays_state_before_the_open_and_in_the_closing_session(yahoo):
    # The download holds all of Thursday; at 09:05 none of it has traded yet.
    yahoo.frame = pd.concat([*(_session(d) for d in THREE_DAYS), _session("2026-10-29")])
    yahoo.now = _ist("2026-10-29", "09:05")
    early = yahoo_intraday.get_intraday_bars("RELIANCE.NS", "2026-10-29")
    assert "through Wed 28 Oct 2026 15:30 IST;" in early and "29 Oct 09:15" not in early
    assert "NSE is pre-open (pre-open order session)" in early and "was an NSE trading day" not in early
    assert "Plan for: Thu 29 Oct 2026, today's session, which opens at 09:15 IST." in early
    assert "square-off time" not in early
    # Past the 15:10 square-off the session still trades, but no new intraday position can be opened.
    yahoo.now = _ist("2026-10-29", "15:20")
    late = yahoo_intraday.get_intraday_bars("RELIANCE.NS", "2026-10-29")
    assert "the rest of today's session" in late and "Today's square-off time (15:10 IST) has passed" in late
    yahoo.now = _ist("2026-10-29", "15:45")
    closing = yahoo_intraday.get_intraday_bars("RELIANCE.NS", "2026-10-29")
    assert "through Thu 29 Oct 2026 15:30 IST (fetched 15:45:00 IST)" in closing
    assert "NSE is closing (closing auction and post-close session)" in closing
    assert "Bars still forming" not in closing and "Plan for: Fri 30 Oct 2026, the next trading day" in closing


@pytest.mark.unit
def test_a_past_dated_snapshot_names_no_moment_after_its_date(yahoo):
    yahoo.now = _ist("2026-10-29", "11:00")
    yahoo.frame = pd.concat([_session(d) for d in ("2026-10-21", "2026-10-22", "2026-10-23", "2026-10-26")])
    text = yahoo_intraday.get_intraday_bars("RELIANCE.NS", "2026-10-23")
    assert "through Fri 23 Oct 2026 15:30 IST" in text and "fetched" not in text
    assert "Plan for: Mon 26 Oct 2026, the next trading day after the analysis date Fri 23 Oct 2026." in text
    assert "29 Oct" not in text and "2026-10-29" not in text and "26 Oct 09:15" not in text


@pytest.mark.unit
def test_a_past_dated_snapshot_is_not_told_that_todays_square_off_has_passed(yahoo):
    # Dated Wed 28 Oct and made after 15:10 on Thu 29 Oct, the session it plans as of Wednesday's close.
    yahoo.now = _ist("2026-10-29", "16:30")
    yahoo.frame = pd.concat([*(_session(d) for d in THREE_DAYS), _session("2026-10-29")])
    text = yahoo_intraday.get_intraday_bars("RELIANCE.NS", "2026-10-28")
    assert "Plan for: Thu 29 Oct 2026, the next trading day after the analysis date Wed 28 Oct 2026." in text
    assert "square-off time" not in text and "cannot be acted on" not in text


@pytest.mark.unit
def test_paper_sizing_reads_its_config_and_a_given_capital_wins(yahoo, monkeypatch):
    yahoo.now = _ist("2026-10-29", "11:00")
    yahoo.frame = pd.concat([_session(d) for d in THREE_DAYS])
    yahoo.config.update(intraday_paper_capital=200_000, intraday_risk_pct=1.0, intraday_leverage_cap=5.0)
    text = yahoo_intraday.get_intraday_bars("RELIANCE.NS", "2026-10-28")
    assert "Paper capital ₹2,00,000.00, risking 1% a trade (₹2,000.00) with a leverage cap of 5x" in text
    assert "Paper capital ₹5,00,000.00" in yahoo_intraday.get_intraday_bars("RELIANCE.NS", "2026-10-28",
                                                                           capital=500_000)
    # Without daily bars the daily ATR is unavailable and the snapshot says why.
    def no_daily_bars(*args, **kwargs):
        raise NoMarketDataError("RELIANCE.NS", detail="no price rows")

    monkeypatch.setattr(yahoo_intraday, "load_ohlcv", no_daily_bars)
    text = yahoo_intraday.get_intraday_bars("RELIANCE.NS", "2026-10-28")
    assert "| ATR(14) on daily bars | n/a |" in text and "Daily bars unavailable" in text


@pytest.mark.unit
def test_paper_sizing_counts_the_configured_charges(yahoo):
    yahoo.now = _ist("2026-10-29", "11:00")
    yahoo.frame = pd.concat([_session(d) for d in THREE_DAYS])

    def rows(text):
        return [line for line in text.splitlines() if line.startswith(("| Long at", "| Short at"))]

    example = yahoo_intraday.get_intraday_bars("RELIANCE.NS", "2026-10-28")
    assert "under the 'india_equity_intraday' example profile" in example
    assert rows(example) and not any(row.endswith("| ₹0.00 |") for row in rows(example))
    yahoo.config.update(intraday_cost_profile="none")
    nothing = yahoo_intraday.get_intraday_bars("RELIANCE.NS", "2026-10-28")
    assert "under the 'none' example profile" in nothing
    assert len(rows(nothing)) == 4 and all(row.endswith("| ₹0.00 |") for row in rows(nothing))


@pytest.mark.unit
def test_the_snapshot_says_where_its_paper_capital_came_from(yahoo):
    yahoo.now = _ist("2026-10-29", "11:00")
    yahoo.frame = pd.concat([_session(d) for d in THREE_DAYS])
    configured = yahoo_intraday.get_intraday_bars("RELIANCE.NS", "2026-10-28")
    assert "- Paper capital: intraday_paper_capital, a configured paper notional, not an account balance." in configured
    given = yahoo_intraday.get_intraday_bars("RELIANCE.NS", "2026-10-28", capital=750_000,
                                             capital_source="the portfolio's value at the analysis date")
    assert "Paper capital ₹7,50,000.00" in given
    assert "- Paper capital: the portfolio's value at the analysis date." in given
    assert "intraday_paper_capital" not in given


@pytest.mark.unit
def test_a_snapshot_beyond_yahoos_window_says_so_without_a_request(yahoo):
    yahoo.now = _ist("2026-10-29", "11:00")
    text = yahoo_intraday.get_intraday_bars("RELIANCE.NS", "2026-07-01")
    assert "Yahoo serves 5m bars for about the last 60 days; 2026-07-01 is outside that window" in text
    assert "Do not estimate intraday prices" in text and yahoo.calls == []


@pytest.mark.unit
def test_a_listing_without_a_calendar_plans_on_its_bars_hours(yahoo):
    yahoo.frame = pd.concat([_session(d, zone=NEW_YORK, start="09:30", end="16:00") for d in THREE_DAYS + ["2026-10-29"]])
    yahoo.now = pd.Timestamp("2026-10-29 11:02", tz=NEW_YORK).to_pydatetime()
    trading = yahoo_intraday.get_intraday_bars("AAPL", "2026-10-29")
    assert "Plan for: Thu 29 Oct 2026, the rest of today's session, open until 16:00 America/New_York." in trading
    assert "through Thu 29 Oct 2026 11:00 America/New_York" in trading and "Bars still forming" in trading
    yahoo.now = pd.Timestamp("2026-10-29 20:00", tz=NEW_YORK).to_pydatetime()
    text = yahoo_intraday.get_intraday_bars("AAPL", "2026-10-29")
    assert "Plan for: Fri 30 Oct 2026, the next trading day; today's session closed at 16:00 America/New_York." in text
    assert "Exit by: the session close" in text and "No exchange calendar is known" in text
    assert "Bars still forming" not in text


@pytest.mark.unit
@pytest.mark.parametrize("clock, plan_for", [
    ("11:02", "Plan for: Thu 29 Oct 2026, the rest of today's session, open until 16:00 America/New_York."),
    ("20:00", "Plan for: Fri 30 Oct 2026, the next trading day; today's session closed at 16:00 America/New_York."),
])
def test_one_session_of_bars_still_plans_on_the_listings_hours(yahoo, clock, plan_for):
    # The hours come from earlier sessions than the one shown, five as plan_session reads them,
    # so the snapshot and the run's own session agree.
    yahoo.frame = pd.concat([_session(d, zone=NEW_YORK, start="09:30", end="16:00") for d in THREE_DAYS + ["2026-10-29"]])
    yahoo.now = pd.Timestamp(f"2026-10-29 {clock}", tz=NEW_YORK).to_pydatetime()
    text = yahoo_intraday.get_intraday_bars("AAPL", "2026-10-29", sessions=1)
    assert plan_for in text and "sessions in the bars: 29 Oct to 29 Oct 2026 (1)" in text
    assert yahoo_intraday.plan_session("AAPL", "2026-10-29")["session"] == ("2026-10-29" if clock == "11:02"
                                                                            else "2026-10-30")
    assert len(yahoo.calls) == 1                 # the cached download serves the five sessions
