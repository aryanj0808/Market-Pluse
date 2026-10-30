"""Gateway: REST endpoints, WebSocket stream, diffing and throttling."""

import asyncio
import json
import time
from datetime import date, datetime

import httpx
import pytest
from starlette.testclient import TestClient

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
from mp_core.stream import SnapshotStreamer
from mp_core.synthetic.generator import generate
from mp_core.synthetic.news import generate_news, load_labels
from mp_services.gateway import create_app

SYM = "NSE:BANKNIFTY"


@pytest.fixture(scope="module")
def dataset(tmp_path_factory):
    out = tmp_path_factory.mktemp("gw") / "BANKNIFTY"
    generate(SYM, out, start=date(2026, 9, 21), days=3, seed=11)
    generate_news(out, seed=3)
    return out


async def live(dataset, until=(11, 0)):
    """A store with ingest + news + one engine snapshot, and the objects to keep it moving."""
    kv = MemoryKV()
    market = MarketStore(kv)
    p = ReplayProvider(dataset, SimClock(datetime(2026, 9, 22, 9, 15, tzinfo=cal.IST)))
    ing = Ingestor(p, market, SYM)
    await ing.run(until=datetime(2026, 9, 22, *until, tzinfo=cal.IST))
    ni = NewsIngestor(
        [FixtureSource(dataset / "news.jsonl", "synthetic")],
        NewsStore(kv),
        FixtureLabelClassifier(load_labels(dataset / "news_labels.jsonl")),
        source=Source.SYNTHETIC,
        market=market,
        replay=True,
    )
    await ni.poll_once()
    engine = Engine(market, SYM)
    await engine.compute_once()
    return market, ing, engine, ni


class Collector:
    def __init__(self):
        self.msgs: list[tuple[float, dict]] = []

    async def __call__(self, text: str) -> None:
        self.msgs.append((time.perf_counter(), json.loads(text)))

    def types(self):
        return [m["type"] for _, m in self.msgs]


def test_sliding_window_arithmetic():
    s = SnapshotStreamer(MarketStore(MemoryKV()), SYM, None)  # type: ignore[arg-type]
    assert s.wait_needed(10.0) == 0
    s._sends.extend([10.0, 10.3, 10.6, 10.9])  # four sends inside one second
    assert s.wait_needed(10.95) == pytest.approx(0.05)  # 5th allowed once 10.0 leaves the window
    assert s.wait_needed(11.0) == pytest.approx(0, abs=1e-9)
    s._sends.clear()
    s._sends.append(20.0)
    assert s.wait_needed(20.04) == pytest.approx(0.06)  # min gap


class TestStreamer:
    async def test_snapshot_then_section_diffs(self, dataset):
        market, ing, engine, _ = await live(dataset)
        out = Collector()
        s = SnapshotStreamer(market, SYM, out)
        await s.push()
        assert out.types()[0] == "snapshot" and "chain" in out.types()
        first = out.msgs[0][1]["snapshot"]
        await ing.run(max_ticks=ing.stats["ticks"] + 1)  # one more 15s tick
        await engine.compute_once()
        await s.push()
        diff = next(m for _, m in reversed(out.msgs) if m["type"] == "diff")  # a chain update may follow it
        assert diff["version"] == first["version"] + 1
        assert "quote" in diff["changed"] and "version" in diff["changed"]
        assert "constituents" not in diff["changed"] or diff["changed"]["constituents"] != first["constituents"]
        unchanged = {k for k in first if k not in diff["changed"]}
        assert unchanged, "a diff must not resend unchanged sections"

    async def test_no_message_without_a_new_version(self, dataset):
        market, *_ = await live(dataset)
        out = Collector()
        s = SnapshotStreamer(market, SYM, out)
        await s.push()
        n = len(out.msgs)
        await s.push()
        assert len(out.msgs) == n

    async def test_news_change_is_announced(self, dataset):
        market, ing, engine, ni = await live(dataset)
        out = Collector()
        s = SnapshotStreamer(market, SYM, out)
        await s.push()
        await ing.run(until=datetime(2026, 9, 22, 14, 0, tzinfo=cal.IST))
        await ni.poll_once()
        await engine.compute_once()
        await s.push()
        assert "news" in out.types()[1:]

    async def test_throttle_coalesces_bursts_and_sends_the_newest(self, dataset):
        market, ing, engine, _ = await live(dataset)
        out = Collector()
        stop = asyncio.Event()
        task = asyncio.create_task(SnapshotStreamer(market, SYM, out).run(stop))
        await asyncio.sleep(0.05)
        for _ in range(20):  # 20 engine updates in a burst
            await ing.run(max_ticks=ing.stats["ticks"] + 1)
            await engine.compute_once()
        last_version = engine.version
        await asyncio.sleep(0.6)
        stop.set()
        await asyncio.wait_for(task, 2)
        diffs = [(t, m) for t, m in out.msgs if m["type"] == "diff"]
        assert 1 <= len(diffs) <= 5  # not 20
        assert diffs[-1][1]["version"] == last_version  # newest state wins
        sends = [t for t, m in out.msgs if m["type"] in ("snapshot", "diff")]
        assert all(sum(1 for u in sends if t <= u < t + 1.0) <= 4 for t in sends)  # <= 4 in any 1 s window
        assert all(b - a >= 0.095 for a, b in zip(sends, sends[1:], strict=False))  # min gap

    async def test_isolated_update_goes_out_immediately(self, dataset):
        market, ing, engine, _ = await live(dataset)
        out = Collector()
        stop = asyncio.Event()
        task = asyncio.create_task(SnapshotStreamer(market, SYM, out).run(stop))
        await asyncio.sleep(0.4)  # well past the minimum gap
        t0 = time.monotonic()
        await ing.run(max_ticks=ing.stats["ticks"] + 1)
        await engine.compute_once()
        await asyncio.sleep(0.15)
        stop.set()
        await asyncio.wait_for(task, 2)
        diffs = [t for t, m in out.msgs if m["type"] == "diff"]
        assert diffs and diffs[0] - t0 < 0.1
        d = next(m for _, m in out.msgs if m["type"] == "diff")
        assert d["engine_wall"] and d["gateway_wall"] >= d["engine_wall"]


class TestRest:
    @pytest.fixture
    async def client(self, dataset):
        market, *_ = await live(dataset)
        app = create_app(market)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            yield c

    async def test_snapshot(self, client):
        r = await client.get("/api/snapshot/NSE:BANKNIFTY")
        assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
        d = r.json()
        assert d["snapshot"]["symbol"] == SYM and d["now"] and d["snapshot"]["signal"]["rules_version"]
        assert (await client.get("/api/snapshot/BANKNIFTY")).status_code == 200
        assert (await client.get("/api/snapshot/NSE:MIDCPNIFTY")).status_code == 404
        assert (await client.get("/api/snapshot/NSE:NIFTY")).status_code == 404  # no engine for it

    async def test_chain_candles_news(self, client):
        ch = (await client.get("/api/chain/NSE:BANKNIFTY")).json()
        assert ch["source"] == "synthetic" and ch["value"]["rows"]
        assert (await client.get("/api/chain/NSE:BANKNIFTY?expiry=2030-01-01")).status_code == 404
        idx = (await client.get("/api/candles/NSE:BANKNIFTY")).json()
        fut = (await client.get("/api/candles/NSE:BANKNIFTY?which=future")).json()
        assert idx["value"][0]["key"]["underlying"] == "BANKNIFTY" and idx["value"][0]["key"]["expiry"] is None
        assert fut["value"][0]["key"]["expiry"] is not None
        news = (await client.get("/api/news/NSE:BANKNIFTY?hours=48")).json()
        assert news["items"] and all(i["relevance"] > 0 for i in news["items"])
        assert news["items"][0]["published_at"] >= news["items"][-1]["published_at"]
        assert (await client.get("/api/news/NSE:BANKNIFTY?min_impact=huge")).status_code == 422

    async def test_stats_need_a_database(self, client):
        r = await client.get("/api/stats/NSE:BANKNIFTY")
        assert r.status_code == 503 and "DATABASE_URL" in r.json()["detail"]

    async def test_health(self, client):
        h = (await client.get("/api/health")).json()
        assert h["ok"] and h["mode"] == "replay"


def test_websocket_round_trip(dataset):
    market, ing, engine, _ = asyncio.run(live(dataset))
    app = create_app(market, max_per_sec=50, min_gap=0)
    with TestClient(app) as tc, tc.websocket_connect("/api/stream/NSE:BANKNIFTY") as ws:
        first = ws.receive_json()
        assert first["type"] == "snapshot" and first["snapshot"]["symbol"] == SYM

        async def update():
            await ing.run(max_ticks=ing.stats["ticks"] + 1)
            await engine.compute_once()

        asyncio.run(update())  # published from this thread; delivered to the app's loop
        msgs = []
        while not any(m["type"] == "diff" for m in msgs) and len(msgs) < 8:  # chain/news may arrive first
            msgs.append(ws.receive_json())
        diff = next(m for m in msgs if m["type"] == "diff")
        assert diff["version"] == first["version"] + 1 and "quote" in diff["changed"]


def test_websocket_unknown_symbol_is_closed(dataset):
    market, *_ = asyncio.run(live(dataset))
    with (
        TestClient(create_app(market)) as tc,
        pytest.raises(Exception),  # noqa: B017 - the close surfaces differently across starlette versions
        tc.websocket_connect("/api/stream/NSE:FOO") as ws,
    ):
        ws.receive_json()


class FakeAssistant:
    model = "claude-sonnet-5-5"

    def __init__(self, chunks=None, refuse=False):
        self.chunks = chunks
        self.refuse = refuse
        self.questions = []

    async def ask(self, question, digest):
        from mp_core.assistant import AskEvent

        self.questions.append((question, digest))
        if self.refuse:
            yield AskEvent("refusal", info={"category": "general_harms"})
            return
        spot = digest["data"]["quote"]["ltp"]
        for c in self.chunks or [f"Spot is {spot:,.2f} [quote]. ", "Support at 1,23,456."]:
            yield AskEvent("text", c)
        yield AskEvent("done", info={"stop_reason": "end_turn", "model": self.model})


def parse_sse(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        out.append((lines["event"], json.loads(lines["data"])))
    return out


class TestAsk:
    @pytest.fixture
    async def market(self, dataset):
        market, *_ = await live(dataset)
        return market

    async def client(self, market, assistant, **kw):
        app = create_app(market, assistant=assistant, **kw)
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")

    async def test_streams_answer_and_flags_unverified_numbers(self, market):
        fa = FakeAssistant()
        async with await self.client(market, fa) as c:
            r = await c.post("/api/ask/NSE:BANKNIFTY", json={"question": "What is the setup?"})
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        events = parse_sse(r.text)
        assert [e for e, _ in events] == ["meta", "delta", "delta", "done"]
        meta, done = events[0][1], events[-1][1]
        assert meta["source"] == "synthetic" and meta["model"] == "claude-sonnet-5-5"
        assert done["unverified_numbers"] == ["1,23,456"]  # the spot checks out; the invented level does not
        question, digest = fa.questions[0]
        assert question == "What is the setup?" and digest["data"]["signal"]["rules_version"] == "fusion-v1"

    async def test_refusal_becomes_an_error_event(self, market):
        async with await self.client(market, FakeAssistant(refuse=True)) as c:
            r = await c.post("/api/ask/NSE:BANKNIFTY", json={"question": "x"})
        assert parse_sse(r.text)[-1] == (
            "error",
            {"message": "Claude declined to answer this question.", "category": "general_harms"},
        )

    async def test_not_configured(self, market):
        async with await self.client(market, None) as c:
            r = await c.post("/api/ask/NSE:BANKNIFTY", json={"question": "x"})
        assert r.status_code == 503 and "ANTHROPIC_API_KEY" in r.json()["detail"]

    async def test_validation_and_rate_limit(self, market):
        async with await self.client(market, FakeAssistant(), ask_per_minute=2) as c:
            assert (await c.post("/api/ask/NSE:BANKNIFTY", json={"question": "x" * 1001})).status_code == 422
            assert (await c.post("/api/ask/NSE:BANKNIFTY", json={"question": "  "})).status_code == 422
            assert (await c.post("/api/ask/NSE:BANKNIFTY", json={"q": "x"})).status_code == 422
            codes = [(await c.post("/api/ask/NSE:BANKNIFTY", json={"question": "x"})).status_code for _ in range(3)]
        assert codes == [200, 200, 429]


async def test_static_assets_are_served_with_module_safe_types(tmp_path, dataset):
    import mimetypes

    market, *_ = await live(dataset)
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<script type=module src=/assets/app.js></script>")
    (tmp_path / "assets" / "app.js").write_text("export {}")
    (tmp_path / "assets" / "app.css").write_text("body{}")
    mimetypes.add_type("text/plain", ".js")  # what a Windows registry often says
    app = create_app(market, web_dir=tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        js = await c.get("/assets/app.js")
        css = await c.get("/assets/app.css")
        assert (await c.get("/")).status_code == 200
    assert js.headers["content-type"].startswith("text/javascript")
    assert css.headers["content-type"].startswith("text/css")
