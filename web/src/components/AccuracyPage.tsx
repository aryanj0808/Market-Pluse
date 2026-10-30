import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError, api } from "../lib/api";

const rate = (x: number | null) => (x === null ? "–" : `${(x * 100).toFixed(1)}%`);

function Card({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div className="rounded-lg border border-slate-800 bg-slate-900/60 p-4">
      <div className="text-xs uppercase tracking-wide text-slate-500">{label}</div>
      <div className="tabular mt-1 text-2xl font-semibold">{value}</div>
      {note && <div className="mt-1 text-xs text-slate-500">{note}</div>}
    </div>
  );
}

/** How past signals actually did - from real data only. When there is nothing real to score it
 *  says so instead of showing numbers. */
export function AccuracyPage({ symbol }: { symbol: string }) {
  const [days, setDays] = useState(90);
  const q = useQuery({ queryKey: ["stats", symbol, days], queryFn: () => api.stats(symbol, days), refetchInterval: 60_000 });
  return (
    <div className="mx-auto max-w-5xl space-y-4 p-4">
      <div className="flex items-center gap-3">
        <h1 className="text-lg font-semibold">Signal accuracy · {symbol}</h1>
        <select
          className="ml-auto rounded bg-slate-800 px-2 py-1 text-sm"
          value={days}
          onChange={(e) => setDays(Number(e.target.value))}
        >
          {[30, 90, 180, 365].map((d) => (
            <option key={d} value={d}>
              last {d} days
            </option>
          ))}
        </select>
      </div>
      {q.isError && (
        <div className="rounded border border-amber-700 bg-amber-950/40 p-4 text-amber-200">
          {q.error instanceof ApiError && q.error.status === 503
            ? "Signal history is unavailable (the gateway has no database configured)."
            : `Could not load statistics: ${(q.error as Error).message}`}
        </div>
      )}
      {q.data && (
        <>
          <div
            className={`rounded border p-4 ${
              q.data.hit_rate === null && q.data.containment_1s === null
                ? "border-amber-700 bg-amber-950/40 text-amber-200"
                : "border-slate-800 bg-slate-900/60 text-slate-300"
            }`}
          >
            {q.data.message}
          </div>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <Card label="Hit rate" value={rate(q.data.hit_rate)} note={`${q.data.n_target} targets / ${q.data.n_stop} stops`} />
            <Card label="Fill rate" value={rate(q.data.fill_rate)} note={`${q.data.n_filled} of ${q.data.n_active} active`} />
            <Card label="Avg MFE / MAE" value={q.data.avg_mfe === null ? "–" : `${q.data.avg_mfe.toFixed(0)} / ${q.data.avg_mae?.toFixed(0)}`} note="index points" />
            <Card label="Signals scored" value={String(q.data.n_scored)} note={`${q.data.n_excluded_synthetic} synthetic excluded`} />
            <Card label="Inside 1σ range" value={rate(q.data.containment_1s)} note="≈68% if implied vol is fair" />
            <Card label="Inside 2σ range" value={rate(q.data.containment_2s)} note="≈95% if implied vol is fair" />
          </div>
          <p className="text-xs text-slate-500">
            Fills are conservative: the entry must trade, and a bar touching both stop and target counts as the stop.
            Synthetic signals are never scored.
          </p>
        </>
      )}
    </div>
  );
}
