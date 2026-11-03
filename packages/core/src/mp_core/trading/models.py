"""Order and position models. Everything that reaches a broker is validated here first: quantity
in whole lots, prices on the exchange tick grid, and an idempotency key (`intent_id`) that is
stable for one decision, so a retry can never become a second order.

Prices are floats (like the rest of the platform) but are checked against the tick grid on the
way in, and money is rounded to paise on the way out.
"""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import Field, model_validator

from mp_core.models import AwareDatetime, StrictModel

Side = Literal["BUY", "SELL"]
OrderType = Literal["LIMIT", "SL-M", "MARKET"]
Leg = Literal["entry", "stop", "target", "exit"]
OrderStatus = Literal["open", "partial", "filled", "cancelled", "rejected"]
TERMINAL: frozenset[str] = frozenset({"filled", "cancelled", "rejected"})


def on_tick(price: float, tick: float) -> bool:
    return abs(price / tick - round(price / tick)) < 1e-6


def intent_key(*parts: str) -> str:
    """16 hex chars: fits the 20-character order tag brokers (Kite) echo back, so a lost response
    can be found again by tag instead of being placed twice."""
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


class OrderIntent(StrictModel):
    intent_id: str = Field(min_length=8, max_length=20)  # idempotency key, sent to the broker as the order tag
    trade_id: str  # groups the entry with its stop / target / exit
    leg: Leg
    instrument: str  # InstrumentKey id of the future
    side: Side
    qty: int = Field(gt=0)  # units (lots x lot_size)
    lot_size: int = Field(gt=0)
    tick_size: float = Field(gt=0)
    order_type: OrderType
    price: float | None = None  # LIMIT
    trigger: float | None = None  # SL-M
    created_at: AwareDatetime
    reason: str

    @model_validator(mode="after")
    def _check(self) -> OrderIntent:
        if self.qty % self.lot_size:
            raise ValueError(f"qty {self.qty} is not a whole number of lots of {self.lot_size}")
        if self.order_type == "LIMIT" and (self.price is None or self.price <= 0):
            raise ValueError("a LIMIT order needs a positive price")
        if self.order_type == "SL-M" and (self.trigger is None or self.trigger <= 0):
            raise ValueError("an SL-M order needs a positive trigger")
        for name in ("price", "trigger"):
            v = getattr(self, name)
            if v is not None and not on_tick(v, self.tick_size):
                raise ValueError(f"{name} {v} is not on the {self.tick_size} tick grid")
        return self

    @property
    def lots(self) -> int:
        return self.qty // self.lot_size

    @property
    def signed_qty(self) -> int:
        return self.qty if self.side == "BUY" else -self.qty


class Order(StrictModel):
    """The broker's view of one intent."""

    intent: OrderIntent
    broker_order_id: str
    status: OrderStatus
    filled_qty: int = 0
    avg_price: float | None = None
    updated_at: AwareDatetime
    reject_reason: str | None = None

    @property
    def is_open(self) -> bool:
        return self.status not in TERMINAL

    @property
    def remaining(self) -> int:
        return self.intent.qty - self.filled_qty


class Fill(StrictModel):
    fill_id: str
    broker_order_id: str
    intent_id: str
    trade_id: str
    leg: Leg
    instrument: str
    side: Side
    qty: int = Field(gt=0)
    price: float = Field(gt=0)
    ts: AwareDatetime

    @property
    def signed_qty(self) -> int:
        return self.qty if self.side == "BUY" else -self.qty


class Position(StrictModel):
    instrument: str
    qty: int  # signed units: + long, - short
    avg_price: float  # of the open quantity
    realized_pnl: float  # INR, gross of charges


class RiskDecision(StrictModel):
    approved: bool
    reasons: list[str] = []  # why it was rejected (empty when approved)
    qty: int = 0  # approved quantity (may be capped below the request)


class HaltState(StrictModel):
    halted: bool
    reason: str | None = None
    by: str | None = None  # "manual:<who>" or "auto:<breaker>"
    since: AwareDatetime | None = None
    flatten: bool = False  # also close open positions (not only stop new entries)
