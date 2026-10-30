import { describe, expect, it } from "vitest";
import type { Snapshot } from "../types";
import { barTime, foldTick, IST_OFFSET_S } from "./bars";
import { freshness } from "./freshness";
import { parseSSE } from "./sse";
import { mergeDiff, originWallMs, serverNowMs, usePulse } from "./store";

const base = {
  symbol: "NSE:BANKNIFTY",
  version: 1,
  quote: { value: { ltp: 100 }, as_of: "2026-09-22T05:00:00+00:00", ttl_seconds: 5, stale: false, source: "synthetic" },
  levels: { value: [{ name: "Pivot", price: 99 }] },
  signal: { status: "no_signal" },
} as unknown as Snapshot;

describe("mergeDiff", () => {
  it("replaces only changed sections and keeps the identity of the rest", () => {
    const next = mergeDiff(base, { version: 2, quote: { ...base.quote, value: { ...base.quote.value, ltp: 101 } } }, []);
    expect(next.quote.value.ltp).toBe(101);
    expect(next.version).toBe(2);
    expect(next.levels).toBe(base.levels); // same object -> memoised panels skip re-render
    expect(next.signal).toBe(base.signal);
  });

  it("removes sections the server dropped", () => {
    const next = mergeDiff(base, {}, ["signal"]);
    expect("signal" in next).toBe(false);
  });
});

describe("store.apply", () => {
  it("applies snapshot, diff, chain and news messages", () => {
    const s = usePulse.getState();
    s.setSymbol("NSE:BANKNIFTY");
    s.apply({ type: "diff", now: "x", version: 9, changed: {}, removed: [], engine_wall: null, gateway_wall: 0 });
    expect(usePulse.getState().snapshot).toBeNull(); // a diff without a base is ignored
    s.apply({ type: "snapshot", now: "2026-09-22T05:00:00+00:00", version: 1, snapshot: base });
    const t = Date.now();
    s.apply(
      { type: "diff", now: "2026-09-22T05:00:15+00:00", version: 2, changed: { version: 2 }, removed: [], engine_wall: t / 1000 - 0.12, gateway_wall: t / 1000 },
      t,
    );
    const st = usePulse.getState();
    expect(st.snapshot?.version).toBe(2);
    s.apply({ type: "news", now: "2026-09-22T05:00:20+00:00", version: 4 });
    expect(usePulse.getState().newsVersion).toBe(4);
  });

  it("latency is measured from the tick when known, else from the engine publish", () => {
    const d = { type: "diff" as const, now: "x", version: 1, changed: {}, removed: [] as string[], gateway_wall: 3 };
    expect(originWallMs({ ...d, engine_wall: 2, tick_wall: 1.5 })).toBe(1500);
    expect(originWallMs({ ...d, engine_wall: 2 })).toBe(2000);
    expect(originWallMs({ type: "news", now: "x", version: 1 })).toBeNull();
  });

  it("extrapolates the server clock from the last message", () => {
    const now = serverNowMs({ serverNow: "2026-09-22T05:00:00Z", receivedAtMs: 1000 }, 3000);
    expect(now).toBe(Date.parse("2026-09-22T05:00:02Z"));
  });
});

describe("freshness", () => {
  const sec = { as_of: "2026-09-22T05:00:00Z", ttl_seconds: 60, stale: false };
  const at = (s: number) => Date.parse("2026-09-22T05:00:00Z") + s * 1000;
  it("fresh within ttl, stale beyond it or when the server says so", () => {
    expect(freshness(sec, at(30), 100).status).toBe("fresh");
    expect(freshness(sec, at(61), 100).status).toBe("stale");
    expect(freshness({ ...sec, stale: true }, at(1), 100).status).toBe("stale");
  });
  it("missing and silent", () => {
    expect(freshness(null, at(1), 0).status).toBe("missing");
    expect(freshness(sec, at(1), 11_000).status).toBe("silent");
  });
});

describe("bars", () => {
  it("bar time is the minute open, shifted to IST", () => {
    expect(barTime("2026-09-22T05:01:45Z")).toBe(Date.parse("2026-09-22T05:01:00Z") / 1000 + IST_OFFSET_S);
  });
  it("folds ticks into the forming bar and opens new bars on the minute", () => {
    let b = foldTick(null, 100, "2026-09-22T05:01:00Z")!;
    b = foldTick(b, 103, "2026-09-22T05:01:15Z")!;
    b = foldTick(b, 98, "2026-09-22T05:01:30Z")!;
    expect([b.open, b.high, b.low, b.close]).toEqual([100, 103, 98, 98]);
    const n = foldTick(b, 99, "2026-09-22T05:02:00Z")!;
    expect(n.time).toBe(b.time + 60);
    expect(n.open).toBe(98);
    expect(foldTick(n, 120, "2026-09-22T05:00:00Z")).toBeNull(); // never goes backwards
  });
});

describe("parseSSE", () => {
  it("parses complete events and keeps a partial one for the next chunk", () => {
    const NL = String.fromCharCode(10);
    const chunk1 = ["event: meta", 'data: {"source":"synthetic"}', "", "event: delta", 'data: {"text":"Spo'].join(NL);
    const a = parseSSE(chunk1);
    expect(a.events).toEqual([{ event: "meta", data: { source: "synthetic" } }]);
    const chunk2 = ['t is up"}', "", "event: done", 'data: {"unverified_numbers":[]}', "", ""].join(NL);
    const b = parseSSE(a.rest + chunk2);
    expect(b.events.map((e) => e.event)).toEqual(["delta", "done"]);
    expect(b.events[0].data).toEqual({ text: "Spot is up" });
    expect(b.rest).toBe("");
  });
});
