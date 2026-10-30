// Performance marks read by the browser performance test (scripts/perf_browser.py).
//   fmpMs      ms from navigation start until the dashboard was first painted WITH data
//   latencies  tick arrival at ingest -> painted here, wall-clock ms (one per stream diff)
// Costs one array push per update; capped so a long-running tab cannot grow without bound.

interface PerfMarks {
  fmpMs: number | null;
  latencies: number[];
  /** per update, ms per hop: [tick->engine, engine->gateway send, send->browser receive, receive->paint] */
  hops: [number, number, number, number][];
}

declare global {
  interface Window {
    __mpPerf?: PerfMarks;
  }
}

const MAX_SAMPLES = 5000;

function marks(): PerfMarks {
  if (!window.__mpPerf) window.__mpPerf = { fmpMs: null, latencies: [], hops: [] };
  return window.__mpPerf;
}

export function markFirstMeaningfulPaint(): void {
  const m = marks();
  if (m.fmpMs === null) m.fmpMs = performance.now();
}

export function recordLatency(ms: number): void {
  const l = marks().latencies;
  if (l.length >= MAX_SAMPLES) l.shift();
  l.push(ms);
}

/** Hop breakdown for one update (all wall-clock seconds except the browser's ms). */
export function recordHops(tickWall: number, engineWall: number, gatewayWall: number, recvMs: number, paintMs: number) {
  const h = marks().hops;
  if (h.length >= MAX_SAMPLES) h.shift();
  h.push([(engineWall - tickWall) * 1000, (gatewayWall - engineWall) * 1000, recvMs - gatewayWall * 1000, paintMs - recvMs]);
}
