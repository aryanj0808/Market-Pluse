import { memo } from "react";
import { pct, px } from "../lib/format";
import type { Constituent } from "../types";

function tone(changePct: number) {
  const a = Math.min(0.9, Math.abs(changePct) / 2.5 + 0.08);
  return changePct >= 0 ? `rgba(16,185,129,${a})` : `rgba(244,63,94,${a})`;
}

/** Tiles sized by index weight, coloured by % change; tooltip shows the contribution to the index. */
export const ConstituentsHeatmap = memo(function ConstituentsHeatmap({ items }: { items: Constituent[] | null }) {
  if (!items) return <div className="p-4 text-sm text-slate-500">No constituent data.</div>;
  const sorted = [...items].sort((a, b) => b.weight - a.weight);
  return (
    <div className="flex h-full flex-wrap content-start gap-1 overflow-y-auto p-2">
      {sorted.map((c) => {
        const ch = ((c.ltp - c.prev_close) / c.prev_close) * 100;
        const contrib = (c.weight * ch) / 100;
        return (
          <div
            key={c.key.underlying}
            className="flex min-w-[64px] flex-col justify-between rounded p-1.5 text-[11px] text-white"
            style={{ backgroundColor: tone(ch), flexGrow: c.weight, flexBasis: `${Math.max(8, c.weight * 1.6)}%`, minHeight: 52 }}
            title={`${c.name}\nweight ${c.weight.toFixed(2)}% · ltp ${px(c.ltp)}\ncontribution ${contrib >= 0 ? "+" : ""}${contrib.toFixed(3)} pct-pts of the index move`}
          >
            <span className="font-semibold">{c.key.underlying}</span>
            <span className="tabular">{pct(ch)}</span>
            <span className="tabular text-[10px] opacity-75">w {c.weight.toFixed(1)}%</span>
          </div>
        );
      })}
    </div>
  );
});
