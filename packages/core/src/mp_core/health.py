"""Service heartbeats: every background service writes `mp:health:{service}` every few seconds with
its wall-clock time and a few counters. Docker healthchecks and /api/health read them, so a service
that is alive but stuck (no heartbeat) is reported, not just one that crashed."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable
from typing import Any

from mp_core.store.kv import KV

log = logging.getLogger(__name__)

SERVICES = ("ingest-market", "engine", "ingest-news", "alerts", "evaluator")
DEFAULT_MAX_AGE = {"evaluator": 120.0}  # the evaluator sleeps between nightly runs but still beats


def key(service: str) -> str:
    return f"mp:health:{service}"


async def beat(kv: KV, service: str, info: dict[str, Any] | None = None) -> None:
    await kv.set(key(service), json.dumps({"wall": time.time(), **(info or {})}, default=str), ttl_seconds=600)


async def heartbeat_loop(
    kv: KV, service: str, info: Callable[[], dict[str, Any]] | None = None, every: float = 5.0
) -> None:
    """Run forever (cancel to stop). A failing beat is logged, never fatal."""
    while True:
        try:
            await beat(kv, service, info() if info else None)
        except Exception as ex:
            log.warning("heartbeat for %s failed: %s", service, ex)
        await asyncio.sleep(every)


@contextlib.asynccontextmanager
async def heartbeat(kv: KV, service: str, info: Callable[[], dict[str, Any]] | None = None, every: float = 5.0):
    task = asyncio.create_task(heartbeat_loop(kv, service, info, every))
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def read(kv: KV, services: tuple[str, ...] = SERVICES, max_age: float = 30.0) -> dict[str, dict]:
    raws = await kv.mget([key(s) for s in services])
    now = time.time()
    out = {}
    for s, raw in zip(services, raws, strict=True):
        if raw is None:
            out[s] = {"ok": False, "age_s": None, "status": "no heartbeat"}
            continue
        d = json.loads(raw)
        age = now - d.pop("wall")
        limit = DEFAULT_MAX_AGE.get(s, max_age)
        out[s] = {"ok": age <= limit, "age_s": round(age, 1), "status": "ok" if age <= limit else "stale", **d}
    return out


async def check(kv: KV, service: str, max_age: float | None = None) -> tuple[bool, str]:
    st = (await read(kv, (service,), max_age or DEFAULT_MAX_AGE.get(service, 30.0)))[service]
    return st["ok"], f"{service}: {st['status']}" + (f" ({st['age_s']}s ago)" if st["age_s"] is not None else "")
