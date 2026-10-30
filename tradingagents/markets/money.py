"""Money as people in each market read it: INR with Indian digit grouping, lakh and crore.

``format_money`` is the one entry point: INR amounts are grouped the Indian way
(``₹1,23,45,678.00``) and can be shortened to lakh / crore; any other currency
keeps the international grouping with its code.
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


def format_percent(fraction: float | None, signed: bool = True, decimals: int = 2) -> str:
    """``0.0123`` -> ``"+1.23%"``; None or a non-number reads ``n/a``."""
    if _missing(fraction):
        return "n/a"
    return f"{fraction * 100:{'+' if signed else ''}.{decimals}f}%"
