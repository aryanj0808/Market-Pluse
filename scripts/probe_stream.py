"""Measure the gateway the way a browser sees it: page + assets over HTTP, then the WebSocket
stream (time to first snapshot, diff rate vs the 4/s cap, diff sizes, engine->client latency).

    uv run python scripts/probe_stream.py [--base http://127.0.0.1:8000] [--seconds 15]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import time
import urllib.request

import websockets


def get(base: str, path: str) -> tuple[float, int, bytes]:
    t = time.perf_counter()
    with urllib.request.urlopen(base + path, timeout=10) as r:
        body = r.read()
        return (time.perf_counter() - t) * 1000, r.status, body


def clock_offset(base: str, samples: int = 15) -> float:
    """Server wall clock minus ours (seconds), NTP-style: keep the sample with the shortest round trip."""
    best = None
    for _ in range(samples):
        t0 = time.time()
        with urllib.request.urlopen(base + "/api/health", timeout=5) as r:
            server = json.loads(r.read())["wall"]
        t1 = time.time()
        if best is None or t1 - t0 < best[0]:
            best = (t1 - t0, server - (t0 + t1) / 2)
    return best[1]


async def stream(base: str, symbol: str, seconds: float, offset: float = 0.0) -> None:
    url = base.replace("http", "ws", 1) + f"/api/stream/{symbol}"
    t0 = time.perf_counter()
    async with websockets.connect(url, max_size=None) as ws:
        first = json.loads(await ws.recv())
        t_first = (time.perf_counter() - t0) * 1000
        snap_kb = len(json.dumps(first.get("snapshot", {}))) / 1024
        types: dict[str, int] = {}
        sizes, lat, tick_lat, times = [], [], [], []
        hop_tick_engine, hop_engine_gateway = [], []
        end = time.perf_counter() + seconds
        while (left := end - time.perf_counter()) > 0:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=left)
            except TimeoutError:
                break
            now = time.time() + offset  # in the server's clock
            m = json.loads(raw)
            types[m["type"]] = types.get(m["type"], 0) + 1
            if m["type"] == "diff":
                sizes.append(len(raw))
                times.append(time.perf_counter())
                if m.get("engine_wall"):
                    lat.append((now - m["engine_wall"]) * 1000)
                if m.get("tick_wall"):
                    tick_lat.append((now - m["tick_wall"]) * 1000)
                    hop_tick_engine.append((m["engine_wall"] - m["tick_wall"]) * 1000)
                    hop_engine_gateway.append((m["gateway_wall"] - m["engine_wall"]) * 1000)
    gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
    print(f"WS first message: {first['type']} after {t_first:.0f} ms ({snap_kb:.0f} KB)")
    print(f"messages in {seconds:.0f}s: {types} -> diffs/sec {types.get('diff', 0) / seconds:.2f} (cap 4)")
    if gaps:
        print(f"min gap between diffs: {min(gaps) * 1000:.0f} ms")
    if sizes:
        print(
            f"diff size: median {statistics.median(sizes) / 1024:.1f} KB, max {max(sizes) / 1024:.1f} KB "
            f"(full snapshot {snap_kb:.0f} KB)"
        )

    def show(name: str, xs: list[float]) -> None:
        if xs:
            q = sorted(xs)
            p95 = q[int(0.95 * (len(q) - 1))]
            print(f"  {name:<26} median {statistics.median(q):6.1f}  p95 {p95:6.1f}  max {q[-1]:6.1f}  (n={len(q)})")

    print("latency, server clock only (skew-free):")
    show("tick -> engine publish", hop_tick_engine)
    show("engine -> gateway send", hop_engine_gateway)
    show("tick -> gateway send", [a + b for a, b in zip(hop_tick_engine, hop_engine_gateway, strict=True)])
    print("latency across machines (corrected by the estimated clock offset; unreliable if the offset is):")
    show("tick -> client", tick_lat)
    if tick_lat and statistics.median(tick_lat) < statistics.median(hop_tick_engine):
        print("  ! client-side numbers are below the server-only ones: the clock offset estimate is off")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--symbol", default="NSE:BANKNIFTY")
    ap.add_argument("--seconds", type=float, default=15)
    a = ap.parse_args()
    deadline = time.time() + 60  # wait for the gateway to be ready (e.g. right after a rebuild)
    while True:
        try:
            ms, st, html = get(a.base, "/")
            break
        except OSError:
            if time.time() > deadline:
                raise
            time.sleep(1)
    print(f"GET / {st} {ms:.0f} ms")
    for asset in re.findall(rb'(?:src|href)="(/assets/[^"]+)"', html):
        ms, st, body = get(a.base, asset.decode())
        print(f"GET {asset.decode()} {st} {ms:.0f} ms {len(body) / 1024:.0f} KB")
    ms, st, body = get(a.base, f"/api/snapshot/{a.symbol}")
    print(f"GET /api/snapshot {st} {ms:.0f} ms {len(body) / 1024:.0f} KB")
    off = clock_offset(a.base)
    print(f"clock offset server-local: {off * 1000:+.1f} ms (corrected below)")
    asyncio.run(stream(a.base, a.symbol, a.seconds, off))


if __name__ == "__main__":
    main()
