"""news-mcp over the MCP protocol (in-memory transport)."""

import json
from datetime import date, datetime

import pytest
from mcp.client.client import Client

from mp_core import market_calendar as cal
from mp_core.clock import SimClock
from mp_core.models import Source
from mp_core.news.classifier import FixtureLabelClassifier
from mp_core.news.feeds import FixtureSource
from mp_core.news.pipeline import NewsIngestor
from mp_core.news.store import NewsStore
from mp_core.pipeline import Engine, Ingestor
from mp_core.providers.replay import ReplayProvider
from mp_core.store import MarketStore, MemoryKV
from mp_core.synthetic.generator import generate
from mp_core.synthetic.news import generate_news, load_labels
from mp_mcp import news
from mp_mcp.common import Deps

SYM = "NSE:BANKNIFTY"


@pytest.fixture(scope="module")
async def deps(tmp_path_factory) -> Deps:
    out = tmp_path_factory.mktemp("newsmcp") / "BANKNIFTY"
    generate(SYM, out, start=date(2026, 9, 21), days=3, seed=11)
    news_path, labels_path = generate_news(out, seed=3)
    kv = MemoryKV()
    market = MarketStore(kv)
    p = ReplayProvider(out, SimClock(datetime(2026, 9, 22, 9, 15, tzinfo=cal.IST)))
    await Ingestor(p, market, SYM).run(until=datetime(2026, 9, 22, 11, 0, tzinfo=cal.IST))
    await NewsIngestor(
        [FixtureSource(news_path, "synthetic")],
        NewsStore(kv),
        FixtureLabelClassifier(load_labels(labels_path)),
        source=Source.SYNTHETIC,
        market=market,
    ).poll_once()
    await Engine(market, SYM).compute_once()
    return Deps(store=market)


async def call(deps, name, args=None):
    async with Client(news.create_server(deps)) as c:
        r = await c.call_tool(name, args or {})
    return (r.is_error if hasattr(r, "is_error") else r.isError), r.content[0].text


async def test_tools_listed(deps):
    async with Client(news.create_server(deps)) as c:
        assert {t.name for t in (await c.list_tools()).tools} == {"get_news", "get_news_score"}


async def test_get_news(deps):
    err, text = await call(deps, "get_news", {"hours": 48})
    assert not err and "\n" not in text
    d = json.loads(text)
    assert d["source"] == "synthetic" and not d["stale"]
    rows = d["data"]["items"]
    assert rows and all(r["relevance"] > 0 for r in rows)
    assert [r["published_at"] for r in rows] == sorted((r["published_at"] for r in rows), reverse=True)
    assert all(r["published_at"] <= d["now"] for r in rows)  # replay clock: no future news
    assert all(r["model"] == "fixture-labels" for r in rows)  # honest about who labelled it
    assert d["data"]["feeds"][0]["feed"] == "synthetic"


async def test_min_impact_filters(deps):
    _, text = await call(deps, "get_news", {"hours": 48, "min_impact": "high"})
    rows = json.loads(text)["data"]["items"]
    assert rows and {r["impact"] for r in rows} == {"high"}


async def test_get_news_score_matches_snapshot(deps):
    err, text = await call(deps, "get_news_score")
    assert not err
    d = json.loads(text)
    snap = await deps.store.get_snapshot(SYM)
    assert d["data"]["score"] == pytest.approx(snap.news.value.score, abs=1e-4)
    assert d["data"]["label"] in ("bearish", "neutral", "bullish") and d["source"] == "synthetic"


@pytest.mark.parametrize("args, msg", [({"hours": 0}, "hours must be"), ({"min_impact": "huge"}, "min_impact")])
async def test_bad_args(deps, args, msg):
    err, text = await call(deps, "get_news", args)
    assert err and msg in text


async def test_no_poll_yet():
    err, text = await call(Deps(store=MarketStore(MemoryKV())), "get_news_score")
    assert err and "no news poll" in text
