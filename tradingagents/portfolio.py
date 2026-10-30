"""The caller's book, as the decision agents see it.

Optional input to a run: what is held, at what average price, and how much cash
is free. Without it the agents cannot tell adding to a full position from
opening a new one. Three states are distinct and must stay so: a position, a
flat book, and no context at all, since treating "not provided" as "flat" would
invent a fact about the caller's account.

Broker-neutral by construction: quantities are generic units and the currency is
whatever label the caller passes, so nothing here implies a venue or an
execution path. Yahoo prices an NSE/BSE holding in INR (read with Indian digit
grouping), a crypto pair in the currency it names (BTC-INR in INR) and a plain
US listing in USD, whatever the label.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from tradingagents.dataflows.symbols import (
    crypto_quote_currency,
    is_us_listing,
    normalize_symbol,
    resolve_symbol,
)
from tradingagents.markets.exchanges import exchange_for_symbol
from tradingagents.markets.money import format_money, format_price, format_quantity


def listing_currency(symbol: str) -> str | None:
    """The currency a canonical symbol's prices are in, where known here: its exchange's
    (INR on NSE/BSE), a crypto pair's quote (BTC-INR in INR), or USD for a plain US
    listing; None otherwise."""
    exchange = exchange_for_symbol(symbol)
    if exchange is not None:
        return exchange.currency
    return crypto_quote_currency(symbol) or ("USD" if is_us_listing(symbol) else None)


class Position(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    ticker: str = Field(description="Instrument symbol, e.g. AAPL or RELIANCE.NS")
    quantity: float = Field(description="Signed units held; negative is short")
    average_price: float | None = Field(default=None, description="Average entry price per unit")
    sector: str | None = Field(default=None, description="Sector, when the caller classifies it themselves")
    acquired: str | None = Field(default=None, description="Purchase date, YYYY-MM-DD, for holding-period tax")

    @field_validator("acquired")
    @classmethod
    def _iso_date(cls, value):
        if value is not None:
            date.fromisoformat(value)
        return value

    def price_currency(self, label: str | None) -> str | None:
        """The currency of this holding's prices: its listing's where known (``listing_currency``),
        else the book's label."""
        return listing_currency(normalize_symbol(self.ticker)) or label


class PortfolioContext(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    cash: float | None = Field(default=None, description="Free cash available")
    currency: str | None = Field(default=None, description="Currency label for cash and prices")
    positions: list[Position] = Field(default_factory=list)

    def positions_in(self, ticker: str) -> list[Position]:
        """The holdings of the instrument the run prices for ``ticker``, under any
        broker spelling (BTCUSDT, 700.HK). Holdings keep the spelling they were
        given: a symbol can also name another instrument (WTI the stock)."""
        symbol = normalize_symbol(ticker)
        return [p for p in self.positions if normalize_symbol(p.ticker) == symbol]

    def position_in(self, ticker: str) -> Position | None:
        held = self.positions_in(ticker)
        return held[0] if held else None

    def held_on(self, day: str) -> PortfolioContext:
        """The book on ``day`` (YYYY-MM-DD): without the lots acquired after it, which a run
        dated before their purchase (a backtest with today's book) must not see."""
        on = date.fromisoformat(day)
        held = [p for p in self.positions if p.acquired is None or date.fromisoformat(p.acquired) <= on]
        return self if len(held) == len(self.positions) else self.model_copy(update={"positions": held})

    def render(self, ticker: str) -> str:
        """The portfolio block for the decision agents, led by the analyzed instrument."""
        symbol = normalize_symbol(ticker)
        held = self.positions_in(symbol)
        lines = []
        for position in held:
            price = (f", average price {format_price(position.average_price, position.price_currency(self.currency))}"
                     if position.average_price is not None else "")
            spelled = position.ticker.strip().upper()
            listed = f" (listed as {position.ticker})" if spelled != symbol else ""
            lines.append(f"- Current position in {symbol}: {format_quantity(position.quantity)} units{price}{listed}")
        if not held:
            lines.append(f"- No current position in {symbol}")
        if self.cash is not None:
            lines.append(f"- Cash available: {format_money(self.cash, self.currency)}")
        others = [p for p in self.positions if all(p is not h for h in held)]
        if others:
            lines.append("- Other positions: " + ", ".join(f"{p.ticker.upper()} {format_quantity(p.quantity)}" for p in others))
        return "Portfolio at the analysis date:\n" + "\n".join(lines)

    def fingerprint(self) -> str:
        """Stable digest of the book, so a changed one cannot resume a stale run."""
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()[:12]


def load_portfolio(path: str | Path, default_exchange: str | None = None) -> PortfolioContext:
    """Read a portfolio JSON file, failing before the run rather than mid-graph.

    With ``default_exchange`` (``NSE``), a bare holding (``RELIANCE``) is read as
    that exchange's listing (``RELIANCE.NS``), as the analyzed ticker is.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        book = PortfolioContext.model_validate(data)
        if default_exchange:
            for position in book.positions:
                position.ticker = resolve_symbol(position.ticker, default_exchange)
        return book
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        raise ValueError(f"portfolio file {path} is not usable: {exc}") from exc
