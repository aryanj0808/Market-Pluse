"""MCP servers over the real protocol (in-memory transport), backed by an in-memory store."""

import json
import statistics
import time
from datetime import date, datetime, timedelta

import pytest
from mcp.client.client import Client

from mp_core import market_calendar as cal
from mp_core.clock import SimClock
from mp_core.pipeline import Engine, Ingestor
from mp_core.providers.replay import ReplayProvider
from mp_core.store import MarketStore, MemoryKV
from mp_core.synthetic.generator import generate
from mp_mcp import market_data, options
from mp_mcp.common import Deps

SYM = "NSE:BANKNIFTY"


@pytest.fixture(scope="module")
def dataset(tmp_path_factory):
    out = tmp_path_factory.mktemp("mcp") / "BANKNIFTY"
    generate(SYM, out, start=date(2026, 9, 21), days=3, seed=11)
    return out


@pytest.fixture(scope="module")
async def deps(dataset) -> Deps:
    p = ReplayProvider(dataset, SimClock(datetime(2026, 9, 22, 9, 15, tzinfo=cal.IST)))
    store = MarketStore(MemoryKV())
    await Ingestor(p, store, SYM).run(until=datetime(2026, 9, 22, 11, 0, tzinfo=cal.IST))
    await Engine(store, SYM).compute_once()
    return Deps(store=store)


async def call(server, name, args=None):
    async with Client(server) as c:
        r = await c.call_tool(name, args or {})
        text = r.content[0].text
        return (r.is_error if hasattr(r, "is_error") else r.isError), text


@pytest.mark.parametrize(
    "factory, tools",
    [
        (market_data.create_server, {"get_quote", "get_candles", "get_constituents", "get_macro"}),
        (options.create_server, {"get_option_chain", "get_iv_summary", "get_oi_buildup", "get_pcr_maxpain"}),
    ],
)
async def test_tool_lists(deps, factory, tools):
    async with Client(factory(deps)) as c:
        listed = {t.name: t for t in (await c.list_tools()).tools}
    assert set(listed) == tools
    assert all(t.description for t in listed.values())


@pytest.mark.parametrize(
    "factory, name, args",
    [
        (market_data.create_server, "get_quote", {}),
        (market_data.create_server, "get_constituents", {}),
        (market_data.create_server, "get_macro", {}),
        (options.create_server, "get_option_chain", {"strikes_each_side": 3}),
        (options.create_server, "get_iv_summary", {}),
        (options.create_server, "get_oi_buildup", {}),
        (options.create_server, "get_pcr_maxpain", {}),
    ],
)
async def test_envelope_compact_and_synthetic(deps, factory, name, args):
    err, text = await call(factory(deps), name, args)
    assert not err, text
    assert "\n" not in text and ": " not in text  # compact JSON
    d = json.loads(text)
    assert {"symbol", "source", "as_of", "stale", "now", "data"} <= set(d)
    assert d["source"] == "synthetic"
    assert datetime.fromisoformat(d["as_of"]) <= datetime.fromisoformat(d["now"])


async def test_quote_numbers_come_from_snapshot(deps):
    _, text = await call(market_data.create_server(deps), "get_quote")
    snap = await deps.store.get_snapshot(SYM)
    d = json.loads(text)["data"]
    assert d["index"]["ltp"] == round(snap.quote.value.ltp, 2)
    assert d["future"]["basis_points"] == pytest.approx(snap.future.value.basis_points, abs=0.01)


async def test_chain_is_centred_and_has_iv(deps):
    _, text = await call(options.create_server(deps), "get_option_chain", {"strikes_each_side": 4})
    d = json.loads(text)["data"]
    strikes = [r["strike"] for r in d["rows"]]
    assert len(strikes) == 9 and strikes == sorted(strikes)
    assert min(strikes) < d["spot"] < max(strikes)
    assert all(r["ce"]["iv"] and r["pe"]["iv"] for r in d["rows"])


async def test_constituent_contributions_sum_to_index_move(deps, dataset):
    _, text = await call(market_data.create_server(deps), "get_constituents")
    d = json.loads(text)
    # constituents are polled once a minute: compare with the index at *their* as_of
    p = ReplayProvider(dataset, SimClock(datetime.fromisoformat(d["as_of"])))
    index_then = (await p.get_quote(p.index_key)).value.change_pct
    total = sum(r["contribution_pct_pts"] for r in d["data"]["constituents"])
    assert total == pytest.approx(index_then, abs=0.01)


@pytest.mark.parametrize(
    "factory, name, args, msg",
    [
        (market_data.create_server, "get_quote", {"symbol": "NSE:MIDCPNIFTY"}, "unknown index"),
        (market_data.create_server, "get_candles", {"interval": "7m"}, "interval must be"),
        (market_data.create_server, "get_candles", {}, "DATABASE_URL"),
        (options.create_server, "get_option_chain", {"expiry": "2030-01-01"}, "not available"),
        (options.create_server, "get_pcr_maxpain", {"expiry": "29-09-2026"}, "YYYY-MM-DD"),
    ],
)
async def test_errors_are_explicit(deps, factory, name, args, msg):
    err, text = await call(factory(deps), name, args)
    assert err and msg in text


async def test_bare_symbol_accepted(deps):
    err, text = await call(market_data.create_server(deps), "get_quote", {"symbol": "BANKNIFTY"})
    assert not err and json.loads(text)["symbol"] == SYM


async def test_stale_flag_follows_shared_clock(deps):
    store = deps.store
    real_now = await store.now()
    await store.set_clock(real_now + timedelta(minutes=20), "replay")
    try:
        _, text = await call(market_data.create_server(deps), "get_quote")
        assert json.loads(text)["stale"] is True
    finally:
        await store.kv.set("mp:clock", json.dumps({"mode": "replay", "now": real_now.isoformat()}))


async def test_latency_under_100ms(deps):
    server = options.create_server(deps)
    async with Client(server) as c:
        for name in ("get_iv_summary", "get_option_chain", "get_oi_buildup"):
            times = []
            for _ in range(15):
                t = time.perf_counter()
                await c.call_tool(name, {})
                times.append((time.perf_counter() - t) * 1000)
            assert statistics.median(times) < 100, (name, times)
