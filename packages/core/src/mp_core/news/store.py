"""News cache in Redis (via the KV interface). News is global; scoring per index happens on read.

Keys:
    mp:news:item:{id}   ScoredNewsItem
    mp:news:index       [{id, published_at, tokens}] oldest first (dedupe + range queries)
    mp:news:health      [FeedHealth]
    mp:news:poll        Sourced[int]: as_of = last completed poll, value = items in cache
    mp:news:version     incremented on every poll that changed something (cheap change check)
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from pydantic import TypeAdapter

from mp_core.models import Source, Sourced, stamp
from mp_core.news.dedupe import find_duplicate
from mp_core.news.models import FeedHealth, ScoredNewsItem
from mp_core.store.kv import KV

_POLL = TypeAdapter(Sourced[int])
_HEALTH = TypeAdapter(list[FeedHealth])


class NewsStore:
    def __init__(self, kv: KV, *, keep: timedelta = timedelta(days=21), max_items: int = 5000) -> None:
        self.kv = kv
        self.keep = keep
        self.max_items = max_items
        self._index: list[dict] | None = None  # cached by the single writer (ingest-news)

    async def reset(self) -> int:
        self._index = None
        return await self.kv.delete_prefix("mp:news:")

    async def _load_index(self) -> list[dict]:
        if self._index is None:
            raw = await self.kv.get("mp:news:index")
            self._index = json.loads(raw) if raw else []
        return self._index

    async def get(self, item_id: str) -> ScoredNewsItem | None:
        raw = await self.kv.get(f"mp:news:item:{item_id}")
        return ScoredNewsItem.model_validate_json(raw) if raw else None

    async def put(self, s: ScoredNewsItem, now: datetime) -> None:
        await self.kv.set(f"mp:news:item:{s.id}", s.model_dump_json())
        idx = [e for e in await self._load_index() if e["id"] != s.id]
        idx.append(
            {"id": s.id, "published_at": s.item.published_at.isoformat(), "tokens": s.title_tokens, "status": s.status}
        )
        cutoff = (now - self.keep).isoformat()
        idx = sorted((e for e in idx if e["published_at"] >= cutoff), key=lambda e: e["published_at"])
        self._index = idx[-self.max_items :]
        await self.kv.set("mp:news:index", json.dumps(self._index, separators=(",", ":")))

    # Syndicated copies appear within hours; an identical headline a day later is usually a new
    # report (and an exact repost is already caught by URL identity).
    async def find_duplicate(
        self, tokens: list[str], published_at: datetime, window: timedelta = timedelta(hours=6)
    ) -> str | None:
        lo, hi = (published_at - window).isoformat(), (published_at + window).isoformat()
        recent = [
            (e["id"], e["tokens"])
            for e in await self._load_index()
            if lo <= e["published_at"] <= hi and e["status"] != "duplicate"
        ]
        return find_duplicate(tokens, recent)

    async def recent(self, since: datetime, until: datetime) -> list[ScoredNewsItem]:
        """Items published in [since, until], oldest first. Reads the index fresh (for readers in
        other processes)."""
        raw = await self.kv.get("mp:news:index")
        idx = json.loads(raw) if raw else []
        lo, hi = since.isoformat(), until.isoformat()
        ids = [e["id"] for e in idx if lo <= e["published_at"] <= hi]
        docs = await self.kv.mget([f"mp:news:item:{i}" for i in ids])
        return [ScoredNewsItem.model_validate_json(d) for d in docs if d]

    async def put_health(self, health: list[FeedHealth]) -> None:
        await self.kv.set("mp:news:health", _HEALTH.dump_json(health).decode())

    async def get_health(self) -> list[FeedHealth]:
        raw = await self.kv.get("mp:news:health")
        return _HEALTH.validate_json(raw) if raw else []

    async def mark_poll(self, now: datetime, source: Source, ttl_seconds: float, changed: bool) -> None:
        n = len(await self._load_index())
        await self.kv.set(
            "mp:news:poll", stamp(n, source=source, as_of=now, now=now, ttl_seconds=ttl_seconds).model_dump_json()
        )
        if changed:
            v = int(await self.kv.get("mp:news:version") or 0) + 1
            await self.kv.set("mp:news:version", str(v))

    async def last_poll(self) -> Sourced[int] | None:
        raw = await self.kv.get("mp:news:poll")
        return _POLL.validate_json(raw) if raw else None

    async def version(self) -> int:
        return int(await self.kv.get("mp:news:version") or 0)
