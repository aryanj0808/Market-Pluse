-- Phase 5: richer outcomes + lookups. signal_outcomes keeps its CHECK (source <> 'synthetic').
ALTER TABLE signal_outcomes
    ADD COLUMN IF NOT EXISTS outcome          TEXT,
    ADD COLUMN IF NOT EXISTS horizon_end      TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS close_at_horizon DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS contained_1s     BOOLEAN,
    ADD COLUMN IF NOT EXISTS contained_2s     BOOLEAN;
CREATE INDEX IF NOT EXISTS signals_symbol_ts ON signals (symbol, ts DESC);
