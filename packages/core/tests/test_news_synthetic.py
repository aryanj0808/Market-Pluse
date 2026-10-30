"""Synthetic news fixtures + news inside the engine snapshot."""

import json
from datetime import date, timedelta

import pytest

from mp_core.clock import SimClock
from mp_core.dataset import DayLabel
from mp_core.models import Source
from mp_core.news.classifier import FixtureLabelClassifier
from mp_core.news.feeds import FixtureSource
from mp_core.news.pipeline import NewsIngestor
from mp_core.news.store import NewsStore
from mp_core.pipeline import Engine, Ingestor
from mp_core.providers.replay import ReplayProvider
from mp_core.store import MarketStore, MemoryKV
from mp_core.synthetic.news import generate_news, load_labels

from conftest import ist


@pytest.fixture(scope="module")
def news_files(synth_dir):
    return generate_news(synth_dir, seed=7)


def test_deterministic(synth_dir, news_files, tmp_path):
    first = news_files[0].read_text()
    generate_news(synth_dir, seed=7)
    assert news_files[0].read_text() == first


def test_unmistakably_synthetic(news_files):
    items = FixtureSource(news_files[0]).items
    assert items and all(i.source is Source.SYNTHETIC for i in items)
    assert all(i.url.startswith("https://news.synthetic.invalid/") for i in items)  # reserved TLD
    assert {i.feed for i in items} == {"synthetic_wire_a", "synthetic_wire_b"}
    assert set(load_labels(news_files[1])) == {i.id for i in items}  # every item labelled


def test_gap_days_get_matching_preopen_story(synth_dir, news_files):
    items = FixtureSource(news_files[0]).items
    labels = load_labels(news_files[1])
    gaps = [DayLabel.model_validate(x) for x in json.loads((synth_dir / "events.json").read_text())]
    for g in (g for g in gaps if g.is_gap_day):
        pre = [
            i
            for i in items
            if i.published_at.astimezone(ist(g.day, 0, 0).tzinfo).date() == g.day
            and i.title.startswith("Global markets")
        ]
        assert pre, f"no pre-open story on gap day {g.day}"
        assert (labels[pre[0].id].sentiment > 0) == (g.gap_pct > 0)
        assert pre[0].published_at < ist(g.day, 9, 15)


async def test_news_reaches_the_snapshot(synth_dir, news_files):
    kv = MemoryKV()
    market = MarketStore(kv)
    p = ReplayProvider(synth_dir, SimClock(ist(date(2026, 9, 23), 9, 15)))
    await Ingestor(p, market, "NSE:BANKNIFTY").run(max_ticks=60)
    ing = NewsIngestor(
        [FixtureSource(news_files[0], "synthetic")],
        NewsStore(kv),
        FixtureLabelClassifier(load_labels(news_files[1])),
        source=Source.SYNTHETIC,
        market=market,
    )
    stats = await ing.poll_once()
    assert stats["classified"] > 20 and stats["duplicates"] > 0 and stats["dropped"] == 0
    engine = Engine(market, "NSE:BANKNIFTY")
    snap = await engine.compute_once()
    n = snap.news
    assert n is not None and n.source is Source.SYNTHETIC and not n.stale and "news" not in snap.stale_sections
    assert n.value.n_used > 0 and -1 < n.value.score < 0  # gap-down morning: bearish pre-open story dominates
    assert n.value.top[0].title.startswith("Global markets slide")
    now = await market.now()
    assert all(c.published_at <= now for c in n.value.top)

    # an hour of replay time with no news poll -> the news section goes stale, honestly
    await market.set_clock(await market.now() + timedelta(minutes=16), "replay")
    snap2 = await engine.compute_once()
    assert snap2.news.stale and "news" in snap2.stale_sections


async def test_snapshot_reports_missing_news_poll(synth_dir):
    market = MarketStore(MemoryKV())
    p = ReplayProvider(synth_dir, SimClock(ist(date(2026, 9, 23), 10, 0)))
    await Ingestor(p, market, "NSE:BANKNIFTY").run(max_ticks=5)
    snap = await Engine(market, "NSE:BANKNIFTY").compute_once()
    assert snap.news is None and any(e.startswith("news: no news poll") for e in snap.errors)
