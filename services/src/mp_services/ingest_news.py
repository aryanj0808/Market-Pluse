"""ingest-news: poll feeds (or a synthetic fixture during replay) -> classify once -> Redis + Timescale."""

from __future__ import annotations

import logging
from pathlib import Path

from mp_core.health import heartbeat
from mp_core.models import Source
from mp_core.news.classifier import FixtureLabelClassifier, NewsClassifier
from mp_core.news.feeds import FixtureSource, http_client, rss_sources
from mp_core.news.pipeline import NewsIngestor
from mp_core.news.store import NewsStore
from mp_core.settings import Settings, get_settings
from mp_core.store import MarketStore, RedisKV
from mp_core.store.history import History
from mp_core.synthetic.news import load_labels
from mp_services._common import run_async, setup_logging

log = logging.getLogger("ingest-news")


def _classifier(s: Settings, fixture: Path | None):
    choice = s.news_classifier
    if choice in ("haiku", "auto"):
        clf = NewsClassifier.from_env()
        if clf is not None or choice == "haiku":
            return clf, "claude-haiku-5-5" if clf else "none (no Anthropic credentials)"
    if choice in ("fixture-labels", "auto") and fixture is not None:
        labels = fixture.with_name("news_labels.jsonl")
        if labels.exists():
            return FixtureLabelClassifier(load_labels(labels)), "fixture-labels (synthetic test labels, NOT an LLM)"
    return None, "none"


async def run() -> None:
    s = get_settings()
    mode = s.news_mode if s.news_mode != "auto" else ("fixture" if s.provider == "replay" else "live")
    kv = RedisKV(s.redis_url)
    market, store = MarketStore(kv), NewsStore(kv)
    history = await History(s.database_url).connect() if (s.record and s.database_url) else None
    client = None
    if mode == "fixture":
        fixture = Path(s.news_fixture or Path(s.replay_dir) / "news.jsonl")
        if not fixture.exists():
            raise SystemExit(f"news fixture {fixture} not found - run `mp synth-news`")
        sources, source = [FixtureSource(fixture, "synthetic")], Source.SYNTHETIC
        await store.reset()  # a replay may restart earlier in time: never keep later news
    else:
        fixture = None
        client = http_client()
        sources, source = rss_sources(client), Source.NEWS_FEED
    classifier, label = _classifier(s, fixture)
    log.info(
        "news mode=%s sources=%s classifier=%s poll=%ss", mode, [x.name for x in sources], label, s.news_poll_seconds
    )
    ing = NewsIngestor(
        sources,
        store,
        classifier,
        source=source,
        market=market,
        history=history,
        ttl_seconds=s.ttl.news,
        replay=mode == "fixture",
    )
    try:
        async with heartbeat(kv, "ingest-news", lambda: ing.stats):
            await ing.run(s.news_poll_seconds)
    finally:
        if client is not None:
            await client.aclose()
        if history is not None:
            await history.close()
        await kv.close()


def main() -> None:
    setup_logging()
    run_async(run())
