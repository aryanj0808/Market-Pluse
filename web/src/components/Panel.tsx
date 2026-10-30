import { memo, useEffect, useState, type ReactNode } from "react";
import { freshness } from "../lib/freshness";
import { serverNowMs, usePulse } from "../lib/store";
import type { Sourced } from "../types";

/** Re-renders only itself once a second, so panels don't have to tick for their age label. */
export const FreshnessBadge = memo(function FreshnessBadge({
  section,
}: {
  section: Pick<Sourced<unknown>, "as_of" | "ttl_seconds" | "stale" | "source"> | null | undefined;
}) {
  const [, setTick] = useState(0);
  useEffect(() => {
    const id = window.setInterval(() => setTick((t) => t + 1), 1000);
    return () => window.clearInterval(id);
  }, []);
  const s = usePulse.getState();
  const f = freshness(section, serverNowMs(s), Date.now() - s.receivedAtMs);
  const style = {
    fresh: "bg-emerald-500/15 text-emerald-300",
    stale: "bg-amber-500/20 text-amber-300",
    missing: "bg-slate-700 text-slate-400",
    silent: "bg-rose-500/20 text-rose-300",
  }[f.status];
  return (
    <span
      className={`rounded px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide ${style}`}
      title={section ? `as of ${section.as_of} | ttl ${section.ttl_seconds}s | source ${section.source}` : "no data"}
    >
      {f.status}
      {f.label && f.status !== "missing" ? ` · ${f.label}` : ""}
    </span>
  );
});

export function Panel({
  title,
  section,
  right,
  children,
  className = "",
}: {
  title: string;
  section?: Pick<Sourced<unknown>, "as_of" | "ttl_seconds" | "stale" | "source"> | null;
  right?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`flex min-h-0 flex-col rounded-lg border border-slate-800 bg-slate-900/60 ${className}`}>
      <header className="flex items-center gap-2 border-b border-slate-800 px-3 py-2">
        <h2 className="text-xs font-semibold uppercase tracking-wider text-slate-400">{title}</h2>
        <div className="ml-auto flex items-center gap-2">
          {right}
          {section !== undefined && <FreshnessBadge section={section} />}
        </div>
      </header>
      <div className="min-h-0 flex-1">{children}</div>
    </section>
  );
}
