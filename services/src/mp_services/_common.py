"""Startup helpers shared by the service mains."""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime

from mp_core import market_calendar as cal
from mp_core.clock import SimClock
from mp_core.logs import configure_logging
from mp_core.providers.base import MarketDataProvider
from mp_core.providers.replay import ReplayProvider
from mp_core.settings import Settings


def run_async(coro) -> None:
    """asyncio.run with a loop async psycopg supports (Windows' default Proactor loop is not)."""
    if sys.platform == "win32":
        asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(coro)


def setup_logging() -> None:
    configure_logging(os.environ.get("MP_LOG_LEVEL", "INFO"))


async def make_live_provider(s: Settings) -> MarketDataProvider:
    if s.provider == "upstox":
        from mp_core.providers.upstox import provider_from_settings

        return await provider_from_settings()
    if s.provider == "kite":
        from mp_core.providers.kite import provider_from_settings as kite_provider

        return await kite_provider()
    raise SystemExit(f"unknown provider {s.provider!r}: use replay, upstox or kite")


def make_provider(s: Settings) -> MarketDataProvider:
    if s.provider != "replay":
        raise SystemExit("live providers are created with make_live_provider()")
    probe = ReplayProvider(s.replay_dir, SimClock(datetime.fromisoformat("2000-01-01T00:00:00+00:00")))
    if s.replay_start:
        start = datetime.fromisoformat(s.replay_start)
        start = start if start.tzinfo else start.replace(tzinfo=cal.IST)
    else:  # 2nd day of the dataset (the first has no previous close), just before the open
        start = cal.session_bounds(probe.days[min(1, len(probe.days) - 1)])[0]
    return ReplayProvider(s.replay_dir, SimClock(start), speed=s.replay_speed or None)
