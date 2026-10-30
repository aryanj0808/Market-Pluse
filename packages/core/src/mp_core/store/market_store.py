"""Typed access to the Redis cache shared by ingest, engine, MCP servers and gateway.

Keys (one namespace per symbol):
    mp:clock                              {"mode": "replay"|"live", "now": iso}  - shared clock
    mp:{SYM}:quote:{instrument_id}        Sourced[Quote]
    mp:{SYM}:expiries                     [iso dates]
    mp:{SYM}:chain:{expiry}               Sourced[OptionChain]   (+ :prev = the one before)
    mp:{SYM}:candles1m:{instrument_id}    Sourced[list[Candle]]  today's completed 1m bars
    mp:{SYM}:daily:{instrument_id}        Sourced[list[Candle]]  recent daily bars
    mp:{SYM}:constituents                 Sourced[list[Constituent]]
    mp:{SYM}:future_key                   instrument id of the front-month future
    mp:macro                              MacroSnapshot
    mp:{SYM}:snapshot                     MarketSnapshot
Channels:
    mp:{SYM}:raw        ingest -> engine   ("quote" | "chain" | "candles" | ...)
    mp:{SYM}:updates    engine -> gateway  ({"version": n, "computed_at": iso})
"""

from __future__ import annotations

import json
import time
from datetime import UTC, date, datetime
from typing import Any

from pydantic import TypeAdapter

from mp_core.models import Candle, Constituent, InstrumentKey, MacroSnapshot, OptionChain, Quote, Sourced
from mp_core.snapshot import EngineInputs, MarketSnapshot
from mp_core.store.kv import KV

_QUOTE = TypeAdapter(Sourced[Quote])
_CHAIN = TypeAdapter(Sourced[OptionChain])
_CANDLES = TypeAdapter(Sourced[list[Candle]])
_CONS = TypeAdapter(Sourced[list[Constituent]])


def _sym(symbol: str) -> str:
    return InstrumentKey.parse(symbol).symbol


class MarketStore:
    def __init__(self, kv: KV) -> None:
        self.kv = kv
        self._parse_cache: dict[str, tuple[str, object]] = {}

    # ------------------------------------------------------------------ clock
    async def set_clock(self, now: datetime, mode: str) -> None:
        await self.kv.set("mp:clock", json.dumps({"mode": mode, "now": now.astimezone(UTC).isoformat()}))

    async def now(self) -> datetime:
        """Replay time when a replay is driving the system, else wall-clock time. Staleness is
        always judged against this, so a paused replay looks stale rather than fresh."""
        raw = await self.kv.get("mp:clock")
        if raw:
            c = json.loads(raw)
            if c.get("mode") == "replay":
                return datetime.fromisoformat(c["now"])
        return datetime.now(UTC)

    async def clock_mode(self) -> str:
        raw = await self.kv.get("mp:clock")
        return json.loads(raw)["mode"] if raw else "live"

    async def reset(self, symbol: str) -> int:
        """Drop all cached data for a symbol (and macro). Used when a replay starts, so data from
        a previous run - possibly *later* than the new start - can never leak in as look-ahead."""
        return await self.kv.delete_prefix(f"mp:{_sym(symbol)}:") + await self.kv.delete_prefix("mp:macro")

    # ------------------------------------------------------------------ raw data
    async def put_quote(self, symbol: str, q: Sourced[Quote]) -> None:
        await self.kv.set(f"mp:{_sym(symbol)}:quote:{q.value.key.id}", q.model_dump_json())

    async def get_quote(self, symbol: str, key: InstrumentKey) -> Sourced[Quote] | None:
        raw = await self.kv.get(f"mp:{_sym(symbol)}:quote:{key.id}")
        return _QUOTE.validate_json(raw) if raw else None

    async def put_chain(self, symbol: str, c: Sourced[OptionChain]) -> bool:
        """Store a chain; the previous one is kept as `:prev`. Returns False if unchanged."""
        k = f"mp:{_sym(symbol)}:chain:{c.value.expiry}"
        old = await self.kv.get(k)
        if old and _CHAIN.validate_json(old).value.ts >= c.value.ts:
            return False
        if old:
            await self.kv.set(k + ":prev", old)
        await self.kv.set(k, c.model_dump_json())
        return True

    async def get_chain(self, symbol: str, expiry: date, prev: bool = False) -> Sourced[OptionChain] | None:
        raw = await self.kv.get(f"mp:{_sym(symbol)}:chain:{expiry}" + (":prev" if prev else ""))
        return _CHAIN.validate_json(raw) if raw else None

    async def put_expiries(self, symbol: str, expiries: list[date]) -> None:
        await self.kv.set(f"mp:{_sym(symbol)}:expiries", json.dumps([e.isoformat() for e in expiries]))

    async def get_expiries(self, symbol: str) -> list[date]:
        raw = await self.kv.get(f"mp:{_sym(symbol)}:expiries")
        return [date.fromisoformat(x) for x in json.loads(raw)] if raw else []

    async def put_candles(self, symbol: str, kind: str, key: InstrumentKey, c: Sourced[list[Candle]]) -> None:
        if kind not in ("candles1m", "daily"):
            raise ValueError(kind)
        await self.kv.set(f"mp:{_sym(symbol)}:{kind}:{key.id}", c.model_dump_json())

    async def get_candles(self, symbol: str, kind: str, key: InstrumentKey) -> Sourced[list[Candle]] | None:
        raw = await self.kv.get(f"mp:{_sym(symbol)}:{kind}:{key.id}")
        return _CANDLES.validate_json(raw) if raw else None

    async def put_constituents(self, symbol: str, c: Sourced[list[Constituent]]) -> None:
        await self.kv.set(f"mp:{_sym(symbol)}:constituents", c.model_dump_json())

    async def get_constituents(self, symbol: str) -> Sourced[list[Constituent]] | None:
        raw = await self.kv.get(f"mp:{_sym(symbol)}:constituents")
        return _CONS.validate_json(raw) if raw else None

    async def put_macro(self, m: MacroSnapshot) -> None:
        await self.kv.set("mp:macro", m.model_dump_json())

    async def get_macro(self) -> MacroSnapshot | None:
        raw = await self.kv.get("mp:macro")
        return MacroSnapshot.model_validate_json(raw) if raw else None

    async def put_future_key(self, symbol: str, key: InstrumentKey) -> None:
        await self.kv.set(f"mp:{_sym(symbol)}:future_key", key.id)

    async def get_future_key(self, symbol: str) -> InstrumentKey | None:
        raw = await self.kv.get(f"mp:{_sym(symbol)}:future_key")
        return InstrumentKey.parse(raw) if raw else None

    # ------------------------------------------------------------------ engine
    def _cached(self, rkey: str, raw: str | None, parse):
        """Parse `raw` once: while the stored JSON is unchanged, the previously parsed object is
        reused. Option chains change once a minute but are read on every snapshot (4/s)."""
        if raw is None:
            return None
        hit = self._parse_cache.get(rkey)
        if hit is not None and hit[0] == raw:
            return hit[1]
        obj = parse(raw)
        self._parse_cache[rkey] = (raw, obj)
        return obj

    async def load_inputs(self, symbol: str) -> EngineInputs | None:
        """Everything build_snapshot needs, in two Redis round trips. None until ingest has written
        an index quote."""
        sym = _sym(symbol)
        key = InstrumentKey.parse(sym)
        exp_raw, fut_raw = await self.kv.mget([f"mp:{sym}:expiries", f"mp:{sym}:future_key"])
        expiries = [date.fromisoformat(x) for x in json.loads(exp_raw)] if exp_raw else []
        fut = InstrumentKey.parse(fut_raw) if fut_raw else None
        keys = {
            "quote": f"mp:{sym}:quote:{key.id}",
            "daily": f"mp:{sym}:daily:{key.id}",
            "constituents": f"mp:{sym}:constituents",
            "macro": "mp:macro",
        }
        if fut is not None:
            keys["future_quote"] = f"mp:{sym}:quote:{fut.id}"
            keys["future_1m"] = f"mp:{sym}:candles1m:{fut.id}"
        for e in expiries:
            keys[f"chain:{e}"] = f"mp:{sym}:chain:{e}"
            keys[f"prev:{e}"] = f"mp:{sym}:chain:{e}:prev"
        raw = dict(zip(keys, await self.kv.mget(list(keys.values())), strict=True))
        quote = self._cached(keys["quote"], raw["quote"], _QUOTE.validate_json)
        if quote is None:
            return None
        chains, prev = {}, {}
        for e in expiries:
            if (c := self._cached(keys[f"chain:{e}"], raw[f"chain:{e}"], _CHAIN.validate_json)) is not None:
                chains[e] = c
            if (p := self._cached(keys[f"prev:{e}"], raw[f"prev:{e}"], _CHAIN.validate_json)) is not None:
                prev[e] = p
        if len(self._parse_cache) > 256:  # expired contracts: keep the cache bounded
            self._parse_cache.clear()
        return EngineInputs(
            symbol=sym,
            quote=quote,
            future_quote=self._cached(keys.get("future_quote", ""), raw.get("future_quote"), _QUOTE.validate_json),
            chains=chains,
            prev_chains=prev,
            daily=self._cached(keys["daily"], raw["daily"], _CANDLES.validate_json),
            future_1m_today=self._cached(keys.get("future_1m", ""), raw.get("future_1m"), _CANDLES.validate_json),
            constituents=self._cached(keys["constituents"], raw["constituents"], _CONS.validate_json),
            macro=self._cached("mp:macro", raw["macro"], MacroSnapshot.model_validate_json),
        )

    async def put_snapshot(self, snap: MarketSnapshot, tick_wall: float | None = None) -> None:
        await self.kv.set(f"mp:{snap.symbol}:snapshot", snap.model_dump_json())
        # wall-clock times (not replay time): when the engine published, and when the oldest raw
        # update folded into this snapshot arrived - so tick-to-screen latency can be measured
        meta = {
            "version": snap.version,
            "computed_at": snap.computed_at.isoformat(),
            "wall": time.time(),
            "tick_wall": tick_wall,
        }
        await self.kv.publish(f"mp:{snap.symbol}:updates", json.dumps(meta))

    async def get_snapshot_json(self, symbol: str) -> str | None:
        """Raw JSON - the fast path for the gateway (no parse/serialise round trip)."""
        return await self.kv.get(f"mp:{_sym(symbol)}:snapshot")

    async def get_snapshot(self, symbol: str) -> MarketSnapshot | None:
        raw = await self.get_snapshot_json(symbol)
        return MarketSnapshot.model_validate_json(raw) if raw else None

    # ------------------------------------------------------------------ events
    async def notify_raw(self, symbol: str, kind: str, **extra: Any) -> None:
        # wall-clock arrival time: lets the dashboard measure true tick-to-screen latency
        await self.kv.publish(f"mp:{_sym(symbol)}:raw", json.dumps({"kind": kind, "wall": time.time(), **extra}))

    def raw_events(self, symbol: str):
        return self.kv.subscribe(f"mp:{_sym(symbol)}:raw")

    def updates(self, symbol: str):
        return self.kv.subscribe(f"mp:{_sym(symbol)}:updates")
