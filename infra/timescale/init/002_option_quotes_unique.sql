-- Re-recording the same chain snapshot (e.g. after an ingest restart) must be a no-op.
CREATE UNIQUE INDEX IF NOT EXISTS option_quotes_uniq
    ON option_quotes (underlying, expiry, strike, option_type, ts, source);
