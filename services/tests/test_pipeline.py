"""Ingest -> store -> engine, in memory (no Redis needed)."""

import asyncio
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from mp_core import market_calendar as cal
from mp_core.clock import SimClock
from mp_core.models import InstrumentKey, Source
from mp_core.pipeline import Engine, Ingestor
from mp_core.providers.replay import ReplayProvider
from mp_core.store import MarketStore, MemoryKV
from mp_core.synthetic.generator import generate

SYM = "NSE:BANKNIFTY"
BN = InstrumentKey.parse(SYM)


def ist(d, hh, mm):
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=cal.IST)


@pytest.fixture(scope="module")
def dataset(tmp_path_factory) -> Path:
    # spans the 29-Sep expiry so the futures roll and expiry drop can be tested
    out = tmp_path_factory.mktemp("pipe") / "BANKNIFTY"
    generate(SYM, out, start=date(2026, 9, 25), days=4, seed=5)
    return out


async def test_ingest_populates_store(dataset):
    p = ReplayProvider(dataset, SimClock(ist(date(2026, 9, 28), 9, 15)))
    store = MarketStore(MemoryKV())
    stats = await Ingestor(p, store, SYM).run(max_ticks=200)
    assert stats["ticks"] == 200 and stats["chains"] >= 2
    now = await store.now()
    assert now == p.clock.now() and await store.clock_mode() == "replay"
    q = await store.get_quote(SYM, BN)
    assert q.source is Source.SYNTHETIC and q.as_of <= now
    exps = await store.get_expiries(SYM)
    assert exps == [date(2026, 9, 29), date(2026, 10, 27)]
    assert (await store.get_chain(SYM, exps[0], prev=True)) is not None  # previous snapshot kept
    fut = await store.get_future_key(SYM)
    assert fut.id == "NSE:BANKNIFTY:2026-09-29"
    assert (await store.get_candles(SYM, "candles1m", fut)).value
    assert (await store.get_candles(SYM, "daily", BN)).value
    assert await store.get_constituents(SYM) is not None and await store.get_macro() is not None


async def test_put_chain_ignores_older_or_same(dataset):
    p = ReplayProvider(dataset, SimClock(ist(date(2026, 9, 28), 11, 0)))
    store = MarketStore(MemoryKV())
    ch = await p.get_option_chain(BN, date(2026, 9, 29))
    assert await store.put_chain(SYM, ch) and not await store.put_chain(SYM, ch)


async def test_engine_reacts_to_ingest_events(dataset):
    p = ReplayProvider(dataset, SimClock(ist(date(2026, 9, 28), 10, 0)))
    store = MarketStore(MemoryKV())
    engine = Engine(store, SYM, debounce_s=0, min_interval=0)
    stop = asyncio.Event()
    task = asyncio.create_task(engine.run(stop))
    await asyncio.sleep(0)
    ing = Ingestor(p, store, SYM)
    for _ in range(40):
        await ing.run(max_ticks=ing.stats["ticks"] + 1)
        await asyncio.sleep(0.001)  # let the engine see the event
    stop.set()
    await asyncio.wait_for(task, 2)
    snap = await store.get_snapshot(SYM)
    assert snap is not None and snap.version >= 10 and snap.source is Source.SYNTHETIC
    assert snap.computed_at == await store.now()


async def test_engine_versions_survive_restart(dataset):
    p = ReplayProvider(dataset, SimClock(ist(date(2026, 9, 28), 10, 0)))
    store = MarketStore(MemoryKV())
    await Ingestor(p, store, SYM).run(max_ticks=20)
    e1 = Engine(store, SYM)
    for _ in range(3):
        await e1.compute_once()
    assert (await Engine(store, SYM).compute_once()).version == 4


async def test_no_snapshot_before_first_quote():
    assert await Engine(MarketStore(MemoryKV()), SYM).compute_once() is None


async def test_futures_roll_and_expiry_drop_across_expiry(dataset):
    p = ReplayProvider(dataset, SimClock(ist(date(2026, 9, 29), 15, 20)))
    store = MarketStore(MemoryKV())
    ing = Ingestor(p, store, SYM)
    await ing.run(until=ist(date(2026, 9, 30), 9, 30))
    assert (await store.get_future_key(SYM)).id == "NSE:BANKNIFTY:2026-10-27"
    assert date(2026, 9, 29) not in await store.get_expiries(SYM)
    snap = await Engine(store, SYM).compute_once()
    assert [e.value.expiry for e in snap.expiries][0] == date(2026, 10, 27)
    assert snap.future.value.key.expiry == date(2026, 10, 27)
    assert snap.future.value.buildup_15m == [] or all(
        o.start.astimezone(cal.IST).date() == date(2026, 9, 30) for o in snap.future.value.buildup_15m
    )


async def test_replay_end_leaves_honest_staleness(dataset):
    p = ReplayProvider(dataset, SimClock(ist(date(2026, 9, 30), 15, 25)))
    store = MarketStore(MemoryKV())
    await Ingestor(p, store, SYM).run()  # runs to the end of the dataset
    await store.set_clock(await store.now() + timedelta(hours=1), "replay")  # an hour passes, no data
    snap = await Engine(store, SYM).compute_once()
    assert "quote" in snap.stale_sections


async def test_replay_restart_from_earlier_time_cannot_see_the_future(dataset):
    store = MarketStore(MemoryKV())
    late = ReplayProvider(dataset, SimClock(ist(date(2026, 9, 28), 14, 0)))
    await Ingestor(late, store, SYM).run(max_ticks=50)
    early = ReplayProvider(dataset, SimClock(ist(date(2026, 9, 28), 10, 0)))
    await Ingestor(early, store, SYM).run(max_ticks=50)
    now = await store.now()
    assert now < ist(date(2026, 9, 28), 10, 30)
    snap = await Engine(store, SYM).compute_once()
    assert snap.quote.as_of <= now
    assert all(e.value.iv.ts <= now for e in snap.expiries)
    assert all(
        c.ts < now for c in snap.future and (await store.get_candles(SYM, "candles1m", snap.future.value.key)).value
    )


async def test_engine_throttles_to_4_per_second_and_keeps_tick_time(dataset):
    import json
    import time

    p = ReplayProvider(dataset, SimClock(ist(date(2026, 9, 28), 10, 0)))
    store = MarketStore(MemoryKV())
    ing = Ingestor(p, store, SYM)
    await ing.run(max_ticks=4)
    updates = []

    async def watch():
        async for m in store.updates(SYM):
            updates.append((time.monotonic(), json.loads(m)))

    watcher = asyncio.create_task(watch())
    engine = Engine(store, SYM)  # defaults: 10 ms coalescing, at most one snapshot per 250 ms
    stop = asyncio.Event()
    task = asyncio.create_task(engine.run(stop))
    await asyncio.sleep(0.4)  # initial snapshot, engine idle
    t_quiet = time.monotonic()
    await ing.run(max_ticks=ing.stats["ticks"] + 1)  # a single tick on a quiet engine
    await asyncio.sleep(0.1)
    reacted = [t for t, _ in updates if t >= t_quiet]
    assert reacted and reacted[0] - t_quiet < 0.08  # leading edge: no 250 ms wait when quiet
    assert updates[-1][1]["tick_wall"] is not None  # tick arrival time travels with the update
    n0, t0 = len(updates), time.monotonic()
    for _ in range(40):  # a burst of ticks
        await ing.run(max_ticks=ing.stats["ticks"] + 1)
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.4)
    stop.set()
    await asyncio.wait_for(task, 2)
    watcher.cancel()
    burst = [t for t, _ in updates[n0:]]
    span = max(burst[-1] - t0, 0.25)
    assert len(burst) <= span / 0.25 + 1.5  # <= 4/s
    assert all(b - a >= 0.24 for a, b in zip(burst, burst[1:], strict=False))
    snap = await store.get_snapshot(SYM)
    assert snap.quote.as_of == (await store.get_quote(SYM, snap.quote.value.key)).as_of  # newest state


async def test_alert_service_end_to_end_dry_run(dataset, caplog):
    import logging

    from mp_core.alerts import AlertDispatcher, TelegramSender
    from mp_services.alerts import AlertService

    p = ReplayProvider(dataset, SimClock(ist(date(2026, 9, 28), 10, 0)))
    store = MarketStore(MemoryKV())
    ing = Ingestor(p, store, SYM)
    engine = Engine(store, SYM)
    sender = TelegramSender(None, None)
    svc = AlertService(store, SYM, AlertDispatcher(sender, allow_synthetic=True))
    with caplog.at_level(logging.INFO):
        for _ in range(240):  # an hour of replay, one engine update per minute
            await ing.run(max_ticks=ing.stats["ticks"] + 4)
            await engine.compute_once()
            await svc.on_update()
    assert sender.dry_run and sender.stats["sent"] == 0  # nothing leaves the machine without a token
    assert svc.sent == sender.stats["dry_run"] >= 1, "the replayed hour should trigger at least one alert"
    assert all(
        line.startswith("[dry run] would send: [SYNTHETIC]")
        for line in (r.getMessage() for r in caplog.records if r.getMessage().startswith("[dry run]"))
    )
