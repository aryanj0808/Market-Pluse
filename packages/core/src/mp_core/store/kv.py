"""Minimal key-value + pub/sub interface. Redis in production, in-memory for unit tests."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Protocol


class KV(Protocol):
    async def get(self, key: str) -> str | None: ...
    async def mget(self, keys: list[str]) -> list[str | None]: ...
    async def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None: ...
    async def publish(self, channel: str, message: str) -> None: ...
    def subscribe(self, channel: str) -> AsyncIterator[str]: ...
    async def delete_prefix(self, prefix: str) -> int: ...
    async def close(self) -> None: ...


class MemoryKV:
    def __init__(self) -> None:
        self._data: dict[str, str] = {}
        self._subs: dict[str, list[tuple[asyncio.AbstractEventLoop, asyncio.Queue[str]]]] = defaultdict(list)

    async def get(self, key: str) -> str | None:
        return self._data.get(key)

    async def mget(self, keys: list[str]) -> list[str | None]:
        return [self._data.get(k) for k in keys]

    async def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        self._data[key] = value

    async def publish(self, channel: str, message: str) -> None:
        current = asyncio.get_running_loop()
        for loop, q in list(self._subs[channel]):
            if loop is current:
                q.put_nowait(message)
            else:  # subscriber lives on another thread's loop
                loop.call_soon_threadsafe(q.put_nowait, message)

    async def subscribe(self, channel: str) -> AsyncIterator[str]:
        entry = (asyncio.get_running_loop(), asyncio.Queue())
        self._subs[channel].append(entry)
        try:
            while True:
                yield await entry[1].get()
        finally:
            self._subs[channel].remove(entry)

    async def delete_prefix(self, prefix: str) -> int:
        doomed = [k for k in self._data if k.startswith(prefix)]
        for k in doomed:
            del self._data[k]
        return len(doomed)

    async def close(self) -> None:
        return None


class RedisKV:
    def __init__(self, url: str) -> None:
        import redis.asyncio as aioredis

        self._r = aioredis.Redis.from_url(url, decode_responses=True, socket_connect_timeout=5)

    async def get(self, key: str) -> str | None:
        return await self._r.get(key)

    async def mget(self, keys: list[str]) -> list[str | None]:
        return await self._r.mget(keys) if keys else []

    async def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        await self._r.set(key, value, ex=ttl_seconds)

    async def publish(self, channel: str, message: str) -> None:
        await self._r.publish(channel, message)

    async def subscribe(self, channel: str) -> AsyncIterator[str]:
        ps = self._r.pubsub()
        await ps.subscribe(channel)
        try:
            async for msg in ps.listen():
                if msg["type"] == "message":
                    yield msg["data"]
        finally:
            await ps.unsubscribe(channel)
            await ps.aclose()

    async def delete_prefix(self, prefix: str) -> int:
        n = 0
        async for k in self._r.scan_iter(match=prefix.replace("*", r"\*") + "*", count=500):
            n += await self._r.delete(k)
        return n

    async def ping(self) -> bool:
        return bool(await self._r.ping())

    async def close(self) -> None:
        await self._r.aclose()
