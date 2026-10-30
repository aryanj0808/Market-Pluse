"""news_items in Timescale: final statuses are never overwritten (no paying for a classification twice)."""

from datetime import UTC, datetime

import psycopg
import pytest

from mp_core.models import Source
from mp_core.news.dedupe import title_tokens
from mp_core.news.models import NewsClassification, RawNewsItem, ScoredNewsItem
from mp_core.store.history import History

from test_infra import _env

pytestmark = pytest.mark.integration
T = datetime(2026, 3, 4, 5, 0, tzinfo=UTC)


def item(status, sentiment=None, url="https://news.synthetic.invalid/itest/1"):
    raw = RawNewsItem(
        feed="itest", url=url, title="Integration test story", published_at=T, fetched_at=T, source=Source.SYNTHETIC
    )
    cls = (
        None
        if sentiment is None
        else NewsClassification(
            relevant=True, entities=["SECTOR_BANKS"], sentiment=sentiment, impact="med", horizon="days", reason="itest"
        )
    )
    return ScoredNewsItem(
        id=raw.id, item=raw, title_tokens=title_tokens(raw.title), status=status, classification=cls, model="itest"
    )


@pytest.fixture
async def hist():
    env = _env()
    h = await History(env["DATABASE_URL"]).connect()
    yield h
    with psycopg.connect(env["DATABASE_URL"], autocommit=True) as conn:
        conn.execute("DELETE FROM news_items WHERE feed = 'itest'")
    await h.close()


async def test_unclassified_is_upgraded_then_frozen(hist):
    await hist.record_news(item("unclassified"))
    assert (await hist.get_news(item("unclassified").id)).status == "unclassified"
    await hist.record_news(item("classified", 0.4))
    got = await hist.get_news(item("classified").id)
    assert got.status == "classified" and got.classification.sentiment == 0.4
    await hist.record_news(item("classified", -0.9))  # a second classification must not replace the first
    await hist.record_news(item("unclassified"))
    assert (await hist.get_news(got.id)).classification.sentiment == 0.4


async def test_dropped_is_final(hist):
    it = item("dropped", url="https://news.synthetic.invalid/itest/2")
    await hist.record_news(it)
    await hist.record_news(item("classified", 0.5, url="https://news.synthetic.invalid/itest/2"))
    assert (await hist.get_news(it.id)).status == "dropped"
