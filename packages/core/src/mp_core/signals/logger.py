"""Logs signals to Timescale with the inputs that produced them - the raw material for evaluation.

Snapshots are rebuilt several times a second; logging all of them would bury the record. A signal
is logged when its status or bias changes, and every 15 minutes while it stays the same.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Protocol

from mp_core.signals.models import Signal
from mp_core.snapshot import MarketSnapshot

REPEAT_EVERY = timedelta(minutes=15)


class SignalHistory(Protocol):
    async def record_signal(self, signal: Signal, inputs: dict) -> None: ...


def signal_inputs(s: MarketSnapshot) -> dict:
    """The parts of the snapshot fusion read, with their provenance - enough to re-derive the signal."""
    front = s.expiries[0] if s.expiries else None
    return {
        "snapshot_version": s.version,
        "stale_sections": s.stale_sections,
        "errors": s.errors,
        "quote": s.quote.model_dump(mode="json"),
        "future": s.future.model_dump(mode="json") if s.future else None,
        "expected_range": s.expected_range.model_dump(mode="json") if s.expected_range else None,
        "levels": s.levels.model_dump(mode="json") if s.levels else None,
        "front_expiry": front.model_dump(mode="json", exclude={"value": {"iv": {"strikes"}}}) if front else None,
        "news": s.news.model_dump(mode="json") if s.news else None,
    }


class SignalLogger:
    def __init__(self, history: SignalHistory) -> None:
        self.history = history
        self._last: tuple[str, str] | None = None
        self._last_ts = None
        self.logged = 0

    async def maybe_log(self, snap: MarketSnapshot) -> bool:
        sig = snap.signal
        if sig is None:
            return False
        key = (sig.status, sig.bias)
        due = self._last_ts is None or sig.ts - self._last_ts >= REPEAT_EVERY or sig.ts < self._last_ts
        if key == self._last and not due:
            return False
        await self.history.record_signal(sig, signal_inputs(snap))
        self._last, self._last_ts = key, sig.ts
        self.logged += 1
        return True
