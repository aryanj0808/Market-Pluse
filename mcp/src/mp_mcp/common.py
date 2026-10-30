"""Shared plumbing for the MCP servers: compact provenance envelopes, dependencies, startup."""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

from mp_core.digest import compact
from mp_core.logs import configure_logging
from mp_core.models import InstrumentKey, Sourced, derived_source
from mp_core.net import allowed_hosts
from mp_core.registry import get_index
from mp_core.settings import get_settings
from mp_core.snapshot import MarketSnapshot
from mp_core.store import MarketStore, RedisKV
from mp_core.store.history import History

log = logging.getLogger(__name__)


@dataclass
class Deps:
    store: MarketStore
    database_url: str | None = None
    _history: History | None = field(default=None, repr=False)

    async def history(self) -> History:
        if self._history is None:
            if not self.database_url:
                raise ToolError("history unavailable: DATABASE_URL is not configured for this server")
            self._history = await History(self.database_url).connect()
        return self._history


def dumps(d: dict) -> str:
    """Compact JSON for tool output (no indentation: every token is read by the model)."""
    return json.dumps(d, separators=(",", ":"), ensure_ascii=False)


def envelope(symbol: str, parts: list[Sourced], data: Any, now: datetime, **extra: Any) -> dict:
    """{symbol, source, as_of, stale, data}. Provenance is that of the oldest / stalest part, and
    synthetic is contagious. Staleness is re-evaluated against the shared clock at call time."""
    parts = [p.restamp(now) for p in parts if p is not None]
    if not parts:
        raise ToolError(f"no data for {symbol}")
    return {
        "symbol": symbol,
        "source": derived_source([p.source for p in parts]).value,
        "as_of": min(p.as_of for p in parts).isoformat(),
        "stale": any(p.stale for p in parts),
        "now": now.isoformat(),
        **extra,
        "data": compact(data),
    }


def check_symbol(symbol: str) -> InstrumentKey:
    try:
        get_index(symbol)
        return InstrumentKey.parse(symbol if ":" in symbol else f"NSE:{symbol}")
    except (KeyError, ValueError) as ex:
        raise ToolError(str(ex)) from None


async def require_snapshot(deps: Deps, symbol: str) -> MarketSnapshot:
    snap = await deps.store.get_snapshot(symbol)
    if snap is None:
        raise ToolError(f"no snapshot for {symbol} yet - is the engine running?")
    return snap


def deps_from_env() -> Deps:
    s = get_settings()
    return Deps(store=MarketStore(RedisKV(s.redis_url)), database_url=s.database_url)


def serve(factory: Callable[[Deps], MCPServer], default_port: int) -> None:
    """Run a server. MP_MCP_TRANSPORT=stdio (default, for Claude Desktop/Code) or streamable-http."""
    configure_logging(os.environ.get("MP_LOG_LEVEL", "INFO"))
    if sys.platform == "win32":  # async psycopg (get_candles) needs a selector loop on Windows
        import asyncio

        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    server = factory(deps_from_env())
    transport = os.environ.get("MP_MCP_TRANSPORT", "stdio")
    if transport == "streamable-http":
        server.run(
            "streamable-http",
            host=os.environ.get("MP_MCP_HOST", "127.0.0.1"),
            port=int(os.environ.get("MP_MCP_PORT", default_port)),
            # The SDK only protects 127.0.0.1 binds; in Docker we bind 0.0.0.0, so set it explicitly.
            transport_security=TransportSecuritySettings(
                allowed_hosts=[f"{h}:*" for h in allowed_hosts()],
                allowed_origins=[f"http://{h}:*" for h in allowed_hosts()],
            ),
        )
    else:
        server.run("stdio")
