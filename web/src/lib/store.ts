import { create } from "zustand";
import type { OptionChain, Snapshot, Sourced, StreamMessage } from "../types";

export type Connection = "connecting" | "open" | "closed";

export interface PulseState {
  symbol: string;
  snapshot: Snapshot | null;
  chain: Sourced<OptionChain> | null;
  newsVersion: number;
  /** server clock (replay time during a replay) at the last message, and when we got it (wall ms) */
  serverNow: string | null;
  receivedAtMs: number;
  connection: Connection;
  /** tick arrival at ingest -> painted in this browser, wall-clock ms (falls back to engine publish) */
  latencyMs: number | null;
  messages: number;
  setSymbol: (s: string) => void;
  setConnection: (c: Connection) => void;
  apply: (msg: StreamMessage, nowMs?: number) => void;
  setLatency: (ms: number) => void;
}

/** Merge a diff into the snapshot. Sections that did not change keep their object identity, so
 *  components selecting them (and memoised on them) do not re-render. */
export function mergeDiff(prev: Snapshot, changed: Partial<Snapshot>, removed: string[]): Snapshot {
  const next = { ...prev, ...changed } as Snapshot & Record<string, unknown>;
  for (const k of removed) delete next[k];
  return next;
}

export const usePulse = create<PulseState>((set, get) => ({
  symbol: "NSE:BANKNIFTY",
  snapshot: null,
  chain: null,
  newsVersion: 0,
  serverNow: null,
  receivedAtMs: 0,
  connection: "connecting",
  latencyMs: null,
  messages: 0,
  setSymbol: (symbol) =>
    set({ symbol, snapshot: null, chain: null, newsVersion: 0, serverNow: null, latencyMs: null, messages: 0 }),
  setConnection: (connection) => set({ connection }),
  setLatency: (latencyMs) => set({ latencyMs }),
  apply: (msg, nowMs = Date.now()) => {
    const base = { serverNow: msg.now, receivedAtMs: nowMs, messages: get().messages + 1 };
    switch (msg.type) {
      case "snapshot":
        set({ ...base, snapshot: msg.snapshot });
        break;
      case "diff": {
        const prev = get().snapshot;
        if (!prev) return; // a diff without a base is useless; the next snapshot will follow
        set({
          ...base,
          snapshot: mergeDiff(prev, msg.changed, msg.removed),
        });
        break;
      }
      case "chain":
        set({ ...base, chain: msg.chain });
        break;
      case "news":
        set({ ...base, newsVersion: msg.version });
        break;
    }
  },
}));

/** Wall-clock origin of a diff: the tick that triggered it if known, else the engine publish. */
export function originWallMs(msg: StreamMessage): number | null {
  if (msg.type !== "diff") return null;
  const t = msg.tick_wall ?? msg.engine_wall;
  return t ? t * 1000 : null;
}

/** Server time now, extrapolated at 1x from the last message (replays run faster - the
 *  "no updates" check below covers a silent feed). */
export function serverNowMs(state: Pick<PulseState, "serverNow" | "receivedAtMs">, wallMs = Date.now()): number | null {
  if (!state.serverNow) return null;
  return Date.parse(state.serverNow) + (wallMs - state.receivedAtMs);
}
