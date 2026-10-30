// Mirrors the Python models the gateway serves (mp_core.snapshot.MarketSnapshot and friends).
// Every number shown in the UI comes from these payloads - the UI never computes analytics.

export interface Sourced<T> {
  value: T;
  source: string;
  as_of: string;
  ttl_seconds: number;
  stale: boolean;
}

export interface InstrumentKey {
  exchange: string;
  underlying: string;
  expiry: string | null;
  strike: number | null;
  option_type: "CE" | "PE" | null;
}

export interface Quote {
  key: InstrumentKey;
  ts: string;
  ltp: number;
  open: number;
  high: number;
  low: number;
  prev_close: number;
  volume: number;
  oi: number | null;
}

export interface Candle {
  key: InstrumentKey;
  interval: string;
  ts: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  oi: number | null;
}

export interface Band {
  lower: number;
  upper: number;
}

export interface ExpectedRange {
  spot: number;
  iv: number;
  calendar_days: number;
  sigma_points: number;
  sigma_pct: number;
  one_sigma: Band;
  two_sigma: Band;
}

export interface Level {
  name: string;
  price: number;
  kind: "support" | "resistance" | "pivot" | "reference";
  basis: string;
}

export interface StrikeIV {
  strike: number;
  call_iv: number | null;
  put_iv: number | null;
}

export interface ExpiryView {
  expiry: string;
  days_to_expiry: number;
  iv: { atm_iv: number | null; skew_25d: number | null; forward: number; strikes: StrikeIV[] };
  pcr: { pcr_oi: number | null; pcr_volume: number | null; max_pain: number; total_call_oi: number; total_put_oi: number };
}

export interface BuildupObs {
  end: string;
  price_change_pct: number;
  oi_change_pct: number;
  buildup: string;
}

export interface FuturesView {
  key: InstrumentKey;
  ltp: number;
  basis_points: number;
  oi: number | null;
  buildup_15m: BuildupObs[];
}

export interface Constituent {
  key: InstrumentKey;
  name: string;
  weight: number;
  ltp: number;
  prev_close: number;
}

export interface Contribution {
  id: string;
  title: string;
  published_at: string;
  contribution: number;
}

export interface NewsScore {
  score: number;
  label: string;
  n_used: number;
  n_items_seen: number;
  n_unclassified: number;
  top: Contribution[];
}

export interface Reason {
  component: string;
  input: string;
  value: number | string | null;
  effect: number;
  text: string;
}

export interface ComponentScore {
  name: string;
  score: number;
  weight: number;
  available: boolean;
  reasons: Reason[];
}

export interface Zone {
  low: number;
  high: number;
  basis: string[];
}

export interface Signal {
  signal_id: string;
  ts: string;
  source: string;
  status: "active" | "no_signal";
  bias: "bullish" | "bearish" | "neutral";
  composite: number;
  confidence: number;
  horizon: string;
  spot: number;
  sigma_points: number;
  components: ComponentScore[];
  buy_zone: Zone | null;
  sell_zone: Zone | null;
  entry: number | null;
  stop_loss: number | null;
  invalidation: number | null;
  targets: { price: number; basis: string }[];
  reward_risk: number | null;
  sizing: { lots: number; lot_size: number; loss_at_stop: number; max_loss: number; instrument: string } | null;
  reasons: Reason[];
  no_signal_reasons: string[];
  rules_version: string;
}

export interface Snapshot {
  symbol: string;
  version: number;
  computed_at: string;
  compute_ms: number;
  source: string;
  stale_sections: string[];
  quote: Sourced<Quote>;
  future: Sourced<FuturesView> | null;
  expected_range: Sourced<Record<string, ExpectedRange>> | null;
  expiries: Sourced<ExpiryView>[];
  levels: Sourced<Level[]> | null;
  constituents: Sourced<Constituent[]> | null;
  macro: Record<string, Sourced<number> | null> | null;
  news: Sourced<NewsScore> | null;
  signal: Signal | null;
  errors: string[];
}

export interface OptionQuote {
  ltp: number;
  bid: number;
  ask: number;
  volume: number;
  oi: number;
  oi_change: number;
}

export interface OptionChain {
  expiry: string;
  ts: string;
  spot: number;
  rows: { strike: number; call: OptionQuote | null; put: OptionQuote | null }[];
}

export interface NewsRow {
  id: string;
  published_at: string;
  feed: string;
  title: string;
  url: string;
  sentiment: number;
  impact: "low" | "med" | "high";
  horizon: string;
  entities: string[];
  relevance: number;
  reason: string;
  model: string | null;
  source: string;
}

export type StreamMessage =
  | { type: "snapshot"; now: string; version: number; snapshot: Snapshot }
  | {
      type: "diff";
      now: string;
      version: number;
      changed: Partial<Snapshot>;
      removed: string[];
      engine_wall: number | null;
      tick_wall?: number | null;
      gateway_wall: number;
    }
  | { type: "chain"; now: string; expiry: string; chain: Sourced<OptionChain> }
  | { type: "news"; now: string; version: number };
