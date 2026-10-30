"""Ingest -> Redis -> engine -> MCP, plus Timescale recording, against the real infrastructure.

Uses Redis DB 15 (not the stack's DB 0) and deletes the Timescale rows it writes."""

import json
import statistics
import time
from datetime import date, datetime, timedelta

import psycopg
import pytest
from mcp.client.client import Client

from mp_core import market_calendar as cal
from mp_core.clock import SimClock
from mp_core.pipeline import Engine, Ingestor
from mp_core.providers.replay import ReplayProvider
from mp_core.store import MarketStore, RedisKV
from mp_core.store.history import History
from mp_core.synthetic.generator import generate
from mp_mcp import market_data, options
from mp_mcp.common import Deps

from test_infra import _env

pytestmark = pytest.mark.integration
SYM = "NSE:BANKNIFTY"
DAY = date(2026, 3, 3)  # outside the default dataset's window, so cleanup cannot touch other data
START = datetime(DAY.year, DAY.month, DAY.day, 9, 15, tzinfo=cal.IST)
END = START + timedelta(minutes=45)


@pytest.fixture(scope="module")
async def stack(tmp_path_factory):
    env = _env()
    out = tmp_path_factory.mktemp("integ") / "BANKNIFTY"
    generate(SYM, out, start=DAY - timedelta(days=1), days=2, seed=21)
    kv = RedisKV(env["REDIS_URL"].rsplit("/", 1)[0] + "/15")
    store = MarketStore(kv)
    hist = await History(env["DATABASE_URL"]).connect()
    p = ReplayProvider(out, SimClock(START))
    stats = await Ingestor(p, store, SYM, history=hist).run(until=END)
    snap = await Engine(store, SYM).compute_once()
    yield {"store": store, "history": hist, "stats": stats, "snap": snap, "env": env, "dir": out}
    with psycopg.connect(env["DATABASE_URL"], autocommit=True) as conn:
        for table in ("ticks", "candles", "option_quotes"):
            conn.execute(
                f"DELETE FROM {table} WHERE source = 'synthetic' AND ts >= %s AND ts < %s",  # noqa: S608
                (START - timedelta(days=2), END + timedelta(days=1)),
            )
    await hist.close()
    await kv.close()


async def test_recorded_to_timescale_with_source(stack):
    s = stack["stats"]
    assert s["ticks"] > 300 and s["recorded_ticks"] == s["ticks"]
    assert s["candles"] > 16 * 40 and s["recorded_quotes"] > 0
    with psycopg.connect(stack["env"]["DATABASE_URL"]) as conn:
        n_quotes, n_iv, sources = conn.execute(
            "SELECT count(*), count(iv), array_agg(DISTINCT source) FROM option_quotes WHERE ts >= %s AND ts <= %s",
            (START - timedelta(days=1), END),  # the first chain seen is the previous session's last
        ).fetchone()
    assert n_quotes == s["recorded_quotes"] and sources == ["synthetic"]
    assert n_iv > 0.3 * n_quotes  # IVs recorded where identifiable


async def test_rerecording_a_chain_is_idempotent(stack):
    store, hist = stack["store"], stack["history"]
    exp = (await store.get_expiries(SYM))[0]
    chain = (await store.get_chain(SYM, exp)).value
    with psycopg.connect(stack["env"]["DATABASE_URL"]) as conn:
        q = "SELECT count(*) FROM option_quotes WHERE ts = %s AND expiry = %s"
        before = conn.execute(q, (chain.ts, exp)).fetchone()[0]
        await hist.record_chain(chain, stack["snap"].source)
        assert conn.execute(q, (chain.ts, exp)).fetchone()[0] == before > 0


async def test_rerecording_ticks_is_idempotent(stack):
    p_ticks = [t async for t in _first_ticks(stack, 5)]
    with psycopg.connect(stack["env"]["DATABASE_URL"]) as conn:
        q = "SELECT count(*) FROM ticks WHERE ts >= %s AND ts <= %s"
        before = conn.execute(q, (START, END)).fetchone()[0]
        await stack["history"].record_ticks(p_ticks, stack["snap"].source)
        assert conn.execute(q, (START, END)).fetchone()[0] == before


async def _first_ticks(stack, n):
    p = ReplayProvider(stack["dir"], SimClock(START))
    i = 0
    async for t in p.stream_ticks([p.index_key]):
        yield t
        i += 1
        if i == n:
            return


async def test_snapshot_in_redis(stack):
    snap = stack["snap"]
    assert snap.source.value == "synthetic" and snap.errors == ["news: no news poll yet (is ingest-news running?)"]
    t = time.perf_counter()
    raw = await stack["store"].get_snapshot_json(SYM)
    assert (time.perf_counter() - t) * 1000 < 100 and json.loads(raw)["version"] == snap.version


async def test_get_candles_reads_recorded_history(stack):
    deps = Deps(store=stack["store"], database_url=stack["env"]["DATABASE_URL"])
    async with Client(market_data.create_server(deps)) as c:
        r = await c.call_tool("get_candles", {"interval": "15m", "days": 1})
    d = json.loads(r.content[0].text)
    assert d["source"] == "synthetic" and d["data"]["columns"][0] == "ts"
    assert len(d["data"]["rows"]) == 3  # 09:15, 09:30, 09:45 - the 10:00 bar has not closed


async def test_mcp_latency_against_redis(stack):
    deps = Deps(store=stack["store"])
    for factory, name in ((options.create_server, "get_option_chain"), (market_data.create_server, "get_quote")):
        async with Client(factory(deps)) as c:
            times = []
            for _ in range(20):
                t = time.perf_counter()
                await c.call_tool(name, {})
                times.append((time.perf_counter() - t) * 1000)
        assert statistics.median(times) < 100, (name, times)
