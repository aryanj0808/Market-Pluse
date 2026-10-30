"""ingest-news: poll sources -> de-duplicate -> classify new items (once) -> cache + record."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Protocol

from mp_core.models import Source
from mp_core.news.classifier import ClassifyResult
from mp_core.news.dedupe import title_tokens
from mp_core.news.feeds import FeedError, NewsSource
from mp_core.news.models import FeedHealth, RawNewsItem, ScoredNewsItem
from mp_core.news.store import NewsStore
from mp_core.store.market_store import MarketStore

log = logging.getLogger(__name__)
FUTURE_TOLERANCE = timedelta(minutes=5)
MAX_FEED_BACKOFF_S = 1800.0  # publisher clocks drift; anything later is not "now" yet
FINAL = ("classified", "dropped", "duplicate")


class Classifier(Protocol):
    async def classify(self, item: RawNewsItem) -> ClassifyResult: ...


class NewsHistory(Protocol):
    async def get_news(self, item_id: str) -> ScoredNewsItem | None: ...
    async def record_news(self, item: ScoredNewsItem) -> None: ...


class NewsIngestor:
    def __init__(
        self,
        sources: Sequence[NewsSource],
        store: NewsStore,
        classifier: Classifier | None,
        *,
        source: Source,
        market: MarketStore | None = None,
        history: NewsHistory | None = None,
        symbols: Sequence[str] = ("NSE:BANKNIFTY", "NSE:NIFTY", "NSE:FINNIFTY"),
        ttl_seconds: float = 900,
        replay: bool = False,
        base_backoff_s: float = 60.0,
    ) -> None:
        self.sources = list(sources)
        self.store = store
        self.classifier = classifier
        self.source = source
        self.market = market
        self.history = history
        self.symbols = list(symbols)
        self.ttl_seconds = ttl_seconds
        self.replay = replay  # fixture news follows the replay clock - never wall-clock time
        self.base_backoff_s = base_backoff_s
        self._backoff: dict[str, tuple[int, float]] = {}  # feed -> (consecutive failures, retry at)
        self._mono = time.monotonic
        self._last_now: datetime | None = None
        self.health = {s.name: FeedHealth(feed=s.name, url=s.url, verified=s.verified) for s in self.sources}
        self.stats = {
            "polls": 0,
            "fetched": 0,
            "new": 0,
            "duplicates": 0,
            "classified": 0,
            "dropped": 0,
            "unclassified": 0,
            "from_history": 0,
            "feed_errors": 0,
            "history_errors": 0,
            "feeds_backing_off": 0,
        }

    async def _history_get(self, item_id: str):
        if self.history is None:
            return None
        try:
            return await self.history.get_news(item_id)
        except Exception as ex:  # database down: fall back to the Redis cache alone
            self.stats["history_errors"] += 1
            log.warning("news history lookup failed: %s", ex)
            return None

    async def now(self) -> datetime:
        """The shared clock: replay time during a replay (so no future news leaks in)."""
        return await self.market.now() if self.market is not None else datetime.now(UTC)

    async def _fetch(self, src: NewsSource, now: datetime) -> list[RawNewsItem]:
        """Fetch one feed. A failing feed backs off exponentially (poll x 2^n, capped at 30 min) so a
        dead or blocked feed is not hammered - and not logged - on every poll."""
        h = self.health[src.name]
        fails, next_ok = self._backoff.get(src.name, (0, 0.0))
        if self._mono() < next_ok:
            self.stats["feeds_backing_off"] += 1
            return []
        try:
            items = await src.fetch(now)
        except FeedError as ex:
            fails += 1
            delay = min(MAX_FEED_BACKOFF_S, self.base_backoff_s * 2 ** (fails - 1))
            self._backoff[src.name] = (fails, self._mono() + delay)
            self.stats["feed_errors"] += 1
            self.health[src.name] = h.model_copy(
                update={
                    "last_attempt": now,
                    "last_error": f"{ex} (failure {fails}, retry in {delay:.0f}s)",
                    "items_last_poll": 0,
                }
            )
            if fails <= 3 or fails % 10 == 0:
                log.warning("feed %s failed (%d in a row, backing off %.0fs): %s", src.name, fails, delay, ex)
            return []
        self._backoff.pop(src.name, None)
        self.health[src.name] = h.model_copy(
            update={"last_attempt": now, "last_ok": now, "last_error": None, "items_last_poll": len(items)}
        )
        return items

    def _scored(self, item: RawNewsItem, tokens: list[str], **kw) -> ScoredNewsItem:
        return ScoredNewsItem(id=item.id, item=item, title_tokens=tokens, **kw)

    async def poll_once(self) -> dict[str, int]:
        if self.replay and (self.market is None or await self.market.clock_mode() != "replay"):
            # Before ingest-market has set the replay clock, "now" would be wall-clock time - weeks
            # after the replay - and every fixture item would be ingested (and pruned) at once.
            self.stats["waiting_for_clock"] = self.stats.get("waiting_for_clock", 0) + 1
            return dict(self.stats)
        now = await self.now()
        if self.replay and self._last_now is not None and now < self._last_now - timedelta(minutes=1):
            log.info("replay clock moved back (%s -> %s): resetting the news cache", self._last_now, now)
            await self.store.reset()
        self._last_now = now
        batches = await asyncio.gather(*(self._fetch(s, now) for s in self.sources))
        items = sorted((i for b in batches for i in b), key=lambda i: (i.published_at, i.feed, i.url))
        self.stats["polls"] += 1
        self.stats["fetched"] += len(items)

        pending: list[tuple[RawNewsItem, list[str]]] = []
        changed = False
        for item in items:
            if item.published_at > now + FUTURE_TOLERANCE:
                continue
            existing = await self.store.get(item.id)
            if existing is not None and existing.status in FINAL:
                continue  # never re-score
            tokens = title_tokens(item.title)
            if existing is None:
                dup = await self.store.find_duplicate(tokens, item.published_at)
                if dup is not None:
                    await self.store.put(self._scored(item, tokens, status="duplicate", duplicate_of=dup), now)
                    self.stats["duplicates"] += 1
                    changed = True
                    continue
            if (cached := await self._history_get(item.id)) is not None and cached.status in FINAL:
                await self.store.put(cached, now)  # classified on an earlier run: reuse, don't pay twice
                self.stats["from_history"] += 1
                changed = True
                continue
            # visible (and de-dupable within this batch) before the model answers
            await self.store.put(self._scored(item, tokens, status="unclassified"), now)
            self.stats["new"] += existing is None
            pending.append((item, tokens))
            changed = True

        if pending and self.classifier is not None:
            results = await asyncio.gather(*(self.classifier.classify(i) for i, _ in pending))
        else:
            results = [None] * len(pending)
        for (item, tokens), r in zip(pending, results, strict=True):
            if r is None:
                scored = self._scored(item, tokens, status="unclassified", error="no classifier configured")
            else:
                scored = self._scored(
                    item,
                    tokens,
                    status=r.status,
                    classification=r.classification,
                    model=r.model,
                    prompt_version=r.prompt_version,
                    classified_at=r.classified_at,
                    error=r.error,
                )
            self.stats[scored.status] += 1
            await self.store.put(scored, now)
            if self.history is not None and scored.status != "unclassified":
                try:
                    await self.history.record_news(scored)
                except Exception as ex:  # Redis still holds the result; the DB copy is best-effort
                    self.stats["history_errors"] += 1
                    log.warning("recording news %s failed: %s", item.id, ex)

        await self.store.put_health(list(self.health.values()))
        await self.store.mark_poll(now, self.source, self.ttl_seconds, changed)
        if self.market is not None and changed:
            for sym in self.symbols:
                await self.market.notify_raw(sym, "news")
        return dict(self.stats)

    async def run(self, poll_seconds: float, stop: asyncio.Event | None = None) -> None:
        while stop is None or not stop.is_set():
            try:
                await self.poll_once()
            except Exception:  # one bad poll must not stop the service
                log.exception("news poll failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait() if stop else asyncio.sleep(poll_seconds), timeout=poll_seconds)
