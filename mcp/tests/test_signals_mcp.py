"""signals-mcp over the MCP protocol, backed by an in-memory store with a full engine snapshot."""

import json
import statistics
import time
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
from mp_mcp import signals
from mp_mcp.common import Deps

SYM = "NSE:BANKNIFTY"


@pytest.fixture(scope="module")
async def deps(tmp_path_factory) -> Deps:
    out = tmp_path_factory.mktemp("sigmcp") / "BANKNIFTY"
    generate(SYM, out, start=date(2026, 9, 21), days=3, seed=11)
    news_path, labels_path = generate_news(out, seed=3)
    kv = MemoryKV()
    market = MarketStore(kv)
    p = ReplayProvider(out, SimClock(datetime(2026, 9, 22, 9, 15, tzinfo=cal.IST)))
    await Ingestor(p, market, SYM).run(until=datetime(2026, 9, 22, 11, 30, tzinfo=cal.IST))
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
    async with Client(signals.create_server(deps)) as c:
        r = await c.call_tool(name, args or {})
    return (r.is_error if hasattr(r, "is_error") else r.isError), r.content[0].text


async def test_tools(deps):
    async with Client(signals.create_server(deps)) as c:
        names = {t.name for t in (await c.list_tools()).tools}
    assert names == {
        "get_market_snapshot",
        "get_signal",
        "get_expected_range",
        "get_levels",
        "backtest",
        "get_signal_stats",
    }


async def test_market_snapshot_is_complete_and_fast(deps):
    err, text = await call(deps, "get_market_snapshot")
    assert not err and "\n" not in text
    d = json.loads(text)
    assert d["source"] == "synthetic"
    assert {
        "quote",
        "future",
        "expected_range",
        "expiries",
        "levels",
        "news",
        "signal",
        "top_movers",
        "macro",
        "stale_sections",
        "errors",
    } <= set(d["data"])
    sig = d["data"]["signal"]
    assert sig["status"] in ("active", "no_signal") and sig["rules_version"] == "fusion-v1"
    async with Client(signals.create_server(deps)) as c:
        times = []
        for _ in range(20):
            t = time.perf_counter()
            await c.call_tool("get_market_snapshot", {})
            times.append((time.perf_counter() - t) * 1000)
    assert statistics.median(times) < 100, times


async def test_signal_reasons_cite_inputs(deps):
    _, text = await call(deps, "get_signal")
    sig = json.loads(text)["data"]
    assert sig["reasons"] and all(r["input"] for r in sig["reasons"])
    assert {c["name"] for c in sig["components"]} == {"technical", "options", "news"}
    if sig["status"] == "no_signal":
        assert sig["no_signal_reasons"]


async def test_expected_range_picks_expiry_by_days(deps):
    _, short = await call(deps, "get_expected_range", {"days": 1})
    _, long = await call(deps, "get_expected_range", {"days": 20})
    s, lg = json.loads(short)["data"], json.loads(long)["data"]
    assert s["iv_expiry"] == "2026-09-29" and lg["iv_expiry"] == "2026-10-27"
    assert lg["sigma_points"] > s["sigma_points"]
    assert s["one_sigma"]["upper"] - s["spot"] == pytest.approx(s["sigma_points"], rel=1e-3)


async def test_levels_role_follows_position(deps):
    _, text = await call(deps, "get_levels")
    d = json.loads(text)["data"]
    for lv in d["levels"]:
        assert lv["role_now"] == (
            "resistance" if lv["price"] > d["spot"] else "support" if lv["price"] < d["spot"] else "at price"
        )
    dists = [abs(lv["distance_pts"]) for lv in d["levels"]]
    assert dists == sorted(dists)


@pytest.mark.parametrize(
    "name, args, msg",
    [
        ("backtest", {"strategy": "momentum", "start": "2025-01-01", "end": "2025-06-30"}, "strategy must be"),
        ("backtest", {"strategy": "expected_range", "start": "2025-01-01", "end": "2025-06-30"}, "DATABASE_URL"),
        ("get_signal_stats", {}, "DATABASE_URL"),
        ("get_expected_range", {"days": 0}, "days must be"),
    ],
)
async def test_errors(deps, name, args, msg):
    err, text = await call(deps, name, args)
    assert err and msg in text
