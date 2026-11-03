"""Order management: intents in, broker orders out, fills back - exactly once.

Duplicate-order safety rests on three rules:
  1. one intent_id -> at most one broker order. A second submit of the same intent returns the
     order already known (recorded *before* the broker is called, so even a crash-retry inside
     this process cannot resend it);
  2. after a timeout or error the order may exist: look it up by tag first and adopt it; only
     an order that is provably absent is placed again;
  3. if the broker cannot even be asked, the state is unknown: raise OrderUncertain - the
     trader halts rather than guess.

Fills are derived from each order's filled quantity and average price (`sync`), which every broker
reports, so partial fills arrive as several Fill records.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from mp_core.trading.audit import Audit
from mp_core.trading.broker import Broker, BrokerError
from mp_core.trading.models import Fill, Order, OrderIntent


class OrderUncertain(RuntimeError):
    """Whether an order exists at the broker could not be established. Trading must halt."""


class OrderManager:
    def __init__(self, broker: Broker, audit: Audit, *, timeout: float = 5.0, attempts: int = 3) -> None:
        self.broker = broker
        self.audit = audit
        self.timeout = timeout
        self.attempts = attempts
        self.orders: dict[str, Order] = {}  # intent_id -> latest known order
        self._submitted: set[str] = set()
        self._seen_fill: dict[str, tuple[int, float]] = {}  # broker_order_id -> (filled qty, avg price)
        self.consecutive_rejects = 0
        self.stats = {"placed": 0, "adopted_after_timeout": 0, "duplicates_suppressed": 0, "rejected": 0}

    async def _find(self, intent_id: str) -> Order | None:
        try:
            orders = await asyncio.wait_for(self.broker.orders(), self.timeout)
        except Exception as ex:
            raise OrderUncertain(f"cannot list broker orders to look for {intent_id}: {ex!r}") from None
        return next((o for o in orders if o.intent.intent_id == intent_id), None)

    async def submit(self, intent: OrderIntent, now: datetime) -> Order:
        if intent.intent_id in self.orders:
            self.stats["duplicates_suppressed"] += 1
            return self.orders[intent.intent_id]
        if intent.intent_id in self._submitted:
            # sent before but the outcome was never established: never resend blindly
            found = await self._find(intent.intent_id)
            if found is None:
                raise OrderUncertain(f"intent {intent.intent_id} was sent earlier with an unknown outcome")
            return self._track(found, now)
        self._submitted.add(intent.intent_id)
        self.audit.record("order_submit", now, **intent.model_dump(mode="json"))
        last_error: Exception | None = None
        for attempt in range(1, self.attempts + 1):
            try:
                order = await asyncio.wait_for(self.broker.place(intent), self.timeout)
                self.stats["placed"] += 1
                return self._track(order, now)
            except (TimeoutError, BrokerError, OSError) as ex:
                last_error = ex
                found = await self._find(intent.intent_id)  # did it reach the exchange anyway?
                if found is not None:
                    self.stats["adopted_after_timeout"] += 1
                    self.audit.record("order_adopted", now, intent_id=intent.intent_id, after=repr(ex), attempt=attempt)
                    return self._track(found, now)
                self.audit.record("order_retry", now, intent_id=intent.intent_id, error=repr(ex), attempt=attempt)
                await asyncio.sleep(0.2 * attempt)
        raise OrderUncertain(f"intent {intent.intent_id}: {self.attempts} attempts failed, last {last_error!r}")

    def _track(self, order: Order, now: datetime) -> Order:
        self.orders[order.intent.intent_id] = order
        if order.status == "rejected":
            self.consecutive_rejects += 1
            self.stats["rejected"] += 1
            self.audit.record("order_rejected", now, intent_id=order.intent.intent_id, reason=order.reject_reason or "")
        else:
            self.consecutive_rejects = 0
            self.audit.record("order_ack", now, intent_id=order.intent.intent_id, broker_order_id=order.broker_order_id)
        return order

    async def cancel(self, intent_id: str, now: datetime, why: str) -> Order | None:
        o = self.orders.get(intent_id)
        if o is None or not o.is_open:
            return o
        try:
            o = await asyncio.wait_for(self.broker.cancel(o.broker_order_id), self.timeout)
        except Exception as ex:
            raise OrderUncertain(f"cancel of {intent_id} failed: {ex!r}") from None
        self.orders[intent_id] = o
        self.audit.record("order_cancel", now, intent_id=intent_id, status=o.status, why=why)
        return o

    async def sync(self, now: datetime) -> list[Fill]:
        """Refresh every tracked order from the broker; return the fills that happened since the last sync."""
        try:
            broker_orders = await asyncio.wait_for(self.broker.orders(), self.timeout)
        except Exception as ex:
            raise OrderUncertain(f"cannot read broker orders: {ex!r}") from None
        fills: list[Fill] = []
        for o in broker_orders:
            iid = o.intent.intent_id
            if iid not in self.orders:
                continue  # not ours (manual trades show up in reconciliation instead)
            prev_qty, prev_avg = self._seen_fill.get(o.broker_order_id, (0, 0.0))
            if o.filled_qty > prev_qty and o.avg_price is not None:
                dq = o.filled_qty - prev_qty
                px = (o.avg_price * o.filled_qty - prev_avg * prev_qty) / dq
                f = Fill(
                    fill_id=f"{o.broker_order_id}:{o.filled_qty}",
                    broker_order_id=o.broker_order_id,
                    intent_id=iid,
                    trade_id=o.intent.trade_id,
                    leg=o.intent.leg,
                    instrument=o.intent.instrument,
                    side=o.intent.side,
                    qty=dq,
                    price=px,
                    ts=o.updated_at,
                )
                fills.append(f)
                self._seen_fill[o.broker_order_id] = (o.filled_qty, o.avg_price)
                self.audit.record("fill", now, fill=f.model_dump(mode="json"))
            if o.status != self.orders[iid].status:
                self.audit.record("order_status", now, intent_id=iid, status=o.status)
            self.orders[iid] = o
        return fills

    def open_orders(self, trade_id: str | None = None) -> list[Order]:
        return [o for o in self.orders.values() if o.is_open and (trade_id is None or o.intent.trade_id == trade_id)]
