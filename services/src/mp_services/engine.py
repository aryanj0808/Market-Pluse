"""engine: raw updates in Redis -> MarketSnapshot in Redis."""

from __future__ import annotations

import logging

from mp_core.health import heartbeat
from mp_core.pipeline import Engine
from mp_core.settings import get_settings
from mp_core.signals.fusion import RiskConfig
from mp_core.store import MarketStore, RedisKV
from mp_core.store.history import History
from mp_services._common import run_async, setup_logging

log = logging.getLogger("engine")


async def run() -> None:
    s = get_settings()
    kv = RedisKV(s.redis_url)
    history = await History(s.database_url).connect() if s.database_url else None
    engine = Engine(
        MarketStore(kv),
        s.default_symbol,
        rate=s.risk_free_rate,
        risk=RiskConfig(s.capital, s.max_risk_pct),
        history=history,
    )
    log.info("engine for %s (signal logging %s)", engine.symbol, "on" if history else "off: no DATABASE_URL")
    try:
        async with heartbeat(kv, "engine", lambda: {"version": engine.version, "log_errors": engine.log_errors}):
            await engine.run()
    finally:
        if history is not None:
            await history.close()
        await kv.close()


def main() -> None:
    setup_logging()
    run_async(run())
