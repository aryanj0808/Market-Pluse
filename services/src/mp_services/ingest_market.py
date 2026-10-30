"""ingest-market: provider -> Redis cache (+ Timescale recording)."""

from __future__ import annotations

import logging

from mp_core.health import heartbeat
from mp_core.pipeline import Ingestor
from mp_core.settings import get_settings
from mp_core.store import MarketStore, RedisKV
from mp_core.store.history import History
from mp_services._common import make_live_provider, make_provider, run_async, setup_logging

log = logging.getLogger("ingest-market")


async def run() -> None:
    s = get_settings()
    provider = make_provider(s) if s.provider == "replay" else await make_live_provider(s)
    kv = RedisKV(s.redis_url)
    history = await History(s.database_url).connect() if (s.record and s.database_url) else None
    if history is None:
        log.warning("recording disabled (MP_RECORD=false or DATABASE_URL unset)")
    ing = Ingestor(provider, MarketStore(kv), s.default_symbol, history=history, rate=s.risk_free_rate)
    log.info(
        "ingesting %s from %s (source=%s, mode=%s, speed=%s)",
        ing.symbol,
        s.provider,
        provider.source,
        ing.mode,
        s.replay_speed,
    )
    try:
        async with heartbeat(kv, "ingest-market", lambda: ing.stats):
            stats = await ing.run()
        log.info("stream ended: %s", stats)
    finally:
        await provider.close()
        if history is not None:
            await history.close()
        await kv.close()


def main() -> None:
    setup_logging()
    run_async(run())
