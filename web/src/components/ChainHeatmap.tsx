import { useVirtualizer } from "@tanstack/react-virtual";
import { memo, useEffect, useMemo, useRef } from "react";
import { compact, px0 } from "../lib/format";
import type { ExpiryView, OptionChain } from "../types";

const ROW = 24;

function heat(value: number, max: number, rgb: string) {
  const a = max > 0 ? Math.min(0.85, (value / max) * 0.85) : 0;
  return { backgroundColor: `rgba(${rgb},${a.toFixed(3)})` };
}

/** Option chain as an OI/IV heatmap, virtualised (only visible rows are in the DOM). */
export const ChainHeatmap = memo(function ChainHeatmap({
  chain,
  expiry,
}: {
  chain: OptionChain | null;
  expiry: ExpiryView | null;
}) {
  const scroller = useRef<HTMLDivElement>(null);
  const rows = chain?.rows ?? [];
  const ivs = useMemo(() => new Map((expiry?.iv.strikes ?? []).map((s) => [s.strike, s])), [expiry]);
  const maxOi = useMemo(() => Math.max(1, ...rows.flatMap((r) => [r.call?.oi ?? 0, r.put?.oi ?? 0])), [rows]);
  const maxChg = useMemo(
    () => Math.max(1, ...rows.flatMap((r) => [Math.abs(r.call?.oi_change ?? 0), Math.abs(r.put?.oi_change ?? 0)])),
    [rows],
  );
  const atm = useMemo(
    () => (chain ? rows.reduce((b, r, i) => (Math.abs(r.strike - chain.spot) < Math.abs(rows[b].strike - chain.spot) ? i : b), 0) : 0),
    [chain, rows],
  );
  const v = useVirtualizer({ count: rows.length, getScrollElement: () => scroller.current, estimateSize: () => ROW, overscan: 10 });

  // centre the ATM strike when a new expiry/chain first arrives
  const centred = useRef<string | null>(null);
  useEffect(() => {
    if (chain && centred.current !== chain.expiry && rows.length) {
      centred.current = chain.expiry;
      v.scrollToIndex(atm, { align: "center" });
    }
  }, [chain, atm, rows.length, v]);

  if (!chain) return <div className="p-4 text-sm text-slate-500">Waiting for the option chain…</div>;
  const cell = "tabular px-1 text-right";
  return (
    <div className="flex h-full flex-col text-[11px]">
      <div className="grid grid-cols-[1fr_1fr_0.7fr_0.9fr_0.7fr_1fr_1fr] border-b border-slate-800 px-1 py-1 text-slate-500">
        <span className="text-right">CE OI</span>
        <span className="text-right">CE ΔOI</span>
        <span className="text-right">CE IV</span>
        <span className="text-center">Strike</span>
        <span className="text-right">PE IV</span>
        <span className="text-right">PE ΔOI</span>
        <span className="text-right">PE OI</span>
      </div>
      <div ref={scroller} className="min-h-0 flex-1 overflow-y-auto">
        <div style={{ height: v.getTotalSize(), position: "relative" }}>
          {v.getVirtualItems().map((it) => {
            const r = rows[it.index];
            const iv = ivs.get(r.strike);
            const isAtm = it.index === atm;
            return (
              <div
                key={r.strike}
                className={`absolute left-0 right-0 grid grid-cols-[1fr_1fr_0.7fr_0.9fr_0.7fr_1fr_1fr] items-center px-1 ${
                  isAtm ? "outline outline-1 outline-amber-400/60" : ""
                }`}
                style={{ height: ROW, transform: `translateY(${it.start}px)` }}
              >
                <span className={cell} style={heat(r.call?.oi ?? 0, maxOi, "244,63,94")}>{compact(r.call?.oi)}</span>
                <span className={cell} style={heat(Math.max(0, r.call?.oi_change ?? 0), maxChg, "244,63,94")}>
                  {compact(r.call?.oi_change)}
                </span>
                <span className={`${cell} text-slate-400`}>{iv?.call_iv ? (iv.call_iv * 100).toFixed(1) : "–"}</span>
                <span className={`tabular text-center font-semibold ${isAtm ? "text-amber-300" : "text-slate-200"}`}>
                  {px0(r.strike)}
                </span>
                <span className={`${cell} text-slate-400`}>{iv?.put_iv ? (iv.put_iv * 100).toFixed(1) : "–"}</span>
                <span className={cell} style={heat(Math.max(0, r.put?.oi_change ?? 0), maxChg, "16,185,129")}>
                  {compact(r.put?.oi_change)}
                </span>
                <span className={cell} style={heat(r.put?.oi ?? 0, maxOi, "16,185,129")}>{compact(r.put?.oi)}</span>
              </div>
            );
          })}
        </div>
      </div>
      {expiry && (
        <div className="tabular flex gap-3 border-t border-slate-800 px-2 py-1 text-slate-400">
          <span>ATM IV {expiry.iv.atm_iv ? (expiry.iv.atm_iv * 100).toFixed(1) + "%" : "–"}</span>
          <span>PCR {expiry.pcr.pcr_oi?.toFixed(2) ?? "–"}</span>
          <span>Max pain {px0(expiry.pcr.max_pain)}</span>
          <span className="ml-auto">IV in %, OI in units</span>
        </div>
      )}
    </div>
  );
});
