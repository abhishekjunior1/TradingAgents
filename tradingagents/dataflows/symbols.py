"""Symbol normalization for vendor calls, the clock a listing's day is read on, and
ticker values safe to use in a path.

Yahoo Finance (the default vendor) uses specific ticker conventions that
differ from the broker / TradingView / MT5 style symbols users often type:

    user types        Yahoo wants       why
    ---------------   ---------------   -----------------------------------
    XAUUSD, XAUUSD+   GC=F              gold has no forex pair on Yahoo;
                                        it is quoted as a COMEX future
    EURUSD            EURUSD=X          spot forex pairs take a ``=X`` suffix
    BTCUSD            BTC-USD           crypto pairs use a ``-`` separator
    SPX500, US500     ^GSPC             index CFDs map to Yahoo index symbols
    09992.HK, 700.HK  9992.HK, 0700.HK  HK codes are zero-padded to 4 digits
    600519.SH         600519.SS         Yahoo spells Shanghai ``.SS``
    NSE:RELIANCE,     RELIANCE.NS       NSE listings take ``.NS``; brokers,
      RELIANCE.NSE,                     TradingView and data vendors spell
      RELIANCE-EQ                       the exchange other ways
    BSE:RELIANCE      RELIANCE.BO       BSE listings take ``.BO`` (by symbol;
                                        Yahoo serves no BSE scrip codes)
    NIFTY, BANKNIFTY  ^NSEI, ^NSEBANK   Indian index names map to Yahoo's
                                        index symbols (``markets.india``)

A bare symbol (``RELIANCE``) names no market; ``resolve_symbol`` gives it the
listing suffix of a default exchange the caller chose.

Passing the raw broker symbol to Yahoo returns an empty result, which the
agents previously received as free text and could hallucinate a price
around (see issue #781). Centralizing the mapping here means every yfinance
entry point resolves symbols the same way, and new instruments are added by
appending a table row rather than editing call sites.
"""

from __future__ import annotations

import logging
import re
from zoneinfo import ZoneInfo

from tradingagents.markets.exchanges import exchange_for_symbol
from tradingagents.markets.india import index_for

logger = logging.getLogger(__name__)

NEW_YORK = ZoneInfo("America/New_York")


# ISO-4217 codes common enough to appear in retail forex pairs. A bare
# six-letter symbol whose halves are BOTH in this set is treated as a spot
# forex pair and given Yahoo's ``=X`` suffix.
_FOREX_CURRENCIES = frozenset(
    {
        "USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD",
        "CNY", "CNH", "HKD", "SGD", "SEK", "NOK", "DKK", "PLN",
        "MXN", "ZAR", "TRY", "INR", "KRW", "BRL", "RUB", "THB",
    }
)

# Crypto bases that brokers quote against USD without a separator.
_CRYPTO_BASES = frozenset(
    {"BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "LTC", "BCH", "DOT", "AVAX", "LINK"}
)

# Explicit aliases for instruments whose broker symbol does not map to a
# Yahoo symbol by rule. Metals/energy resolve to their front-month future;
# index CFD names resolve to the underlying Yahoo index symbol. Extend by
# adding rows — no call site changes required.
_ALIASES = {
    # Precious metals (spot names -> COMEX/NYMEX futures)
    "XAUUSD": "GC=F", "XAU": "GC=F", "GOLD": "GC=F",
    "XAGUSD": "SI=F", "XAG": "SI=F", "SILVER": "SI=F",
    "XPTUSD": "PL=F", "XPDUSD": "PA=F",
    # Energy
    "WTICOUSD": "CL=F", "USOIL": "CL=F", "WTI": "CL=F",
    "BCOUSD": "BZ=F", "UKOIL": "BZ=F", "BRENT": "BZ=F",
    "NATGAS": "NG=F", "XNGUSD": "NG=F",
    "COPPER": "HG=F", "XCUUSD": "HG=F",
    # Index CFDs -> Yahoo index symbols
    "SPX500": "^GSPC", "US500": "^GSPC", "SPX": "^GSPC",
    "NAS100": "^NDX", "US100": "^NDX", "USTEC": "^NDX",
    "US30": "^DJI", "DJI30": "^DJI", "WS30": "^DJI",
    "GER40": "^GDAXI", "GER30": "^GDAXI", "DE40": "^GDAXI",
    "UK100": "^FTSE", "JP225": "^N225", "JPN225": "^N225",
    "FRA40": "^FCHI", "EU50": "^STOXX50E", "HK50": "^HSI",
}

# HKEX codes as Yahoo spells them: the number zero-padded to 4 digits (#957).
_HK_CODE = re.compile(r"^(\d{1,5})\.HK$")
_SHANGHAI_SH = re.compile(r"^(\d{6})\.SH$")

# Indian listings as brokers, TradingView and data vendors write them. NSE's
# "EQ" series is its ordinary equity segment.
_EXCHANGE_PREFIX = re.compile(r"^(NSE|BSE|BOM)\s*:\s*(\S.*)$")
_VENDOR_SUFFIX = {".NSE": ".NS", ".BSE": ".BO"}
_NSE_EQ_SERIES = re.compile(r"^([A-Z0-9&\-]+)-EQ$")
# A symbol that names no market: no suffix, index caret, future or pair marker.
_BARE = re.compile(r"^[A-Z0-9][A-Z0-9&\-]*$")
_BSE_SCRIP_CODE = re.compile(r"^\d{6}(\.BO)?$")


def _indian_listing(s: str) -> str:
    """``NSE:RELIANCE``, ``RELIANCE.NSE``, ``RELIANCE-EQ`` -> ``RELIANCE.NS``; ``BSE:``/``BOM:`` -> ``.BO``."""
    if prefixed := _EXCHANGE_PREFIX.match(s):
        venue, symbol = prefixed.groups()
        index = index_for(symbol)
        if index is not None:
            return index.symbol
        return symbol.strip() + (".NS" if venue == "NSE" else ".BO")
    for vendor, yahoo in _VENDOR_SUFFIX.items():
        if s.endswith(vendor):
            return s[: -len(vendor)] + yahoo
    if series := _NSE_EQ_SERIES.match(s):
        return series.group(1) + ".NS"
    return s


# Crypto quote currencies that all map to Yahoo's USD pair. Yahoo lists only
# ``<BASE>-USD`` (not the USDT/USDC stablecoin pairs), so a broker symbol quoted
# in any of these resolves to ``-USD`` (#982). Longest first so ``USDT``/``USDC``
# match before the ``USD`` substring.
_CRYPTO_QUOTES = ("USDT", "USDC", "USD")


def crypto_base(raw: str) -> str | None:
    """Return the crypto base (e.g. ``BTC``) for a known USD/USDT/USDC-quoted
    crypto symbol in any form the pipeline may hold — ``BTC-USD``, ``BTCUSD``,
    ``BTC-USDT`` — or None for non-crypto symbols. Purely syntactic.
    """
    if not isinstance(raw, str):
        return None
    compact = raw.strip().upper().rstrip("+").replace("-", "")
    for quote in _CRYPTO_QUOTES:
        if compact.endswith(quote):
            base = compact[: -len(quote)]
            return base if base in _CRYPTO_BASES else None
    return None


def _normalize_crypto(s: str) -> str | None:
    """Return ``<BASE>-USD`` for a known USD/USDT/USDC-quoted crypto, else None."""
    base = crypto_base(s)
    return f"{base}-USD" if base else None


def is_us_listing(symbol: str) -> bool:
    """Whether a canonical Yahoo symbol is a plain US listing: one that names no
    market (no suffix, index caret, future or pair marker) and is not a crypto
    pair. Yahoo lists such symbols on US exchanges."""
    return isinstance(symbol, str) and bool(_BARE.fullmatch(symbol)) and crypto_base(symbol) is None


def listing_zone(symbol: str | None) -> ZoneInfo | None:
    """The time zone whose date is "today" for a listing: an NSE/BSE listing's or
    index's exchange zone, New York's for a plain US listing, else None (the
    machine's own date: futures, forex, crypto, and other markets' listings and indices).

    ``symbol`` is read as ``normalize_symbol`` reads it, so ``NSE:RELIANCE`` is India's.
    """
    canonical = normalize_symbol(symbol) if isinstance(symbol, str) else None
    if not canonical:
        return None
    exchange = exchange_for_symbol(canonical)
    if exchange is not None:
        return exchange.tz
    return NEW_YORK if is_us_listing(canonical) else None


def normalize_symbol(raw: str) -> str:
    """Map a user/broker symbol to its canonical Yahoo Finance symbol.

    Resolution order (first match wins):
      1. Explicit alias table (metals, energy, index CFDs).
      2. Crypto rule: a known crypto base quoted in USD/USDT/USDC (dashed or
         not) -> ``BASE-USD``.
      3. Forex rule: six letters that are two ISO currency codes -> ``PAIR=X``.
      4. HK rule: a numeric ``.HK`` code -> Yahoo's 4-digit padding
         (``09992.HK`` -> ``9992.HK``, ``700.HK`` -> ``0700.HK``).
      5. Shanghai rule: ``600519.SH`` -> ``600519.SS``.
      6. Indian rules: exchange-prefixed and vendor-suffixed listings
         (``NSE:RELIANCE``, ``RELIANCE.BSE``) take Yahoo's suffix, and Indian
         index names (``NIFTY 50``, ``BANKNIFTY``) their Yahoo index symbol.
      7. Otherwise the upper-cased symbol is returned unchanged (plain
         equities, ETFs, Yahoo-native symbols like ``GC=F`` or ``^GSPC``).

    A trailing ``+`` (broker CFD marker, e.g. ``XAUUSD+``) is stripped before
    matching. The function is purely syntactic — it performs no network
    calls — so it is safe to apply on every request.
    """
    if not isinstance(raw, str) or not raw.strip():
        return raw

    s = raw.strip().upper()
    # Broker CFD/qualifier suffixes Yahoo never uses.
    s = s.rstrip("+")
    s = _indian_listing(s)

    crypto = _normalize_crypto(s)
    index = index_for(s)
    if s in _ALIASES:
        canonical = _ALIASES[s]
    elif index is not None:
        canonical = index.symbol
    elif crypto is not None:
        canonical = crypto
    elif len(s) == 6 and s[:3] in _FOREX_CURRENCIES and s[3:] in _FOREX_CURRENCIES:
        canonical = f"{s}=X"
    elif hk := _HK_CODE.match(s):
        canonical = f"{int(hk.group(1)):04d}.HK"
    elif sh := _SHANGHAI_SH.match(s):
        canonical = f"{sh.group(1)}.SS"
    else:
        canonical = s

    if canonical != raw.strip().upper():
        logger.info("Resolved symbol %r to Yahoo symbol %r", raw, canonical)
    return canonical


def resolve_symbol(raw: str, default_exchange: str | None = None) -> str:
    """``normalize_symbol``, with a bare equity symbol listed on ``default_exchange``.

    ``RELIANCE`` with ``NSE`` is ``RELIANCE.NS``. A symbol that already names its
    market (a suffix, an index, a future, a forex or crypto pair) is left as it
    is. BSE scrip codes (``500325``) are refused with the fix: Yahoo Finance
    serves BSE listings by symbol (``RELIANCE.BO``) and returns nothing for a code.
    """
    from tradingagents.markets.exchanges import get_exchange

    canonical = normalize_symbol(raw)
    if not isinstance(canonical, str) or not canonical:
        return canonical
    exchange = get_exchange(default_exchange)
    if _BSE_SCRIP_CODE.fullmatch(canonical) and (exchange is not None or canonical.endswith(".BO")):
        raise ValueError(
            f"{raw!r} looks like a BSE scrip code; Yahoo Finance has no data for those. "
            "Use the trading symbol with its exchange, e.g. RELIANCE.BO or RELIANCE.NS."
        )
    if exchange is None or not _BARE.fullmatch(canonical) or crypto_base(canonical):
        return canonical
    return canonical + exchange.suffix


# Tickers can contain letters, digits, dot, dash, underscore, caret
# (index symbols like ^GSPC), equals (futures like GC=F), plus
# (forex/CFD symbols like XAUUSD+) and ampersand (NSE listings like M&M.NS).
# None of these enable directory traversal, so the value never escapes a
# containing directory when interpolated into a path. Anything else is rejected.
_TICKER_PATH_RE = re.compile(r"^[A-Za-z0-9._\-\^=+&]+$")


def safe_ticker_component(value: str, *, max_len: int = 32) -> str:
    """Validate ``value`` is safe to interpolate into a filesystem path.

    Tickers come from user CLI input or from LLM tool calls, both of which
    can be influenced by attacker-controlled content (e.g. prompt injection
    embedded in fetched news). Without validation, a value like
    ``"../../../etc/foo"`` flows into ``os.path.join`` / ``Path /`` and
    escapes the configured cache, checkpoint, or results directory.

    Returns ``value`` unchanged when it matches the allowed pattern; raises
    ``ValueError`` otherwise.
    """
    if not isinstance(value, str) or not value:
        raise ValueError(f"ticker must be a non-empty string, got {value!r}")
    if len(value) > max_len:
        raise ValueError(f"ticker exceeds {max_len} chars: {value!r}")
    if not _TICKER_PATH_RE.fullmatch(value):
        raise ValueError(
            f"ticker contains characters not allowed in a filesystem path: {value!r}"
        )
    # The regex above allows '.', so values like '.', '..', '...' would pass,
    # and as a path component they traverse the parent directory. Reject any
    # value that's only dots.
    if set(value) == {"."}:
        raise ValueError(f"ticker cannot consist solely of dots: {value!r}")
    return value
