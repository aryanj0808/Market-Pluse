// Incremental 1-minute bars for the chart: history from /api/candles, then quote ticks folded
// into the forming bar and pushed with series.update (never setData on every tick).

export interface Bar {
  time: number; // bar open, unix seconds, shifted to IST for display (the chart renders UTC)
  open: number;
  high: number;
  low: number;
  close: number;
}

export const IST_OFFSET_S = 5.5 * 3600;

export function barTime(isoTs: string): number {
  const s = Math.floor(Date.parse(isoTs) / 1000);
  return s - (s % 60) + IST_OFFSET_S;
}

/** Fold a tick into the forming bar: same minute -> extend it, new minute -> start a new bar.
 *  Ticks older than the forming bar are ignored (the chart only moves forward). */
export function foldTick(prev: Bar | null, ltp: number, isoTs: string): Bar | null {
  const t = barTime(isoTs);
  if (prev && t < prev.time) return null;
  if (prev && t === prev.time)
    return { ...prev, high: Math.max(prev.high, ltp), low: Math.min(prev.low, ltp), close: ltp };
  return { time: t, open: prev ? prev.close : ltp, high: ltp, low: ltp, close: ltp };
}
