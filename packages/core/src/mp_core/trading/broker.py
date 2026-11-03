"""What the order manager needs from a broker. The paper broker implements it today; a live
adapter (Kite) must implement exactly the same surface, so the code path does not change."""

from __future__ import annotations

from typing import Protocol

from mp_core.trading.models import Order, OrderIntent


class BrokerError(RuntimeError):
    """A request failed and the broker's state is known not to have changed (e.g. a validation
    error returned before anything was placed)."""


class Broker(Protocol):
    name: str

    async def place(self, intent: OrderIntent) -> Order:
        """Send one order, tagged with `intent.intent_id`. May raise TimeoutError / BrokerError; after
        a timeout the order may or may not exist - the caller must look it up by tag, never resend
        blindly. Brokers do NOT de-duplicate: placing the same intent twice makes two orders."""
        ...

    async def cancel(self, broker_order_id: str) -> Order: ...

    async def orders(self) -> list[Order]:
        """Every order of the day with its current status, filled quantity and average price."""
        ...

    async def positions(self) -> dict[str, int]:
        """Net open quantity per instrument (units, signed), as the broker sees it."""
        ...
