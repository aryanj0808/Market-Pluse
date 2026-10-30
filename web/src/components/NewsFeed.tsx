import { useQuery } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import { memo, useRef } from "react";
import { api } from "../lib/api";
import { istDateTime, signed } from "../lib/format";
import type { NewsRow } from "../types";

const IMPACT = {
  high: "bg-rose-600/80 text-white",
  med: "bg-amber-500/80 text-slate-950",
  low: "bg-slate-700 text-slate-300",
};

/** Feeds are untrusted: only plain web links become clickable (never javascript: or data:). */
const isWebLink = (url: string) => /^https?:\/\//i.test(url);

const Item = memo(function Item({ n }: { n: NewsRow }) {
  const synthetic = n.source === "synthetic";
  return (
    <div className="border-b border-slate-800/80 px-3 py-2">
      <div className="flex items-center gap-2 text-[10px] text-slate-500">
        <span className={`rounded px-1.5 py-px font-semibold uppercase ${IMPACT[n.impact]}`}>{n.impact}</span>
        <span className={n.sentiment > 0 ? "text-emerald-400" : n.sentiment < 0 ? "text-rose-400" : ""}>
          sentiment {signed(n.sentiment, 1)}
        </span>
        <span>relevance {(n.relevance * 100).toFixed(0)}%</span>
        <span className="ml-auto">{istDateTime(n.published_at)}</span>
      </div>
      <div className="mt-0.5 text-[13px] leading-snug text-slate-100">
        {synthetic || !isWebLink(n.url) ? n.title : (
          <a href={n.url} target="_blank" rel="noreferrer noopener" className="hover:underline">
            {n.title}
          </a>
        )}
      </div>
      <div className="mt-0.5 text-[11px] text-slate-400">{n.reason}</div>
      <div className="mt-1 flex flex-wrap gap-1 text-[10px]">
        {n.entities.map((e) => (
          <span key={e} className="rounded bg-slate-800 px-1 font-mono text-slate-400">{e}</span>
        ))}
        <span className="ml-auto text-slate-600">{n.feed}</span>
        {n.model === "fixture-labels" && (
          <span className="rounded bg-amber-500/20 px-1 text-amber-300" title="Synthetic test label, not an LLM classification">
            test label
          </span>
        )}
      </div>
    </div>
  );
});

/** News feed, refetched only when the stream says the news cache changed; virtualised. */
export const NewsFeed = memo(function NewsFeed({ symbol, version }: { symbol: string; version: number }) {
  const q = useQuery({ queryKey: ["news", symbol, version], queryFn: () => api.news(symbol), placeholderData: (p) => p });
  const items = q.data?.items ?? [];
  const scroller = useRef<HTMLDivElement>(null);
  const v = useVirtualizer({ count: items.length, getScrollElement: () => scroller.current, estimateSize: () => 92, overscan: 6 });
  const broken = q.data?.feeds.filter((f) => f.last_error) ?? [];
  return (
    <div className="flex h-full flex-col">
      {broken.length > 0 && (
        <div className="border-b border-slate-800 px-3 py-1 text-[11px] text-amber-300">
          {broken.length} feed(s) failing: {broken.map((f) => f.feed).join(", ")}
        </div>
      )}
      {(q.data?.unclassified ?? 0) > 0 && (
        <div className="border-b border-slate-800 px-3 py-1 text-[11px] text-slate-400">
          {q.data?.unclassified} item(s) waiting for classification (not scored)
        </div>
      )}
      <div ref={scroller} className="min-h-0 flex-1 overflow-y-auto">
        {q.isError && <div className="p-3 text-sm text-amber-300">News unavailable: {(q.error as Error).message}</div>}
        {!q.isError && items.length === 0 && <div className="p-3 text-sm text-slate-500">No relevant news in the window.</div>}
        <div style={{ height: v.getTotalSize(), position: "relative" }}>
          {v.getVirtualItems().map((it) => (
            <div
              key={items[it.index].id}
              data-index={it.index}
              ref={v.measureElement}
              className="absolute left-0 right-0"
              style={{ transform: `translateY(${it.start}px)` }}
            >
              <Item n={items[it.index]} />
            </div>
          ))}
        </div>
      </div>
    </div>
  );
});
