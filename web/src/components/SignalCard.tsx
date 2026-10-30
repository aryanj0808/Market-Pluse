import { memo } from "react";
import { pct, px0, signed } from "../lib/format";
import type { Signal } from "../types";

const BIAS_STYLE = {
  bullish: "text-emerald-300",
  bearish: "text-rose-300",
  neutral: "text-slate-300",
};

function ScoreBar({ value }: { value: number }) {
  const w = Math.min(50, Math.abs(value) * 50);
  return (
    <div className="relative h-1.5 w-full rounded bg-slate-800">
      <div className="absolute left-1/2 top-0 h-full w-px bg-slate-600" />
      <div
        className={`absolute top-0 h-full rounded ${value >= 0 ? "bg-emerald-500" : "bg-rose-500"}`}
        style={value >= 0 ? { left: "50%", width: `${w}%` } : { right: "50%", width: `${w}%` }}
      />
    </div>
  );
}

export const SignalCard = memo(function SignalCard({ signal }: { signal: Signal | null }) {
  if (!signal) return <div className="p-4 text-sm text-slate-500">No signal computed yet.</div>;
  const active = signal.status === "active";
  return (
    <div className="flex h-full flex-col gap-3 overflow-y-auto p-3 text-sm">
      <div className="flex items-baseline gap-3">
        <span
          className={`rounded px-2 py-0.5 text-xs font-bold ${
            active ? (signal.bias === "bullish" ? "bg-emerald-600" : "bg-rose-600") : "bg-slate-700"
          }`}
        >
          {active ? "ACTIVE" : "NO SIGNAL"}
        </span>
        <span className={`text-lg font-semibold capitalize ${BIAS_STYLE[signal.bias]}`}>{signal.bias}</span>
        <span className="tabular text-slate-400">composite {signal.composite.toFixed(2)}</span>
        <span className="tabular ml-auto text-slate-400">confidence {pct(signal.confidence * 100, 0).replace("+", "")}</span>
      </div>

      <div className="grid grid-cols-[80px_1fr_44px] items-center gap-x-2 gap-y-1">
        {signal.components.map((c) => (
          <div key={c.name} className="contents">
            <span className={`text-xs capitalize ${c.available ? "text-slate-300" : "text-slate-600"}`}>{c.name}</span>
            {c.available ? <ScoreBar value={c.score} /> : <span className="text-xs text-slate-600">unavailable</span>}
            <span className="tabular text-right text-xs text-slate-400">{c.available ? signed(c.score) : ""}</span>
          </div>
        ))}
      </div>

      <div className="grid grid-cols-2 gap-2 text-xs">
        {signal.buy_zone && (
          <div className="rounded border border-emerald-900 bg-emerald-950/40 p-2">
            <div className="text-emerald-300">Buy zone</div>
            <div className="tabular">
              {px0(signal.buy_zone.low)} – {px0(signal.buy_zone.high)}
            </div>
            <div className="font-mono text-[10px] text-slate-500">{signal.buy_zone.basis.join(", ")}</div>
          </div>
        )}
        {signal.sell_zone && (
          <div className="rounded border border-rose-900 bg-rose-950/40 p-2">
            <div className="text-rose-300">Sell zone</div>
            <div className="tabular">
              {px0(signal.sell_zone.low)} – {px0(signal.sell_zone.high)}
            </div>
            <div className="font-mono text-[10px] text-slate-500">{signal.sell_zone.basis.join(", ")}</div>
          </div>
        )}
      </div>

      {active && (
        <div className="tabular grid grid-cols-2 gap-x-4 gap-y-0.5 rounded bg-slate-800/60 p-2 text-xs">
          <span className="text-slate-400">Entry</span>
          <span>{px0(signal.entry)}</span>
          <span className="text-slate-400">Stop loss</span>
          <span className="text-rose-300">{px0(signal.stop_loss)}</span>
          <span className="text-slate-400">Invalidation</span>
          <span>{px0(signal.invalidation)}</span>
          <span className="text-slate-400">Targets</span>
          <span className="text-emerald-300">{signal.targets.map((t) => px0(t.price)).join(" · ")}</span>
          <span className="text-slate-400">Reward : risk</span>
          <span>{signal.reward_risk?.toFixed(2)}</span>
          {signal.sizing && (
            <>
              <span className="text-slate-400">Size (suggestion)</span>
              <span>
                {signal.sizing.lots} × {signal.sizing.lot_size} · ₹{px0(signal.sizing.loss_at_stop)} at stop
              </span>
            </>
          )}
        </div>
      )}

      {signal.no_signal_reasons.length > 0 && (
        <ul className="space-y-1 text-xs text-amber-300">
          {signal.no_signal_reasons.map((r) => (
            <li key={r}>• {r}</li>
          ))}
        </ul>
      )}

      <div>
        <div className="mb-1 text-[11px] uppercase tracking-wide text-slate-500">Why</div>
        <ul className="space-y-1.5">
          {signal.reasons.map((r, i) => (
            <li key={i} className="text-xs leading-snug">
              <span className="mr-1 rounded bg-slate-800 px-1 text-[10px] uppercase text-slate-400">{r.component}</span>
              <span className={r.effect > 0 ? "text-emerald-200" : r.effect < 0 ? "text-rose-200" : "text-slate-300"}>
                {r.text}
              </span>
              <div className="font-mono text-[10px] text-slate-500">← {r.input}</div>
            </li>
          ))}
        </ul>
      </div>
      <p className="mt-auto text-[10px] text-slate-500">
        Rule set {signal.rules_version} · analysis only, not a recommendation to trade.
      </p>
    </div>
  );
});
