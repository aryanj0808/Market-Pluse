"""Phase 8 hardening: heartbeats, database outages, feed backoff, gateway health and rate limits."""

import time
from datetime import date, datetime

import httpx
import pytest

from mp_core import health
from mp_core import market_calendar as cal
from mp_core.clock import SimClock
from mp_core.models import Source
from mp_core.news.feeds import FeedError
from mp_core.news.pipeline import NewsIngestor
from mp_core.news.store import NewsStore
from mp_core.pipeline import Engine, Ingestor
from mp_core.providers.replay import ReplayProvider
from mp_core.store import MarketStore, MemoryKV
from mp_core.store.history import History, HistoryUnavailable
from mp_core.synthetic.generator import generate
from mp_services.gateway import create_app

SYM = "NSE:BANKNIFTY"


@pytest.fixture(scope="module")
def dataset(tmp_path_factory):
    out = tmp_path_factory.mktemp("hard") / "BANKNIFTY"
    generate(SYM, out, start=date(2026, 9, 21), days=2, seed=4)
    return out


def ist(d, hh, mm):
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=cal.IST)


class TestHeartbeats:
    async def test_fresh_stale_missing(self, monkeypatch):
        kv = MemoryKV()
        await health.beat(kv, "engine", {"version": 7})
        st = await health.read(kv, ("engine", "alerts"))
        assert st["engine"]["ok"] and st["engine"]["version"] == 7
        assert st["alerts"] == {"ok": False, "age_s": None, "status": "no heartbeat"}
        real = time.time
        monkeypatch.setattr(time, "time", lambda: real() + 45)  # 45 s without a beat
        ok, msg = await health.check(kv, "engine")
        assert not ok and "stale" in msg
        ok, _ = await health.check(kv, "evaluator")  # never beat
        assert not ok

    async def test_context_manager_beats_then_stops(self):
        import asyncio

        kv = MemoryKV()
        async with health.heartbeat(kv, "engine", lambda: {"n": 1}, every=0.01):
            await asyncio.sleep(0.03)
        assert (await health.read(kv, ("engine",)))["engine"]["n"] == 1


class TestDatabaseOutage:
    async def test_unreachable_database_fails_fast_with_backoff(self):
        h = History("postgresql://u:p@127.0.0.1:1/x")
        with pytest.raises(HistoryUnavailable, match="cannot connect"):
            await h.record_ticks([object()], Source.SYNTHETIC)
        t = time.perf_counter()
        with pytest.raises(HistoryUnavailable, match="next reconnect"):
            await h.record_ticks([object()], Source.SYNTHETIC)
        assert time.perf_counter() - t < 0.05  # no connect attempt during backoff
        assert not h.healthy

    async def test_ingest_survives_a_dead_database_and_catches_up(self, dataset):
        class FlakyHistory:
            def __init__(self):
                self.down = True
                self.candles = []

            async def record_ticks(self, ticks, source):
                if self.down:
                    raise HistoryUnavailable("down")
                return len(ticks)

            async def record_chain(self, chain, source, ivs=None):
                if self.down:
                    raise HistoryUnavailable("down")
                return 1

            async def record_candles(self, candles, source):
                if self.down:
                    raise HistoryUnavailable("down")
                self.candles += candles
                return len(candles)

        hist = FlakyHistory()
        p = ReplayProvider(dataset, SimClock(ist(date(2026, 9, 22), 9, 15)))
        store = MarketStore(MemoryKV())
        ing = Ingestor(p, store, SYM, history=hist, tick_flush=10)
        await ing.run(max_ticks=60)  # ~7 replay minutes with the database down
        assert ing.stats["ticks"] == 60 and ing.stats["record_errors"] > 0
        assert await store.get_quote(SYM, p.index_key) is not None  # live data kept flowing
        hist.down = False
        await ing.run(max_ticks=ing.stats["ticks"] + 20)
        idx = sorted(c.ts for c in hist.candles if c.key.id == SYM)
        assert idx and idx[0] == ist(date(2026, 9, 22), 9, 15)  # the outage's bars were written later

    async def test_engine_publishes_even_if_signal_logging_fails(self, dataset):
        class BrokenLog:
            async def record_signal(self, signal, inputs):
                raise HistoryUnavailable("down")

        p = ReplayProvider(dataset, SimClock(ist(date(2026, 9, 22), 10, 0)))
        store = MarketStore(MemoryKV())
        await Ingestor(p, store, SYM).run(max_ticks=10)
        engine = Engine(store, SYM, history=BrokenLog())
        snap = await engine.compute_once()
        assert snap is not None and engine.log_errors == 1
        assert (await store.get_snapshot(SYM)).version == snap.version


class TestFeedBackoff:
    async def test_failing_feed_backs_off_exponentially(self):
        calls = []

        class Dead:
            name, url, verified = "dead", "https://x", False

            async def fetch(self, now):
                calls.append(now)
                raise FeedError("dead: HTTP 403")

        market = MarketStore(MemoryKV())
        await market.set_clock(datetime(2026, 9, 22, 5, 0, tzinfo=cal.IST), "replay")
        ing = NewsIngestor(
            [Dead()], NewsStore(market.kv), None, source=Source.NEWS_FEED, market=market, base_backoff_s=60
        )
        clock = [1000.0]
        ing._mono = lambda: clock[0]
        await ing.poll_once()
        await ing.poll_once()  # inside the 60 s backoff: skipped
        assert len(calls) == 1 and ing.stats["feeds_backing_off"] == 1
        clock[0] += 61
        await ing.poll_once()  # retried, fails again -> 120 s
        clock[0] += 61
        await ing.poll_once()
        assert len(calls) == 2
        h = (await NewsStore(market.kv).get_health())[0]
        assert "failure 2" in h.last_error and "retry in 120s" in h.last_error


class TestGateway:
    @pytest.fixture
    async def store(self, dataset):
        p = ReplayProvider(dataset, SimClock(ist(date(2026, 9, 22), 10, 0)))
        store = MarketStore(MemoryKV())
        await Ingestor(p, store, SYM).run(max_ticks=10)
        await Engine(store, SYM).compute_once()
        await health.beat(store.kv, "engine")
        return store

    async def test_health_reports_services(self, store):
        app = create_app(store)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            h = (await c.get("/api/health")).json()
        assert h["ok"] and h["services"]["engine"]["ok"]
        assert h["services"]["alerts"]["status"] == "no heartbeat" and h["all_ok"] is False

    async def test_rest_rate_limit(self, store):
        app = create_app(store, rest_rate=1.0, rest_burst=5)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            codes = [(await c.get("/api/snapshot/NSE:BANKNIFTY")).status_code for _ in range(7)]
            assert codes == [200] * 5 + [429, 429]
            r = await c.get("/api/snapshot/NSE:BANKNIFTY")
            assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
            assert (await c.get("/api/health")).status_code == 200  # health is exempt
