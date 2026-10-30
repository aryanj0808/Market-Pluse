import type { Candle, NewsRow, Sourced } from "../types";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function get<T>(path: string): Promise<T> {
  const r = await fetch(path, { headers: { Accept: "application/json" } });
  if (!r.ok) {
    let detail = r.statusText;
    try {
      detail = (await r.json()).detail ?? detail;
    } catch {
      /* not JSON */
    }
    throw new ApiError(r.status, detail);
  }
  return (await r.json()) as T;
}

export const api = {
  candles: (symbol: string) => get<Sourced<Candle[]>>(`/api/candles/${encodeURIComponent(symbol)}`),
  news: (symbol: string, hours = 48) =>
    get<{ now: string; items: NewsRow[]; unclassified: number; feeds: { feed: string; last_error: string | null }[] }>(
      `/api/news/${encodeURIComponent(symbol)}?hours=${hours}`,
    ),
  stats: (symbol: string, days: number) => get<Stats>(`/api/stats/${encodeURIComponent(symbol)}?days=${days}`),
};

export interface Stats {
  n_scored: number;
  n_excluded_synthetic: number;
  n_active: number;
  n_filled: number;
  n_target: number;
  n_stop: number;
  n_open: number;
  hit_rate: number | null;
  fill_rate: number | null;
  avg_mfe: number | null;
  avg_mae: number | null;
  containment_1s: number | null;
  containment_2s: number | null;
  message: string;
}
