"""healthcheck <service>: exit 0 if the service's heartbeat in Redis is fresh (Docker HEALTHCHECK)."""

from __future__ import annotations

import asyncio
import sys

from mp_core.health import check
from mp_core.settings import get_settings
from mp_core.store import RedisKV


async def _check(service: str) -> tuple[bool, str]:
    kv = RedisKV(get_settings().redis_url)
    try:
        return await check(kv, service)
    finally:
        await kv.close()


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: healthcheck <service>")
    try:
        ok, msg = asyncio.run(_check(sys.argv[1]))
    except Exception as ex:  # Redis unreachable counts as unhealthy
        ok, msg = False, f"{sys.argv[1]}: cannot read heartbeat ({type(ex).__name__})"
    print(msg)
    sys.exit(0 if ok else 1)
