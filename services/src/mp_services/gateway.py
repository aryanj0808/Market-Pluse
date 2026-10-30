"""gateway: the dashboard's only backend. Serves cached state from Redis - never waits on an LLM,
an MCP round-trip or the engine.

    GET  /api/health
    GET  /api/snapshot/{symbol}            {"now", "snapshot"}   (raw cached JSON, no re-serialising)
    GET  /api/chain/{symbol}?expiry=       Sourced[OptionChain]  (front expiry by default)
    GET  /api/candles/{symbol}?which=index|future                today's completed 1m bars
    GET  /api/news/{symbol}?hours=24&min_impact=low              relevant classified items
    GET  /api/stats/{symbol}?days=90       signal statistics (real data only; refuses synthetic)
    WS   /api/stream/{symbol}              snapshot, then per-section diffs (max 4/s)
    POST /api/ask/{symbol}                 Ask-Claude: SSE stream (meta, delta..., done | error)
    GET  /                                 the built React app (if MP_WEB_DIR exists)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import mimetypes
import os
import time
from collections import defaultdict, deque
from datetime import timedelta
from pathlib import Path

import anthropic
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from mp_core.assistant import MAX_QUESTION_CHARS, Assistant, verify_numbers
from mp_core.digest import market_digest
from mp_core.health import read as read_health
from mp_core.models import InstrumentKey, StrictModel
from mp_core.news.models import Impact
from mp_core.news.score import entity_weights, relevant_items
from mp_core.news.store import NewsStore
from mp_core.registry import get_index
from mp_core.settings import get_settings
from mp_core.signals.evaluation import compute_stats
from mp_core.store import MarketStore, RedisKV
from mp_core.store.history import History
from mp_core.stream import SnapshotStreamer
from mp_services._common import setup_logging

log = logging.getLogger("gateway")


class AskBody(StrictModel):
    question: str


def _symbol(symbol: str) -> str:
    try:
        get_index(symbol)
    except KeyError:
        raise HTTPException(404, f"unknown index {symbol!r}") from None
    return InstrumentKey.parse(symbol if ":" in symbol else f"NSE:{symbol}").symbol


def _json(body: str) -> Response:
    return Response(content=body, media_type="application/json", headers={"Cache-Control": "no-store"})


def create_app(
    store: MarketStore,
    *,
    database_url: str | None = None,
    web_dir: Path | None = None,
    max_per_sec: float = 4.0,
    min_gap: float = 0.1,
    assistant: Assistant | None = None,
    ask_per_minute: int = 6,
    rest_rate: float = 20.0,
    rest_burst: float = 40.0,
) -> FastAPI:
    """Clients get at most `max_per_sec` diffs in any 1-second window (see mp_core.stream).
    `assistant` None disables Ask-Claude (503); questions are limited per client and in flight."""
    app = FastAPI(title="Market Pulse gateway", docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["GET"],
        allow_headers=["*"],
    )
    news = NewsStore(store.kv)
    buckets: dict[str, tuple[float, float]] = {}  # client -> (tokens, last refill)

    @app.middleware("http")
    async def rate_limit(request: Request, call_next):
        """Token bucket per client for the REST API (the stream and health checks are exempt)."""
        path = request.url.path
        if path.startswith("/api/") and path != "/api/health" and not path.startswith("/api/stream"):
            who = request.client.host if request.client else "?"
            now_m = time.monotonic()
            tokens, last = buckets.get(who, (rest_burst, now_m))
            tokens = min(rest_burst, tokens + (now_m - last) * rest_rate)
            if tokens < 1:
                buckets[who] = (tokens, now_m)
                return JSONResponse(
                    {"detail": "too many requests"},
                    status_code=429,
                    headers={"Retry-After": str(max(1, int((1 - tokens) / rest_rate) + 1))},
                )
            buckets[who] = (tokens - 1, now_m)
            if len(buckets) > 10_000:  # bound memory against many clients
                buckets.clear()
        return await call_next(request)

    state: dict = {"history": None}

    async def history() -> History:
        if not database_url:
            raise HTTPException(503, "history unavailable: DATABASE_URL is not configured")
        if state["history"] is None:
            state["history"] = await History(database_url).connect()
        return state["history"]

    @app.get("/api/health")
    async def health() -> dict:
        """Gateway liveness (`ok`), plus every background service's heartbeat (`services`)."""
        try:
            services = await read_health(store.kv)
        except Exception as ex:  # Redis down: the gateway is alive but blind
            services = {"redis": {"ok": False, "status": f"unreachable ({type(ex).__name__})"}}
        return {
            "ok": True,
            "services": services,
            "all_ok": all(v["ok"] for v in services.values()),
            "clock": (await store.now()).isoformat(),
            "mode": await store.clock_mode(),
            "wall": time.time(),
        }  # lets probes correct for host/container clock skew

    @app.get("/api/snapshot/{symbol}")
    async def snapshot(symbol: str) -> Response:
        sym = _symbol(symbol)
        raw = await store.get_snapshot_json(sym)
        if raw is None:
            raise HTTPException(404, f"no snapshot for {sym} yet - is the engine running?")
        return _json(f'{{"now":"{(await store.now()).isoformat()}","snapshot":{raw}}}')

    @app.get("/api/chain/{symbol}")
    async def chain(symbol: str, expiry: str | None = None) -> Response:
        sym = _symbol(symbol)
        exps = await store.get_expiries(sym)
        if not exps:
            raise HTTPException(404, "no expiries cached")
        key = expiry or exps[0].isoformat()
        raw = await store.kv.get(f"mp:{sym}:chain:{key}")
        if raw is None:
            raise HTTPException(404, f"no chain for {key}; have {[e.isoformat() for e in exps]}")
        return _json(raw)

    @app.get("/api/candles/{symbol}")
    async def candles(symbol: str, which: str = Query("index", pattern="^(index|future)$")) -> Response:
        sym = _symbol(symbol)
        key = InstrumentKey.parse(sym) if which == "index" else await store.get_future_key(sym)
        raw = await store.kv.get(f"mp:{sym}:candles1m:{key.id}") if key else None
        if raw is None:
            raise HTTPException(404, f"no {which} candles cached for {sym}")
        return _json(raw)

    @app.get("/api/news/{symbol}")
    async def news_items(
        symbol: str, hours: int = Query(24, ge=1, le=168), min_impact: str = Query("low", pattern="^(low|med|high)$")
    ) -> dict:
        sym = _symbol(symbol)
        now = await store.now()
        items = await news.recent(now - timedelta(hours=hours), now)
        cons = await store.get_constituents(sym)
        weights = entity_weights(get_index(sym), {c.key.underlying: c.weight for c in cons.value} if cons else None)
        poll = await news.last_poll()
        return {
            "now": now.isoformat(),
            "poll": poll.model_dump(mode="json", exclude={"value"}) if poll else None,
            "items": relevant_items(items, weights, Impact(min_impact)),
            "unclassified": sum(s.status == "unclassified" for s in items),
            "feeds": [h.model_dump(mode="json") for h in await news.get_health()],
        }

    @app.get("/api/stats/{symbol}")
    async def stats(symbol: str, days: int = Query(90, ge=1, le=3650)) -> dict:
        sym = _symbol(symbol)
        hist = await history()
        since = await store.now() - timedelta(days=days)
        s = compute_stats(
            sym, await hist.outcomes(sym, since), since, n_synthetic_signals=await hist.count_synthetic_signals(sym)
        )
        return s.model_dump(mode="json")

    ask_slots = asyncio.Semaphore(2)
    ask_log: dict[str, deque[float]] = defaultdict(deque)

    @app.post("/api/ask/{symbol}")
    async def ask(symbol: str, body: AskBody, request: Request) -> StreamingResponse:
        """Answer a question from the current market digest. Runs on its own request and never
        touches the stream, so a slow answer cannot hold up the dashboard."""
        sym = _symbol(symbol)
        if assistant is None:
            raise HTTPException(503, "Ask-Claude is not configured: set ANTHROPIC_API_KEY for the gateway")
        if not body.question.strip() or len(body.question) > MAX_QUESTION_CHARS:
            raise HTTPException(422, f"question must be 1-{MAX_QUESTION_CHARS} characters")
        who = request.client.host if request.client else "?"
        now_m = time.monotonic()
        recent = ask_log[who]
        while recent and now_m - recent[0] > 60:
            recent.popleft()
        if len(recent) >= ask_per_minute:
            raise HTTPException(429, f"limit is {ask_per_minute} questions per minute")
        recent.append(now_m)
        snap = await store.get_snapshot(sym)
        if snap is None:
            raise HTTPException(404, f"no snapshot for {sym} yet")
        digest = market_digest(snap, await store.now())

        async def events():
            def sse(event: str, data: dict) -> str:
                return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

            yield sse(
                "meta",
                {k: digest[k] for k in ("symbol", "source", "as_of", "stale", "now")}
                | {"stale_sections": snap.stale_sections, "model": assistant.model},
            )
            answer = []
            try:
                async with ask_slots:
                    async for ev in assistant.ask(body.question, digest):
                        if ev.kind == "text":
                            answer.append(ev.text)
                            yield sse("delta", {"text": ev.text})
                        elif ev.kind == "refusal":
                            yield sse("error", {"message": "Claude declined to answer this question.", **ev.info})
                            return
                        else:
                            text = "".join(answer)
                            yield sse("done", {**ev.info, "unverified_numbers": verify_numbers(text, digest)})
            except anthropic.APIStatusError as ex:
                log.warning("ask failed: %s", ex)
                yield sse("error", {"message": f"Claude API error ({ex.status_code})"})
            except anthropic.APIConnectionError:
                yield sse("error", {"message": "could not reach the Claude API"})

        return StreamingResponse(
            events(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
        )

    @app.websocket("/api/stream/{symbol}")
    async def stream(ws: WebSocket, symbol: str) -> None:
        try:
            sym = _symbol(symbol)
        except HTTPException:
            await ws.close(code=4404)
            return
        await ws.accept()
        stop = asyncio.Event()
        streamer = SnapshotStreamer(store, sym, ws.send_text, max_per_sec=max_per_sec, min_gap=min_gap)

        async def watch_close() -> None:
            with contextlib.suppress(WebSocketDisconnect, RuntimeError):
                while True:
                    await ws.receive_text()  # the client sends nothing meaningful; this detects close
            stop.set()

        watcher = asyncio.create_task(watch_close())
        try:
            await streamer.run(stop)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            stop.set()
            watcher.cancel()
            log.info("stream %s closed after %s", sym, streamer.sent)

    if web_dir is not None and web_dir.is_dir():
        # Python guesses types from the Windows registry, which often maps .js to text/plain - and
        # browsers refuse module scripts served as text/plain. Pin the types the app needs.
        for ext, mime in (
            (".js", "text/javascript"),
            (".mjs", "text/javascript"),
            (".css", "text/css"),
            (".svg", "image/svg+xml"),
            (".json", "application/json"),
            (".woff2", "font/woff2"),
        ):
            mimetypes.add_type(mime, ext)
        app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")
    return app


def main() -> None:
    import uvicorn

    setup_logging()
    s = get_settings()
    web = Path(os.environ.get("MP_WEB_DIR", "web/dist"))
    assistant = Assistant.from_env(effort=os.environ.get("MP_ASK_EFFORT", "low"))
    if assistant is None:
        log.warning("Ask-Claude disabled: no Anthropic credentials")
    app = create_app(MarketStore(RedisKV(s.redis_url)), database_url=s.database_url, web_dir=web, assistant=assistant)
    uvicorn.run(
        app,
        host=os.environ.get("MP_GATEWAY_HOST", "127.0.0.1"),
        port=int(os.environ.get("MP_GATEWAY_PORT", "8000")),
        log_level="info",
        ws_ping_interval=20,
    )
