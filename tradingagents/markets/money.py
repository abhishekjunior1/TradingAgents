"""Money as people in each market read it: INR with Indian digit grouping, lakh and crore.

``format_money`` is the one entry point for amounts: INR amounts are grouped the
Indian way (``₹1,23,45,678.00``) and can be shortened to lakh / crore; any other
currency keeps the international grouping with its code. ``format_price`` writes a
price per unit the same way, with the digits a sub-cent price needs.
"""

import math

LAKH = 100_000
CRORE = 10_000_000


def indian_grouping(whole: int) -> str:
    """``12345678`` -> ``"1,23,45,678"``: the last three digits, then pairs."""
    digits = str(abs(int(whole)))
    if len(digits) <= 3:
        grouped = digits
    else:
        head, tail = digits[:-3], digits[-3:]
        pairs = []
        while len(head) > 2:
            pairs.insert(0, head[-2:])
            head = head[:-2]
        grouped = ",".join(([head] if head else []) + pairs + [tail])
    return ("-" if whole < 0 else "") + grouped


def _grouped(magnitude: float, decimals: int) -> str:
    """A non-negative amount, rounded once as fixed-point text, then grouped the Indian way."""
    whole, _, fraction = f"{magnitude:.{decimals}f}".partition(".")
    return indian_grouping(int(whole)) + (f".{fraction}" if decimals else "")


def _missing(value) -> bool:
    return value is None or (isinstance(value, float) and not math.isfinite(value))


def format_inr(value: float, decimals: int = 2) -> str:
    """``1234567.891`` -> ``"₹12,34,567.89"``; a negative amount reads ``-₹…``."""
    if _missing(value):
        return "n/a"
    return f"{'-' if value < 0 else ''}₹{_grouped(abs(value), decimals)}"


def format_inr_compact(value: float) -> str:
    """Large amounts in crore or lakh (``₹1,234.57 crore``), smaller ones in full."""
    if _missing(value):
        return "n/a"
    sign, magnitude = ("-" if value < 0 else ""), abs(value)
    if magnitude >= CRORE:
        return f"{sign}₹{_grouped(magnitude / CRORE, 2)} crore"
    if magnitude >= LAKH:
        return f"{sign}₹{magnitude / LAKH:.2f} lakh"
    return format_inr(value)


def format_money(value: float | None, currency: str | None, compact: bool = False) -> str:
    """An amount in its currency: Indian grouping for INR, ``1,234.50 USD`` otherwise."""
    if _missing(value):
        return "n/a"
    if (currency or "").upper() == "INR":
        return format_inr_compact(value) if compact else format_inr(value)
    return f"{value:,.2f}{' ' + currency if currency else ''}"


def format_price(value: float | None, currency: str | None) -> str:
    """A price per unit in its currency, to six significant digits and at least two decimals.

    Two fixed decimals print XRP-BTC's 0.000016 BTC as ``0.00 BTC`` and ETH-BTC's
    0.028907 as ``0.03``; these are the digits the market tools show
    (``yahoo.common.price_text``). Totals, values and P/L stay with ``format_money``.
    """
    if _missing(value):
        return "n/a"
    magnitude = abs(value)
    decimals = 2 if magnitude == 0 else max(2, 5 - math.floor(math.log10(magnitude)))
    whole, _, fraction = f"{magnitude:.{decimals}f}".partition(".")
    digits = fraction.rstrip("0").ljust(2, "0")
    sign = "-" if value < 0 else ""
    if (currency or "").upper() == "INR":
        return f"{sign}₹{indian_grouping(int(whole))}.{digits}"
    return f"{sign}{int(whole):,}.{digits}{' ' + currency if currency else ''}"


def format_quantity(quantity: float) -> str:
    """A holding's size in full: ``12,345`` shares, ``0.0125`` of a coin (``,.4g`` wrote 1.234e+04)."""
    if float(quantity).is_integer():
        return f"{int(quantity):,}"
    text = f"{quantity:,.8f}".rstrip("0").rstrip(".")
    return text if text not in ("0", "-0") else f"{quantity:.3g}"     # dust below a satoshi


def format_percent(fraction: float | None, signed: bool = True, decimals: int = 2) -> str:
    """``0.0123`` -> ``"+1.23%"``; None or a non-number reads ``n/a``."""
    if _missing(fraction):
        return "n/a"
    return f"{fraction * 100:{'+' if signed else ''}.{decimals}f}%"
