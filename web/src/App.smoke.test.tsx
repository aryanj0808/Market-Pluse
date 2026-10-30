// Renders the whole dashboard in jsdom with REAL gateway payloads (captured from the running stack:
// src/test/*.json, synthetic data) and a fake WebSocket. Catches runtime errors that type-checking
// cannot - a panel crashing on real data, a bad chart call - without needing a browser.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import candles from "./test/candles.json";
import chain from "./test/chain.json";
import news from "./test/news.json";
import snapshotResp from "./test/snapshot.json";

// lightweight-charts draws on <canvas>, which jsdom lacks: record the calls instead.
const chartCalls: string[] = [];
vi.mock("lightweight-charts", () => {
  const series = {
    setData: (d: unknown[]) => chartCalls.push(`setData:${d.length}`),
    update: () => chartCalls.push("update"),
    createPriceLine: (o: { title: string }) => (chartCalls.push(`line:${o.title}`), o),
    removePriceLine: () => undefined,
  };
  return {
    ColorType: { Solid: "solid" },
    LineStyle: { Solid: 0, Dotted: 1, Dashed: 2, LargeDashed: 3 },
    createChart: () => ({
      addCandlestickSeries: () => series,
      timeScale: () => ({ fitContent: () => undefined }),
      remove: () => undefined,
    }),
  };
});

class FakeWS {
  static last: FakeWS | null = null;
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  constructor(public url: string) {
    FakeWS.last = this;
  }
  close() {}
  emit(msg: unknown) {
    this.onmessage?.({ data: JSON.stringify(msg) });
  }
}

let root: Root | null = null;
let host: HTMLDivElement;

beforeAll(() => {
  vi.stubGlobal("WebSocket", FakeWS);
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  vi.stubGlobal("requestAnimationFrame", (cb: () => void) => setTimeout(cb, 0));
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      const body = url.includes("/api/candles") ? candles : url.includes("/api/news") ? news : { detail: "not mocked" };
      return { ok: !("detail" in body), status: "detail" in body ? 404 : 200, statusText: "", json: async () => body };
    }),
  );
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
});

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
});

const flush = () => act(() => new Promise((r) => setTimeout(r, 20)));

describe("dashboard smoke test (real payloads)", () => {
  it("renders every panel from a real snapshot, chain and news", async () => {
    const { default: App } = await import("./App");
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    act(() =>
      root!.render(
        <QueryClientProvider client={new QueryClient()}>
          <App />
        </QueryClientProvider>,
      ),
    );
    expect(host.textContent).toContain("Connecting to the gateway");
    const ws = FakeWS.last!;
    expect(ws.url).toContain("/api/stream/NSE%3ABANKNIFTY");

    const snap = snapshotResp.snapshot;
    act(() => {
      ws.onopen?.();
      ws.emit({ type: "snapshot", now: snapshotResp.now, version: snap.version, snapshot: snap });
      ws.emit({ type: "chain", now: snapshotResp.now, expiry: chain.value.expiry, chain });
      ws.emit({ type: "news", now: snapshotResp.now, version: 1 });
    });
    await flush();
    const text = host.textContent ?? "";
    expect(text).toContain("SYNTHETIC DATA"); // the warning banner
    expect(text).toContain("Price · levels · expected range");
    expect(text).toContain(snap.signal.status === "active" ? "ACTIVE" : "NO SIGNAL");
    expect(text).toContain("Why");
    for (const r of snap.signal.reasons.slice(0, 3)) expect(text).toContain(r.input); // reasons cite inputs
    expect(text).toContain("Option chain");
    expect(text).toContain("Strike");
    expect(text).toContain("Constituents");
    expect(text).toContain("HDFCBANK");
    expect(chartCalls.some((c) => c.startsWith("setData:"))).toBe(true); // history loaded once
    expect(chartCalls.some((c) => c === "line:+1σ")).toBe(true); // expected-range bands drawn

    // a diff that only moves the quote: the chart updates incrementally, nothing else breaks
    const before = chartCalls.filter((c) => c.startsWith("setData")).length;
    const q = snap.quote;
    const t = new Date(Date.parse(q.value.ts) + 15_000).toISOString();
    act(() =>
      ws.emit({
        type: "diff",
        now: t,
        version: snap.version + 1,
        changed: { version: snap.version + 1, quote: { ...q, as_of: t, value: { ...q.value, ltp: q.value.ltp + 5, ts: t } } },
        removed: [],
        engine_wall: Date.now() / 1000,
        tick_wall: Date.now() / 1000 - 0.05,
        gateway_wall: Date.now() / 1000,
      }),
    );
    await flush();
    expect(chartCalls.at(-1)).toBe("update"); // series.update, not setData
    expect(chartCalls.filter((c) => c.startsWith("setData")).length).toBe(before);
    expect(host.textContent).toContain("tick→screen");

    // Ask-Claude opens beside the dashboard; the dashboard keeps rendering
    const askButton = [...host.querySelectorAll("button")].find((b) => b.textContent === "Ask Claude")!;
    act(() => askButton.click());
    expect(host.textContent).toContain("Answers use only the current market snapshot");
    expect(host.textContent).toContain("Option chain");
  });
});
