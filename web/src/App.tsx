import { useEffect, useState } from "react";
import { useShallow } from "zustand/react/shallow";
import { AccuracyPage } from "./components/AccuracyPage";
import { AskPanel } from "./components/AskPanel";
import { ChainHeatmap } from "./components/ChainHeatmap";
import { ConstituentsHeatmap } from "./components/ConstituentsHeatmap";
import { NewsFeed } from "./components/NewsFeed";
import { FreshnessBadge, Panel } from "./components/Panel";
import { PriceChart } from "./components/PriceChart";
import { SignalCard } from "./components/SignalCard";
import { istDateTime, pct, px, upDown } from "./lib/format";
import { markFirstMeaningfulPaint } from "./lib/perf";
import { usePulse } from "./lib/store";
import { useStream } from "./lib/stream";

const SYMBOLS = ["NSE:BANKNIFTY", "NSE:NIFTY", "NSE:FINNIFTY"];
const NO_ERRORS: string[] = [];

function Header({ tab, setTab, onAsk }: { tab: string; setTab: (t: string) => void; onAsk: () => void }) {
  const { symbol, setSymbol, quote, source, connection, serverNow, latencyMs, stale, mode } = usePulse(
    useShallow((s) => ({
      symbol: s.symbol,
      setSymbol: s.setSymbol,
      quote: s.snapshot?.quote ?? null,
      source: s.snapshot?.source ?? null,
      connection: s.connection,
      serverNow: s.serverNow,
      latencyMs: s.latencyMs,
      stale: s.snapshot?.stale_sections.length ?? 0,
      mode: s.snapshot?.quote.source,
    })),
  );
  const q = quote?.value;
  const change = q ? ((q.ltp - q.prev_close) / q.prev_close) * 100 : null;
  return (
    <header className="flex flex-wrap items-center gap-4 border-b border-slate-800 px-4 py-2">
      <span className="font-semibold tracking-wide text-sky-300">Market Pulse</span>
      <select className="rounded bg-slate-800 px-2 py-1 text-sm" value={symbol} onChange={(e) => setSymbol(e.target.value)}>
        {SYMBOLS.map((s) => (
          <option key={s}>{s}</option>
        ))}
      </select>
      {q && (
        <span className="tabular flex items-baseline gap-2">
          <span className="text-xl font-semibold">{px(q.ltp)}</span>
          <span className={upDown(change ?? 0)}>{pct(change)}</span>
          <FreshnessBadge section={quote} />
        </span>
      )}
      <nav className="ml-4 flex gap-1 text-sm">
        {["dashboard", "accuracy"].map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`rounded px-2 py-1 capitalize ${tab === t ? "bg-slate-700 text-white" : "text-slate-400 hover:text-slate-200"}`}
          >
            {t}
          </button>
        ))}
      </nav>
      <button onClick={onAsk} className="rounded bg-sky-700 px-2 py-1 text-sm text-white hover:bg-sky-600">
        Ask Claude
      </button>
      <div className="ml-auto flex items-center gap-3 text-xs text-slate-400">
        {serverNow && <span title={mode === "synthetic" ? "replay clock" : "server clock"}>{istDateTime(serverNow)} IST</span>}
        {stale > 0 && <span className="text-amber-300">{stale} stale section(s)</span>}
        {latencyMs !== null && (
          <span title="tick arrival at ingest → painted in this browser" className={latencyMs > 300 ? "text-amber-300" : ""}>
            tick→screen {Math.round(latencyMs)} ms
          </span>
        )}
        <span className="flex items-center gap-1">
          <span
            className={`inline-block h-2 w-2 rounded-full ${
              connection === "open" ? "bg-emerald-400" : connection === "connecting" ? "bg-amber-400" : "bg-rose-500"
            }`}
          />
          {connection}
        </span>
      </div>
      {source === "synthetic" && (
        <div className="w-full rounded bg-amber-500/15 px-3 py-1 text-xs text-amber-200">
          SYNTHETIC DATA — generated for development. Prices, OI, news and signals are not real; no statistics are computed from them.
        </div>
      )}
    </header>
  );
}

function Dashboard() {
  const symbol = usePulse((s) => s.symbol);
  const snapshotReady = usePulse((s) => s.snapshot !== null);
  const signal = usePulse((s) => s.snapshot?.signal ?? null);
  const levels = usePulse((s) => s.snapshot?.levels ?? null);
  const expected = usePulse((s) => s.snapshot?.expected_range ?? null);
  const chain = usePulse((s) => s.chain);
  const front = usePulse((s) => s.snapshot?.expiries[0] ?? null);
  const constituents = usePulse((s) => s.snapshot?.constituents ?? null);
  const news = usePulse((s) => s.snapshot?.news ?? null);
  const newsVersion = usePulse((s) => s.newsVersion);
  // selectors must return stable values: a fresh `[]` on every call makes useSyncExternalStore
  // re-render forever (this crashed the page before the first snapshot arrived)
  const errors = usePulse((s) => s.snapshot?.errors ?? NO_ERRORS);
  const connection = usePulse((s) => s.connection);

  // first meaningful paint: the frame after the dashboard first renders with data
  useEffect(() => {
    if (snapshotReady) requestAnimationFrame(markFirstMeaningfulPaint);
  }, [snapshotReady]);

  if (!snapshotReady)
    return (
      <div className="p-6 text-slate-400">
        {connection === "open" ? `No snapshot for ${symbol} yet - is the engine running for it?` : "Connecting to the gateway…"}
      </div>
    );
  const er = expected?.value.session ?? expected?.value.next_session;
  return (
    <main className="grid min-h-0 flex-1 grid-cols-1 gap-3 p-3 lg:grid-cols-3 lg:grid-rows-[minmax(380px,1.2fr)_minmax(300px,1fr)]">
      <Panel
        title="Price · levels · expected range"
        section={levels}
        className="lg:col-span-2"
        right={
          er && (
            <span className="tabular text-xs text-slate-400">
              1σ ±{er.sigma_points.toFixed(0)} pts ({er.sigma_pct.toFixed(2)}%) · IV {(er.iv * 100).toFixed(1)}%
            </span>
          )
        }
      >
        <PriceChart symbol={symbol} />
      </Panel>
      <Panel title="Signal" section={expected} className="lg:row-span-2">
        <SignalCard signal={signal} />
      </Panel>
      <Panel title={`Option chain${chain ? " · " + chain.value.expiry : ""}`} section={chain}>
        <ChainHeatmap chain={chain?.value ?? null} expiry={front?.value ?? null} />
      </Panel>
      <div className="grid min-h-0 grid-rows-2 gap-3">
        <Panel
          title="News"
          section={news}
          right={news && <span className={`tabular text-xs ${upDown(news.value.score)}`}>{news.value.score.toFixed(2)} {news.value.label}</span>}
        >
          <NewsFeed symbol={symbol} version={newsVersion} />
        </Panel>
        <Panel title="Constituents" section={constituents}>
          <ConstituentsHeatmap items={constituents?.value ?? null} />
        </Panel>
      </div>
      {errors.length > 0 && (
        <div className="text-xs text-amber-300 lg:col-span-3">Missing inputs: {errors.join(" · ")}</div>
      )}
    </main>
  );
}

export default function App() {
  const symbol = usePulse((s) => s.symbol);
  const [tab, setTab] = useState("dashboard");
  const [askOpen, setAskOpen] = useState(false);
  useStream(symbol);
  return (
    <div className="flex h-full flex-col">
      <Header tab={tab} setTab={setTab} onAsk={() => setAskOpen(true)} />
      {tab === "dashboard" ? <Dashboard /> : <AccuracyPage symbol={symbol} />}
      {askOpen && <AskPanel symbol={symbol} onClose={() => setAskOpen(false)} />}
    </div>
  );
}
