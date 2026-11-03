"""A broker simulator that behaves like a real one where it matters for safety: it does not
de-duplicate orders, it can lose a response after placing the order, it rejects orders above the
exchange freeze quantity, and it fills partially when told to. Prices come from `on_price`
(the trader service feeds it the futures quote from each snapshot).

Fill rules (conservative):
    LIMIT    fills at the limit price once the market trades through it; a marketable limit at
             placement fills at the current price (never better than the market)
    SL-M     triggers when the market touches the trigger, fills at the worse of trigger and
             market, minus/plus slippage
    MARKET   fills at the market plus/minus slippage
"""

from __future__ import annotations

import itertools
from collections import deque
from datetime import datetime

from mp_core.clock import Clock
from mp_core.trading.broker import BrokerError
from mp_core.trading.models import Order, OrderIntent


class PaperBroker:
    name = "paper"

    def __init__(
        self,
        clock: Clock,
        *,
        slippage_bps: float = 1.0,
        freeze_qty: int | None = None,
        max_fill_per_update: int | None = None,
    ) -> None:
        self.clock = clock
        self.slippage_bps = slippage_bps
        self.freeze_qty = freeze_qty
        self.max_fill_per_update = max_fill_per_update  # units; None = fill completely
        self._orders: dict[str, Order] = {}
        self._last: dict[str, float] = {}
        self._pos: dict[str, int] = {}
        self._ids = itertools.count(1)
        self._faults: deque[str] = deque()
        self.placed = 0  # every order that reached the "exchange", duplicates included

    # ------------------------------------------------------------------ fault injection
    def inject(self, *faults: str) -> None:
        """Queue faults for the next place() calls: "timeout_after_place" (the order is placed but
        the response is lost), "timeout_before_place", "reject", "error" (BrokerError)."""
        self._faults.extend(faults)

    def adjust_position(self, instrument: str, qty: int) -> None:
        """Simulate a position appearing outside this system (manual trade, broker-side square-off)."""
        self._pos[instrument] = self._pos.get(instrument, 0) + qty

    # ------------------------------------------------------------------ broker surface
    async def place(self, intent: OrderIntent) -> Order:
        fault = self._faults.popleft() if self._faults else None
        if fault == "timeout_before_place":
            raise TimeoutError("simulated timeout before the order reached the exchange")
        if fault == "error":
            raise BrokerError("simulated broker error (nothing placed)")
        now = self.clock.now()
        oid = f"P{next(self._ids):06d}"
        self.placed += 1
        status, reason = "open", None
        if fault == "reject":
            status, reason = "rejected", "simulated RMS rejection"
        elif self.freeze_qty is not None and intent.qty > self.freeze_qty:
            status, reason = "rejected", f"quantity {intent.qty} exceeds freeze limit {self.freeze_qty}"
        order = Order(intent=intent, broker_order_id=oid, status=status, updated_at=now, reject_reason=reason)
        self._orders[oid] = order
        if status == "open":
            last = self._last.get(intent.instrument)
            if last is not None:
                self._try_fill(oid, last, now, at_placement=True)
        if fault == "timeout_after_place":
            raise TimeoutError("simulated timeout: the order was placed but the response was lost")
        return self._orders[oid]

    async def cancel(self, broker_order_id: str) -> Order:
        o = self._orders.get(broker_order_id)
        if o is None:
            raise BrokerError(f"unknown order {broker_order_id}")
        if o.is_open:
            o = o.model_copy(update={"status": "cancelled", "updated_at": self.clock.now()})
            self._orders[broker_order_id] = o
        return o

    async def orders(self) -> list[Order]:
        return list(self._orders.values())

    async def positions(self) -> dict[str, int]:
        return {k: v for k, v in self._pos.items() if v}

    # ------------------------------------------------------------------ market
    def on_price(self, instrument: str, price: float, ts: datetime | None = None) -> None:
        self._last[instrument] = price
        now = ts or self.clock.now()
        for oid, o in list(self._orders.items()):
            if o.is_open and o.intent.instrument == instrument:
                self._try_fill(oid, price, now)

    def _slip(self, price: float, side: str) -> float:
        s = price * self.slippage_bps / 10_000
        return price + s if side == "BUY" else price - s

    def _try_fill(self, oid: str, last: float, now: datetime, at_placement: bool = False) -> None:
        o = self._orders[oid]
        it = o.intent
        buy = it.side == "BUY"
        px: float | None = None
        if it.order_type == "MARKET":
            px = self._slip(last, it.side)
        elif it.order_type == "LIMIT":
            if (last <= it.price) if buy else (last >= it.price):
                px = min(last, it.price) if buy else max(last, it.price)
                if not at_placement:
                    px = it.price  # traded through the limit: assume the limit price, not better
        elif it.order_type == "SL-M" and ((last >= it.trigger) if buy else (last <= it.trigger)):
            worse = max(last, it.trigger) if buy else min(last, it.trigger)
            px = self._slip(worse, it.side)
        if px is None:
            return
        qty = o.remaining if self.max_fill_per_update is None else min(o.remaining, self.max_fill_per_update)
        qty -= qty % it.lot_size
        if qty <= 0:
            return
        filled = o.filled_qty + qty
        avg = px if not o.filled_qty else (o.avg_price * o.filled_qty + px * qty) / filled
        self._orders[oid] = o.model_copy(
            update={
                "filled_qty": filled,
                "avg_price": avg,
                "status": "filled" if filled == it.qty else "partial",
                "updated_at": now,
            }
        )
        self._pos[it.instrument] = self._pos.get(it.instrument, 0) + (qty if buy else -qty)
