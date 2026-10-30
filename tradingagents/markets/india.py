"""Indian market knowledge: NSE/BSE indices, sector indices, and the briefing the agents reason from.

The index symbols are Yahoo Finance's, checked against Yahoo on 2026-10-29
(name, INR quote, daily history). Some sector indices are listed by Yahoo with
almost no history; they are named here but marked ``history=False``, so nothing
computes a return from them.

The briefing states how the market works (sessions, settlement, price bands,
reporting deadlines, the policy calendar) and what this system cannot see (FII/DII
flows, a stock's own price band, promoter holdings). It carries no live numbers:
those come only from tools, with their source and date.
"""

import re
from dataclasses import dataclass
from datetime import date

from tradingagents.markets.exchanges import EXCHANGES, Exchange


@dataclass(frozen=True)
class IndexInfo:
    name: str
    symbol: str                 # Yahoo Finance symbol
    exchange: str               # "NSE" or "BSE"
    aliases: tuple[str, ...]    # what a person may type, compacted (see ``compact``)
    history: bool = True        # Yahoo serves a usable daily history


INDICES: tuple[IndexInfo, ...] = (
    IndexInfo("NIFTY 50", "^NSEI", "NSE", ("NIFTY", "NIFTY50", "CNXNIFTY")),
    IndexInfo("NIFTY Bank", "^NSEBANK", "NSE", ("BANKNIFTY", "NIFTYBANK")),
    IndexInfo("NIFTY Financial Services", "NIFTY_FIN_SERVICE.NS", "NSE",
              ("FINNIFTY", "NIFTYFIN", "NIFTYFINSERVICE", "NIFTYFINANCIALSERVICES")),
    IndexInfo("NIFTY IT", "^CNXIT", "NSE", ("NIFTYIT", "CNXIT")),
    IndexInfo("BSE SENSEX", "^BSESN", "BSE", ("SENSEX", "BSESENSEX", "SPBSESENSEX")),
    IndexInfo("India VIX", "^INDIAVIX", "NSE", ("INDIAVIX", "NIFTYVIX")),
    IndexInfo("NIFTY Next 50", "^NSMIDCP", "NSE", ("NIFTYNEXT50", "NIFTYJUNIOR")),
    IndexInfo("NIFTY 100", "^CNX100", "NSE", ("NIFTY100",)),
    IndexInfo("NIFTY 500", "^CRSLDX", "NSE", ("NIFTY500",)),
    IndexInfo("NIFTY Auto", "^CNXAUTO", "NSE", ("NIFTYAUTO",)),
    IndexInfo("NIFTY Pharma", "^CNXPHARMA", "NSE", ("NIFTYPHARMA",)),
    IndexInfo("NIFTY FMCG", "^CNXFMCG", "NSE", ("NIFTYFMCG",)),
    IndexInfo("NIFTY Metal", "^CNXMETAL", "NSE", ("NIFTYMETAL",)),
    IndexInfo("NIFTY Energy", "^CNXENERGY", "NSE", ("NIFTYENERGY",)),
    IndexInfo("NIFTY Realty", "^CNXREALTY", "NSE", ("NIFTYREALTY",)),
    IndexInfo("NIFTY PSU Bank", "^CNXPSUBANK", "NSE", ("NIFTYPSUBANK",)),
    IndexInfo("NIFTY Infrastructure", "^CNXINFRA", "NSE", ("NIFTYINFRA", "NIFTYINFRASTRUCTURE")),
    IndexInfo("NIFTY India Consumption", "^CNXCONSUM", "NSE", ("NIFTYCONSUMPTION",)),
    IndexInfo("NIFTY MNC", "^CNXMNC", "NSE", ("NIFTYMNC",)),
    # Listed by Yahoo with no usable history (one row on 2026-10-29).
    IndexInfo("NIFTY Media", "^CNXMEDIA", "NSE", ("NIFTYMEDIA",), history=False),
    IndexInfo("NIFTY Midcap 100", "NIFTY_MIDCAP_100.NS", "NSE", ("NIFTYMIDCAP100",), history=False),
    IndexInfo("NIFTY Smallcap 100", "^CNXSC", "NSE", ("NIFTYSMALLCAP100", "NIFTYSMLCAP100"), history=False),
    IndexInfo("NIFTY Private Bank", "NIFTY_PVT_BANK.NS", "NSE", ("NIFTYPRIVATEBANK", "NIFTYPVTBANK"), history=False),
    IndexInfo("NIFTY Healthcare", "NIFTY_HEALTHCARE.NS", "NSE", ("NIFTYHEALTHCARE",), history=False),
    IndexInfo("NIFTY Oil & Gas", "NIFTY_OIL_AND_GAS.NS", "NSE", ("NIFTYOILANDGAS", "NIFTYOILGAS"), history=False),
)

INDEX_BY_SYMBOL: dict[str, IndexInfo] = {index.symbol.upper(): index for index in INDICES}
INDEX_BY_ALIAS: dict[str, IndexInfo] = {alias: index for index in INDICES for alias in index.aliases}

# Cross-asset series an Indian stock is read against.
USD_INR = "USDINR=X"
BRENT = "BZ=F"

# Yahoo tags that make a news article India-related: an NSE/BSE listing (by
# suffix), the headline indices, India VIX or the rupee.
NEWS_TICKERS = (".NS", ".BO", "^NSEI", "^BSESN", "^NSEBANK", "^INDIAVIX", "INR=X", USD_INR)
NEWS_GAP = (
    "Yahoo's news search returned no stories in this window that could be confirmed as "
    "India-related. RBI policy, the rupee, SEBI, the Union Budget and FII/DII flows are not "
    "covered by this vendor here: report them as unavailable rather than inferring them."
)


def compact(text: str) -> str:
    """``"Nifty Fin Service"`` -> ``"NIFTYFINSERVICE"``: how index names are matched."""
    return re.sub(r"[\s_\-&.]", "", text.upper()) if text else ""


def index_for(text: str) -> IndexInfo | None:
    """The index a person means by ``text`` (``NIFTY 50``, ``banknifty``, ``^NSEI``), else None."""
    if not isinstance(text, str) or not text.strip():
        return None
    upper = text.strip().upper()
    if upper in INDEX_BY_SYMBOL:
        return INDEX_BY_SYMBOL[upper]
    key = compact(upper)
    return INDEX_BY_ALIAS.get(key) or next(
        (index for index in INDICES if compact(index.name) == key), None)


# Yahoo's sector / industry -> the closest NIFTY sectoral index. An approximation:
# a company's index membership is NSE's call, not this table's.
_INDUSTRY_INDEX = (
    ("bank", "^NSEBANK"),
    ("auto", "^CNXAUTO"),
    ("steel", "^CNXMETAL"), ("aluminum", "^CNXMETAL"), ("copper", "^CNXMETAL"),
    ("metal", "^CNXMETAL"), ("mining", "^CNXMETAL"),
    ("engineering & construction", "^CNXINFRA"), ("infrastructure", "^CNXINFRA"),
    ("drug", "^CNXPHARMA"), ("pharma", "^CNXPHARMA"),
    ("real estate", "^CNXREALTY"),
)
_SECTOR_INDEX = {
    "financial services": "NIFTY_FIN_SERVICE.NS",
    "technology": "^CNXIT",
    "healthcare": "^CNXPHARMA",
    "consumer defensive": "^CNXFMCG",
    "energy": "^CNXENERGY",
    "utilities": "^CNXENERGY",
    "real estate": "^CNXREALTY",
    "consumer cyclical": "^CNXCONSUM",
}


def sector_index_for(sector: str | None, industry: str | None) -> IndexInfo | None:
    """The NIFTY sectoral index closest to a company's Yahoo sector and industry, if any."""
    text = (industry or "").lower()
    for needle, symbol in _INDUSTRY_INDEX:
        if needle in text:
            return INDEX_BY_SYMBOL[symbol]
    symbol = _SECTOR_INDEX.get((sector or "").strip().lower())
    return INDEX_BY_SYMBOL[symbol] if symbol else None


EVIDENCE_RULES = (
    # The example names no real date: a historical run must not read one after its own.
    "Evidence rules: tie every important number or claim to the tool and the as-of date it came from "
    "(for example 'Yahoo Finance daily close, YYYY-MM-DD'). Label anything that comes from neither a tool "
    "nor this prompt as an assumption or general knowledge. When data is missing, stale or unavailable "
    "(a tool says DATA_UNAVAILABLE or NO_DATA_AVAILABLE), say so and lower your confidence instead of "
    "filling the gap; never estimate a price, ratio or flow figure you were not given."
)


# SEBI's closing-auction changes (circulars of 16 Jan and 15 Oct 2026; NSE go-live
# NSE/CMTR/75479): from 3 Aug 2026 stocks with derivatives end continuous trading
# at 15:15 for a closing auction; from 7 Dec 2026 the auction follows 15:30.
_CLOSING_AUCTION = date(2026, 8, 3)
_AUCTION_AFTER_CLOSE = date(2026, 12, 7)


def session_rules(day: date) -> str:
    """NSE/BSE equity hours in force on ``day``, naming no date after it."""
    if day < _CLOSING_AUCTION:
        return ("pre-open 09:00-09:15 IST, continuous trading 09:15-15:30 with the closing price the "
                "volume-weighted average of 15:00-15:30, and a post-close session 15:40-16:00")
    if day < _AUCTION_AFTER_CLOSE:
        return ("pre-open 09:00-09:15 IST; stocks with derivatives trade continuously 09:15-15:15 and then in a "
                "closing auction 15:15-15:35 (within 3% of the 15:00-15:15 average price), other stocks "
                "09:15-15:30 closing on the 15:00-15:30 average; post-close session 15:50-16:00")
    return ("pre-open 09:00-09:15 IST, continuous trading 09:15-15:30, then a closing auction 15:30-15:40 for "
            "stocks with derivatives; post-close session 15:50-16:00")


def special_session_hours(exchange: Exchange, day: date) -> str | None:
    """``day``'s special session when it keeps other hours than the regular ones (Muhurat trading), else None."""
    special = exchange.special_sessions.get(day)
    if not special or special[2:] == exchange.regular:
        return None
    return (f"{special[0]}, a special session: pre-open {special[1]:%H:%M}-{special[2]:%H:%M} "
            f"{exchange.tz_label}, trading {special[2]:%H:%M}-{special[3]:%H:%M}")


def hours_in_force(exchange: Exchange, day: date) -> str:
    """The hours in force on ``day``: a special session's on other hours, else ``session_rules``."""
    return special_session_hours(exchange, day) or session_rules(day)


# RBI Monetary Policy Committee meetings for 2026-27 (RBI press release
# 2025-2026/2306 of 23 Mar 2026). Before that release they were not public.
MPC_SCHEDULE_PUBLISHED = date(2026, 3, 23)
MPC_MEETINGS: tuple[tuple[date, date], ...] = (
    (date(2026, 4, 6), date(2026, 4, 8)), (date(2026, 6, 3), date(2026, 6, 5)),
    (date(2026, 8, 3), date(2026, 8, 5)), (date(2026, 10, 5), date(2026, 10, 7)),
    (date(2026, 12, 2), date(2026, 12, 4)), (date(2027, 2, 3), date(2027, 2, 5)),
)


def next_mpc_meeting(day: date) -> tuple[date, date] | None:
    """The next scheduled MPC meeting a run dated ``day`` could have known of, else None."""
    if day < MPC_SCHEDULE_PUBLISHED:
        return None
    return next(((start, end) for start, end in MPC_MEETINGS if end >= day), None)


def market_briefing(exchange_code: str, currency: str = "INR", on: date | None = None) -> str:
    """How the Indian market works on ``on``, for agents reasoning about an NSE/BSE instrument.

    Rules and structure only, as in force on the run's date; figures come from
    tools with their dates.
    """
    other = "BSE" if exchange_code == "NSE" else "NSE"
    on = on or date.today()
    meeting = next_mpc_meeting(on)
    mpc = (f"; the next scheduled meeting is {meeting[0]:%d}-{meeting[1]:%d %b %Y} (decisions are customarily "
           "announced on its last day)") if meeting else ""
    # A special session on other hours (Muhurat trading) keeps its own that day; the usual
    # hours stay given, for the sessions either side of it.
    exchange = EXCHANGES.get(exchange_code)
    special = special_session_hours(exchange, on) if exchange is not None else None
    in_force = f" Hours in force on {on:%d %b %Y}: {special}." if special else ""
    return (
        f"Indian market rules for this {exchange_code} listing: prices, values and returns are in {currency}; "
        f"the exchange runs on India Standard Time (IST, UTC+05:30) and trades on weekdays that are not "
        f"exchange holidays, plus occasional special sessions such as Muhurat trading at Diwali. Usual hours: "
        f"{session_rules(on)}.{in_force} A day's close is final only after the post-close session. "
        f"The same company may also trade on {other} at a slightly different price. Equity trades settle T+1. "
        "Stocks without derivatives carry fixed daily price bands (circuit limits) set by the exchange, "
        "commonly 2%, 5%, 10% or 20%; stocks with derivatives have no fixed band but a 10% dynamic band the "
        "exchange widens in steps after a cooling-off period; the band for this stock is not in your tools, "
        "so do not assume one. Market-wide circuit breakers halt all trading when the NIFTY 50 or SENSEX "
        "moves 10%, 15% or 20% from the previous close (the halt's length depends on the time of day; the "
        "20% level closes the market for the day). "
        "Listed companies report quarterly results within 45 days of each quarter end and annual audited "
        "results within 60 days of the financial year end (31 March), so results seasons run mid-July to "
        "mid-August, mid-October to mid-November, mid-January to mid-February, and April to May. "
        f"RBI's Monetary Policy Committee sets the repo rate at scheduled meetings (at least four a year by "
        f"law, usually six){mpc}. The Union Budget is presented on 1 February. "
        "Weigh the drivers specific to India: RBI policy (the repo rate, inflation and liquidity), the Union "
        "Budget and government policy, SEBI regulation and exchange announcements, foreign (FII/FPI) and "
        "domestic (DII) institutional flows, the rupee, and crude oil (India imports most of its crude, so "
        "costlier oil tends to pressure the rupee and oil-consuming sectors while a weaker rupee tends to help "
        "exporters such as IT services and pharma; these are typical sensitivities, not data). Corporate "
        "actions (dividends, bonus issues, splits, buybacks) move prices on their ex-dates. "
        "This system has no feed for FII/DII flows, promoter holdings or pledges, F&O open interest, or "
        "exchange announcements: if they matter, say they are unavailable rather than estimating them."
    )
