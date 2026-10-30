"""evaluator: nightly scoring of past signals against actual prices (real data only)."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime, time, timedelta

from mp_core import market_calendar as cal
from mp_core.health import heartbeat
from mp_core.settings import get_settings
from mp_core.signals.evaluation import compute_stats, evaluate_pending
from mp_core.store import RedisKV
from mp_core.store.history import History
from mp_services._common import run_async, setup_logging

log = logging.getLogger("evaluator")
RUN_AT = time(18, 0)  # IST, after the close and the exchange's EOD files


async def run_once(history: History, symbol: str) -> None:
    now = datetime.now(UTC)
    stats = await evaluate_pending(history, symbol, now)
    summary = compute_stats(
        symbol, await history.outcomes(symbol), n_synthetic_signals=await history.count_synthetic_signals(symbol)
    )
    log.info("%s: %s | %s", symbol, stats, summary.message)


async def run() -> None:
    s = get_settings()
    if not s.database_url:
        raise SystemExit("evaluator needs DATABASE_URL")
    history = History(s.database_url)  # connects lazily; a database outage is retried, not fatal
    once = os.environ.get("MP_EVAL_ONCE", "").lower() in ("1", "true", "yes")
    kv = RedisKV(s.redis_url)
    state = {"runs": 0, "last_run": None}
    try:
        async with heartbeat(kv, "evaluator", lambda: state, every=30):
            await _loop(history, s.default_symbol, once, state)
    finally:
        await history.close()
        await kv.close()


async def _loop(history: History, symbol: str, once: bool, state: dict) -> None:
    while True:
        try:
            await run_once(history, symbol)
            state["runs"] += 1
            state["last_run"] = datetime.now(cal.IST).isoformat()
        except Exception:  # database down etc.: try again at the next run instead of dying
            log.exception("evaluation run failed")
        if once:
            return
        now = datetime.now(cal.IST)
        nxt = datetime.combine(now.date(), RUN_AT, cal.IST)
        if nxt <= now:
            nxt += timedelta(days=1)
        await asyncio.sleep((nxt - now).total_seconds())


def main() -> None:
    setup_logging()
    run_async(run())
