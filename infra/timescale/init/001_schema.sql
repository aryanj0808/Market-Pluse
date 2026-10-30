-- Market Pulse history schema. Every row carries `source`; synthetic rows are never
-- mixed into evaluation (enforced in code and by the CHECK on signal_outcomes).

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- Raw ticks we record from a live feed (Phase 3 onwards).
CREATE TABLE ticks (
    ts          TIMESTAMPTZ NOT NULL,
    instrument  TEXT        NOT NULL,   -- InstrumentKey.id, e.g. NSE:BANKNIFTY:2026-11-24:52000:CE
    ltp         DOUBLE PRECISION NOT NULL CHECK (ltp > 0),
    volume      BIGINT,
    oi          BIGINT,
    source      TEXT        NOT NULL
);
SELECT create_hypertable('ticks', by_range('ts', INTERVAL '1 day'));
CREATE INDEX ON ticks (instrument, ts DESC);

CREATE TABLE candles (
    ts          TIMESTAMPTZ NOT NULL,   -- bar open
    instrument  TEXT        NOT NULL,
    interval    TEXT        NOT NULL,
    open        DOUBLE PRECISION NOT NULL,
    high        DOUBLE PRECISION NOT NULL,
    low         DOUBLE PRECISION NOT NULL,
    close       DOUBLE PRECISION NOT NULL,
    volume      BIGINT      NOT NULL DEFAULT 0,
    oi          BIGINT,
    source      TEXT        NOT NULL,
    PRIMARY KEY (instrument, interval, ts, source),
    CHECK (high >= GREATEST(open, close) AND low <= LEAST(open, close))
);
SELECT create_hypertable('candles', by_range('ts', INTERVAL '7 days'));

-- Option chain snapshots, one row per contract per snapshot.
CREATE TABLE option_quotes (
    ts          TIMESTAMPTZ NOT NULL,
    underlying  TEXT        NOT NULL,   -- NSE:BANKNIFTY
    expiry      DATE        NOT NULL,
    strike      DOUBLE PRECISION NOT NULL,
    option_type CHAR(2)     NOT NULL CHECK (option_type IN ('CE', 'PE')),
    spot        DOUBLE PRECISION NOT NULL,
    ltp         DOUBLE PRECISION NOT NULL,
    bid         DOUBLE PRECISION,
    ask         DOUBLE PRECISION,
    volume      BIGINT,
    oi          BIGINT,
    oi_change   BIGINT,
    iv          DOUBLE PRECISION,       -- filled by the engine (Phase 3), not the feed
    source      TEXT        NOT NULL
);
SELECT create_hypertable('option_quotes', by_range('ts', INTERVAL '1 day'));
CREATE INDEX ON option_quotes (underlying, expiry, ts DESC);

-- NSE F&O bhavcopy (end-of-day, real history).
CREATE TABLE fo_eod (
    trade_date       DATE   NOT NULL,
    instrument       TEXT   NOT NULL,
    underlying       TEXT   NOT NULL,
    expiry           DATE   NOT NULL,
    strike           DOUBLE PRECISION,
    option_type      CHAR(2),
    open             DOUBLE PRECISION, high DOUBLE PRECISION,
    low              DOUBLE PRECISION, close DOUBLE PRECISION,
    settle           DOUBLE PRECISION,
    underlying_price DOUBLE PRECISION,
    oi               BIGINT NOT NULL,
    oi_change        BIGINT NOT NULL,
    volume           BIGINT NOT NULL,
    volume_unit      TEXT   NOT NULL CHECK (volume_unit IN ('contracts', 'units')),
    lot_size         INT,
    source           TEXT   NOT NULL DEFAULT 'nse_bhavcopy',
    PRIMARY KEY (instrument, trade_date)
);
CREATE INDEX ON fo_eod (underlying, trade_date);

-- Every signal the engine emits, with the full inputs that produced it (Phase 5).
CREATE TABLE signals (
    ts          TIMESTAMPTZ NOT NULL,
    signal_id   UUID        NOT NULL,
    symbol      TEXT        NOT NULL,
    bias        TEXT        NOT NULL,
    payload     JSONB       NOT NULL,   -- zones, stop, targets, confidence, reasons[]
    inputs      JSONB       NOT NULL,   -- snapshot of every input, with source/as_of
    source      TEXT        NOT NULL,   -- 'synthetic' if any input was synthetic
    PRIMARY KEY (signal_id, ts)
);
SELECT create_hypertable('signals', by_range('ts', INTERVAL '30 days'));

-- Nightly scoring results. Synthetic signals are never scored.
CREATE TABLE signal_outcomes (
    signal_id   UUID        NOT NULL,
    scored_at   TIMESTAMPTZ NOT NULL,
    horizon     TEXT        NOT NULL,
    hit         BOOLEAN,
    mfe         DOUBLE PRECISION,
    mae         DOUBLE PRECISION,
    source      TEXT        NOT NULL CHECK (source <> 'synthetic'),
    PRIMARY KEY (signal_id, horizon)
);
