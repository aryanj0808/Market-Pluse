import { useQuery } from "@tanstack/react-query";
import {
  ColorType,
  LineStyle,
  createChart,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type UTCTimestamp,
} from "lightweight-charts";
import { memo, useEffect, useRef } from "react";
import { api } from "../lib/api";
import { barTime, foldTick, type Bar } from "../lib/bars";
import { usePulse, type PulseState } from "../lib/store";

const LEVEL_COLOR: Record<string, string> = {
  support: "#34d399",
  resistance: "#fb7185",
  pivot: "#a78bfa",
  reference: "#94a3b8",
};

/** Candles + key levels + expected-range bands + signal lines. History is loaded once; ticks
 *  are folded into the forming bar with series.update - setData is never called per tick. */
export const PriceChart = memo(function PriceChart({ symbol }: { symbol: string }) {
  const box = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const series = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const lastBar = useRef<Bar | null>(null);
  const lines = useRef<IPriceLine[]>([]);
  const history = useQuery({ queryKey: ["candles", symbol], queryFn: () => api.candles(symbol), staleTime: Infinity });

  // create the chart once
  useEffect(() => {
    if (!box.current) return;
    const c = createChart(box.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: "transparent" }, textColor: "#94a3b8", fontSize: 11 },
      grid: { vertLines: { color: "#1e293b" }, horzLines: { color: "#1e293b" } },
      timeScale: { timeVisible: true, secondsVisible: false, borderColor: "#334155" },
      rightPriceScale: { borderColor: "#334155" },
      crosshair: { mode: 0 },
    });
    chart.current = c;
    series.current = c.addCandlestickSeries({
      upColor: "#10b981",
      downColor: "#f43f5e",
      borderVisible: false,
      wickUpColor: "#10b981",
      wickDownColor: "#f43f5e",
    });
    return () => {
      c.remove();
      chart.current = null;
      series.current = null;
      lastBar.current = null;
      lines.current = [];
    };
  }, [symbol]);

  // history (once per symbol)
  useEffect(() => {
    const s = series.current;
    if (!s || !history.data) return;
    const bars: Bar[] = history.data.value.map((c) => ({
      time: barTime(c.ts),
      open: c.open,
      high: c.high,
      low: c.low,
      close: c.close,
    }));
    s.setData(bars.map((b) => ({ ...b, time: b.time as UTCTimestamp })));
    lastBar.current = bars.at(-1) ?? null;
    chart.current?.timeScale().fitContent();
  }, [history.data]);

  // live ticks: subscribe outside React rendering, update only the forming bar
  useEffect(
    () =>
      usePulse.subscribe((state, prev) => {
        const q = state.snapshot?.quote;
        if (!q || q === prev.snapshot?.quote || !series.current) return;
        const next = foldTick(lastBar.current, q.value.ltp, q.value.ts);
        if (!next) return;
        lastBar.current = next;
        series.current.update({ ...next, time: next.time as UTCTimestamp });
      }),
    [symbol],
  );

  // levels, expected-range bands and signal lines: drawn from the current state when the chart
  // mounts (it mounts after the first snapshot has arrived), then redrawn only when they change
  useEffect(() => {
    const draw = (state: PulseState, prev: PulseState | null) => {
        const snap = state.snapshot;
        const s = series.current;
        if (!snap || !s) return;
        const p = prev?.snapshot;
        if (p && p.levels === snap.levels && p.expected_range === snap.expected_range && p.signal === snap.signal)
          return;
        for (const l of lines.current) s.removePriceLine(l);
        lines.current = [];
        const add = (price: number, color: string, title: string, style = LineStyle.Dotted, width: 1 | 2 = 1) =>
          lines.current.push(s.createPriceLine({ price, color, title, lineStyle: style, lineWidth: width, axisLabelVisible: true }));
        const spot = snap.quote.value.ltp;
        const er = snap.expected_range?.value.session ?? snap.expected_range?.value.next_session;
        const sigma = er?.sigma_points ?? spot * 0.01;
        for (const lv of snap.levels?.value ?? []) {
          if (Math.abs(lv.price - spot) <= 2 * sigma) add(lv.price, LEVEL_COLOR[lv.kind] ?? "#94a3b8", lv.name);
        }
        if (er) {
          add(er.one_sigma.upper, "#38bdf8", "+1σ", LineStyle.Dashed);
          add(er.one_sigma.lower, "#38bdf8", "-1σ", LineStyle.Dashed);
          add(er.two_sigma.upper, "#0ea5e9", "+2σ", LineStyle.LargeDashed);
          add(er.two_sigma.lower, "#0ea5e9", "-2σ", LineStyle.LargeDashed);
        }
        const sig = snap.signal;
        if (sig?.status === "active" && sig.entry !== null && sig.stop_loss !== null) {
          add(sig.entry, "#fbbf24", "entry", LineStyle.Solid, 2);
          add(sig.stop_loss, "#f43f5e", "stop", LineStyle.Solid, 2);
          sig.targets.forEach((t, i) => add(t.price, "#10b981", `T${i + 1}`, LineStyle.Solid, 2));
        }
    };
    draw(usePulse.getState(), null);
    return usePulse.subscribe((state, prev) => draw(state, prev));
  }, [symbol]);

  return (
    <div className="relative h-full min-h-[320px]">
      <div ref={box} className="absolute inset-0" />
      {history.isError && (
        <div className="absolute left-3 top-2 text-xs text-amber-300">history unavailable - showing live ticks only</div>
      )}
    </div>
  );
});
