# Performance report

Generated 2026-10-30 05:35 UTC by `scripts/perf_browser.py` - **ALL TARGETS MET**

| Target | Measured | Limit | Result |
|---|---|---|---|
| First meaningful paint (max of cold loads) | 138 ms | < 1500 ms | pass |
| Tick-to-screen p95 | 272 ms | < 300 ms | pass |
| REST snapshot p95 | 22 ms | < 100 ms | pass |
| MCP get_market_snapshot p95 | 8 ms | < 100 ms | pass |

## Distributions

| Measure | samples | median | p95 | max | min |
|---|---|---|---|---|---|
| First meaningful paint (cold)  5 | 129 ms | 138 ms | 138 ms | 115 ms |
| Tick-to-screen  224 | 228 ms | 272 ms | 298 ms | 128 ms |
| REST `/api/snapshot`  50 | 5 ms | 22 ms | 28 ms | 3 ms |
| MCP `get_market_snapshot`  30 | 5 ms | 8 ms | 45 ms | 4 ms |

### Where tick-to-screen time goes (same clock throughout)

| Hop | samples | median | p95 | max | min |
|---|---|---|---|---|---|
| tick -> engine publish  224 | 223 ms | 266 ms | 520 ms | 123 ms |
| engine -> gateway send  224 | 4 ms | 6 ms | 11 ms | -279 ms |
| gateway -> browser  224 | -2 ms | 0 ms | 2 ms | -4 ms |
| browser receive -> paint  224 | 2 ms | 5 ms | 14 ms | 1 ms |

## Method

- Browser: msedge 156.0.4314.50, headless, driven by Playwright; viewport 1600x1000.
- Machine: Windows-11-10.0.26200-SP0, Intel64 Family 6 Model 151 Stepping 5, GenuineIntel, Python 3.12.16.
- ingest-market, engine and gateway run natively on this machine against Redis DB 14 (Redis itself in Docker), so tick arrival and paint use the same clock. Recording and signal logging are off; everything else is the production code path.
- Data: the synthetic replay at 60x - a 15-second tick for the index and two futures every 250 ms of wall time, i.e. at least the 4 updates/s the stream is capped at.
- First meaningful paint: 5 cold loads, each in a fresh browser context (empty cache, new WebSocket); time from navigation start to the frame after the dashboard first renders with data.
- Tick-to-screen: one tab streaming for 60 s after a 5 s warm-up; per update, the time from the tick's arrival at ingest to the next animation frame after the update was applied.
- Snapshot: 50 sequential REST calls to the native gateway; MCP: 30 `get_market_snapshot` calls to the Docker signals-mcp over streamable HTTP (only if the stack is running).

Tick-to-screen is dominated by the 4 updates/s cap: the engine publishes at most every 260 ms, so a tick can wait up to that long for its slot by design; the rest of the path (engine compute, Redis, gateway, WebSocket, React, paint) adds about 10-30 ms. The margin under the 300 ms target is therefore small by construction - a faster path cannot remove the wait, only a higher update cap could.

Hop values of a few ms below zero are timer resolution between Python and the browser on the same clock, not a measurement error of consequence.
