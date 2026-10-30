import { useRef, useState } from "react";
import { istDateTime } from "../lib/format";
import { readSSE } from "../lib/sse";

const SUGGESTIONS = [
  "What's the setup for the next session, and why?",
  "Why is there no signal right now?",
  "Which levels matter most today?",
  "How is the news affecting the index?",
];

interface Meta {
  source: string;
  as_of: string;
  stale: boolean;
  stale_sections: string[];
  model: string;
}

/** Ask-Claude side panel. Runs on its own HTTP request with local state only, so a slow answer
 *  never touches the stream or the other panels. */
export function AskPanel({ symbol, onClose }: { symbol: string; onClose: () => void }) {
  const [question, setQuestion] = useState("");
  const [asked, setAsked] = useState("");
  const [answer, setAnswer] = useState("");
  const [meta, setMeta] = useState<Meta | null>(null);
  const [unverified, setUnverified] = useState<string[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const abort = useRef<AbortController | null>(null);

  async function ask(q: string) {
    if (!q.trim() || busy) return;
    abort.current?.abort();
    const ctl = new AbortController();
    abort.current = ctl;
    setAsked(q);
    setAnswer("");
    setMeta(null);
    setUnverified(null);
    setError(null);
    setBusy(true);
    try {
      const r = await fetch(`/api/ask/${encodeURIComponent(symbol)}`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
        body: JSON.stringify({ question: q }),
        signal: ctl.signal,
      });
      if (!r.ok) {
        let detail = r.statusText;
        try {
          detail = (await r.json()).detail ?? detail;
        } catch {
          /* not JSON */
        }
        throw new Error(typeof detail === "string" ? detail : `HTTP ${r.status}`);
      }
      for await (const ev of readSSE(r, ctl.signal)) {
        const d = ev.data as Record<string, unknown>;
        if (ev.event === "meta") setMeta(d as unknown as Meta);
        else if (ev.event === "delta") setAnswer((a) => a + (d.text as string));
        else if (ev.event === "done") setUnverified((d.unverified_numbers as string[]) ?? []);
        else if (ev.event === "error") setError(d.message as string);
      }
    } catch (e) {
      if ((e as Error).name !== "AbortError") setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <aside className="fixed inset-y-0 right-0 z-20 flex w-full max-w-md flex-col border-l border-slate-800 bg-slate-950 shadow-2xl">
      <header className="flex items-center gap-2 border-b border-slate-800 px-4 py-3">
        <h2 className="font-semibold">Ask Claude</h2>
        <span className="text-xs text-slate-500">{symbol}</span>
        <button onClick={onClose} className="ml-auto rounded px-2 text-slate-400 hover:bg-slate-800" aria-label="Close">
          ✕
        </button>
      </header>

      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-4 text-sm">
        {!asked && (
          <div className="space-y-2">
            <p className="text-slate-400">
              Answers use only the current market snapshot. Every number is checked against that data afterwards.
            </p>
            {SUGGESTIONS.map((s) => (
              <button
                key={s}
                onClick={() => {
                  setQuestion(s);
                  void ask(s);
                }}
                className="block w-full rounded border border-slate-800 px-3 py-2 text-left text-slate-300 hover:border-sky-700"
              >
                {s}
              </button>
            ))}
          </div>
        )}
        {asked && <div className="rounded bg-slate-800/70 px-3 py-2 text-slate-200">{asked}</div>}
        {meta && (
          <div className="text-[11px] text-slate-500">
            data as of {istDateTime(meta.as_of)} IST · {meta.model}
            {meta.source === "synthetic" && <span className="ml-1 text-amber-300">· synthetic data</span>}
            {meta.stale && <span className="ml-1 text-amber-300">· some inputs stale ({meta.stale_sections.join(", ")})</span>}
          </div>
        )}
        {(answer || busy) && (
          <div className="whitespace-pre-wrap leading-relaxed text-slate-100">
            {answer}
            {busy && <span className="ml-0.5 inline-block h-4 w-1.5 animate-pulse bg-sky-400 align-middle" />}
          </div>
        )}
        {unverified && unverified.length === 0 && (
          <div className="text-[11px] text-emerald-400">✓ every number in this answer was found in the data</div>
        )}
        {unverified && unverified.length > 0 && (
          <div className="rounded border border-amber-700 bg-amber-950/40 px-3 py-2 text-xs text-amber-200">
            {unverified.length} number(s) not found in the market data - treat with caution: {unverified.join(", ")}
          </div>
        )}
        {error && <div className="rounded border border-rose-800 bg-rose-950/40 px-3 py-2 text-xs text-rose-200">{error}</div>}
      </div>

      <form
        className="border-t border-slate-800 p-3"
        onSubmit={(e) => {
          e.preventDefault();
          void ask(question);
        }}
      >
        <textarea
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              void ask(question);
            }
          }}
          maxLength={1000}
          rows={2}
          placeholder="Ask about the current setup…"
          className="w-full resize-none rounded border border-slate-700 bg-slate-900 px-3 py-2 text-sm outline-none focus:border-sky-600"
        />
        <div className="mt-2 flex items-center gap-2">
          <span className="text-[10px] text-slate-500">Analysis only - not investment advice.</span>
          {busy ? (
            <button type="button" onClick={() => abort.current?.abort()} className="ml-auto rounded bg-slate-700 px-3 py-1 text-sm">
              Stop
            </button>
          ) : (
            <button type="submit" disabled={!question.trim()} className="ml-auto rounded bg-sky-600 px-3 py-1 text-sm disabled:opacity-40">
              Ask
            </button>
          )}
        </div>
      </form>
    </aside>
  );
}
