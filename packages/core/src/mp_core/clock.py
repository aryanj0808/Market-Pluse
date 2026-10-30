"""Clocks. Everything time-dependent takes a Clock so replays have no look-ahead."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol

from mp_core.models.common import _require_aware


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class SimClock:
    """Manually driven clock for replays and tests. Never moves backwards."""

    def __init__(self, start: datetime) -> None:
        self._now = _require_aware(start)

    def now(self) -> datetime:
        return self._now

    def set(self, t: datetime) -> None:
        t = _require_aware(t)
        if t < self._now:
            raise ValueError(f"SimClock cannot move backwards ({t} < {self._now})")
        self._now = t

    def advance(self, delta: timedelta) -> None:
        self.set(self._now + delta)
