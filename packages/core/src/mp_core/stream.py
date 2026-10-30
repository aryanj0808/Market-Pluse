"""Snapshot streaming for the gateway's WebSocket: full snapshot first, then per-section diffs.

Messages (JSON):
    {"type": "snapshot", "now", "version", "snapshot": {...}}            on connect / after a gap
    {"type": "diff", "now", "version", "changed": {section: value}, "removed": [section...],
     "tick_wall", "engine_wall", "gateway_wall"}  wall-clock stamps     on engine updates
    {"type": "chain", "now", "expiry", "chain": Sourced[OptionChain]}     front chain changed
    {"type": "news", "now", "version"}                                     news cache changed (refetch)

Throttle - literally "max 4 per second": at most `max_per_sec` pushes in any trailing 1-second
window, and at least `min_gap` between two pushes. An update goes out at once when both allow it;
otherwise the burst is coalesced and only the newest state is sent when they do. Nothing is ever
queued behind a slow client. (A fixed 250 ms spacing would stack on top of the engine's own
spacing and add delay; the window does not.)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections import deque
from collections.abc import Awaitable, Callable

from mp_core.news.store import NewsStore
from mp_core.store.market_store import MarketStore

Send = Callable[[str], Awaitable[None]]


def _dumps(d: dict) -> str:
    return json.dumps(d, separators=(",", ":"))


class SnapshotStreamer:
    def __init__(
        self, store: MarketStore, symbol: str, send: Send, *, max_per_sec: float = 4.0, min_gap: float = 0.1
    ) -> None:
        self.store = store
        self.news = NewsStore(store.kv)
        self.symbol = symbol
        self.send = send
        self.max_per_sec = max_per_sec
        self.min_gap = min_gap
        self._sends: deque[float] = deque()
        self._sections: dict[str, str] = {}
        self._version: int | None = None
        self._chain_ts: str | None = None
        self._news_version: int | None = None
        self._last_meta: dict = {}
        self.sent = {"snapshot": 0, "diff": 0, "chain": 0, "news": 0}

    async def _now(self) -> str:
        return (await self.store.now()).isoformat()

    async def _emit(self, msg: dict) -> None:
        self.sent[msg["type"]] += 1
        if msg["type"] in ("snapshot", "diff"):  # the rate limit applies to what clients receive
            self._sends.append(time.perf_counter())  # high resolution: monotonic() ticks at 15.6 ms on Windows
        await self.send(_dumps(msg))

    async def push(self) -> None:
        """Send whatever changed since the last push (or a full snapshot the first time)."""
        raw = await self.store.get_snapshot_json(self.symbol)
        if raw is None:
            return
        snap = json.loads(raw)
        sections = {k: _dumps(v) if isinstance(v, (dict, list)) else json.dumps(v) for k, v in snap.items()}
        now = await self._now()
        if self._version is None:
            await self._emit({"type": "snapshot", "now": now, "version": snap["version"], "snapshot": snap})
        elif snap["version"] != self._version:
            changed = {k: snap[k] for k, v in sections.items() if self._sections.get(k) != v}
            removed = [k for k in self._sections if k not in sections]
            await self._emit(
                {
                    "type": "diff",
                    "now": now,
                    "version": snap["version"],
                    "changed": changed,
                    "removed": removed,
                    "engine_wall": self._last_meta.get("wall"),
                    "tick_wall": self._last_meta.get("tick_wall"),
                    "gateway_wall": time.time(),
                }
            )
        self._sections, self._version = sections, snap["version"]

        expiries = snap.get("expiries") or []
        if expiries:
            front = expiries[0]["value"]["expiry"]
            chain_raw = await self.store.kv.get(f"mp:{self.symbol}:chain:{front}")
            if chain_raw and chain_raw != self._chain_ts:  # compare the text; parse only on change
                self._chain_ts = chain_raw
                await self._emit({"type": "chain", "now": now, "expiry": front, "chain": json.loads(chain_raw)})
        nv = await self.news.version()
        if nv != self._news_version:
            if self._news_version is not None or nv:
                await self._emit({"type": "news", "now": now, "version": nv})
            self._news_version = nv

    def wait_needed(self, now: float) -> float:
        """Seconds until a push is allowed under both limits (0 = now)."""
        while self._sends and now - self._sends[0] >= 1.0:
            self._sends.popleft()
        gap = self.min_gap - (now - self._sends[-1]) if self._sends else 0.0
        rate = self._sends[0] + 1.0 - now if len(self._sends) >= self.max_per_sec else 0.0
        return max(0.0, gap, rate)

    async def run(self, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        dirty = asyncio.Event()

        async def listen() -> None:
            async for msg in self.store.updates(self.symbol):
                with contextlib.suppress(ValueError, TypeError):
                    self._last_meta = json.loads(msg)
                dirty.set()

        listener = asyncio.create_task(listen())
        try:
            await self.push()
            while not stop.is_set():
                waiter = asyncio.create_task(dirty.wait())
                stopper = asyncio.create_task(stop.wait())
                await asyncio.wait({waiter, stopper}, return_when=asyncio.FIRST_COMPLETED)
                waiter.cancel()
                stopper.cancel()
                if stop.is_set():
                    break
                # coalesce the burst; the newest state goes out when allowed. Re-checked after every
                # wake-up: event-loop timers may fire up to one clock tick early.
                while (wait := self.wait_needed(time.perf_counter())) > 0 and not stop.is_set():
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), timeout=wait + 0.001)
                if stop.is_set():
                    break
                dirty.clear()
                await self.push()
        finally:
            listener.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await listener
