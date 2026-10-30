import { useEffect } from "react";
import type { StreamMessage } from "../types";
import { recordHops, recordLatency } from "./perf";
import { originWallMs, usePulse } from "./store";

/** Keep one WebSocket to the gateway for the selected symbol; reconnect with backoff. The UI
 *  renders from the store this feeds - it never polls the snapshot. */
export function useStream(symbol: string): void {
  useEffect(() => {
    let ws: WebSocket | null = null;
    let retry = 0;
    let timer: number | undefined;
    let closed = false;
    const { apply, setConnection } = usePulse.getState();

    const connect = () => {
      setConnection("connecting");
      const proto = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${location.host}/api/stream/${encodeURIComponent(symbol)}`);
      ws.onopen = () => {
        retry = 0;
        setConnection("open");
      };
      ws.onmessage = (ev) => {
        try {
          const recvMs = Date.now();
          const msg = JSON.parse(ev.data as string) as StreamMessage;
          apply(msg);
          const origin = originWallMs(msg);
          if (origin !== null) {
            // React renders the store change synchronously; the next frame is when it is painted
            requestAnimationFrame(() => {
              const paintMs = Date.now();
              const ms = Math.max(0, paintMs - origin);
              usePulse.getState().setLatency(ms);
              recordLatency(ms);
              if (msg.type === "diff" && msg.tick_wall && msg.engine_wall)
                recordHops(msg.tick_wall, msg.engine_wall, msg.gateway_wall, recvMs, paintMs);
            });
          }
        } catch (err) {
          console.error("bad stream message", err);
        }
      };
      ws.onclose = () => {
        setConnection("closed");
        if (closed) return;
        const delay = Math.min(5000, 500 * 2 ** retry++);
        timer = window.setTimeout(connect, delay);
      };
    };
    connect();
    return () => {
      closed = true;
      window.clearTimeout(timer);
      ws?.close();
    };
  }, [symbol]);
}
