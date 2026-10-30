from enum import StrEnum


class AnalystType(StrEnum):
    MARKET = "market"
    # Wire value stays "social" for saved-config and string-keyed-caller
    # back-compat; the user-facing label is "Sentiment Analyst".
    SOCIAL = "social"
    NEWS = "news"
    FUNDAMENTALS = "fundamentals"
    # Intraday levels and a paper plan for one session; joins an intraday run by
    # itself and is otherwise chosen by name, never by default.
    DAY_TRADER = "day_trader"


class AssetType(StrEnum):
    STOCK = "stock"
    CRYPTO = "crypto"
