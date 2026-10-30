-- Re-recording ticks (e.g. a replay or ingest restart) must not duplicate rows. Two genuinely
-- different ticks at the same timestamp differ in ltp or volume and are both kept.
DELETE FROM ticks a USING ticks b
 WHERE a.ctid < b.ctid AND a.ts = b.ts AND a.instrument = b.instrument AND a.source = b.source
   AND a.ltp = b.ltp AND a.volume IS NOT DISTINCT FROM b.volume;
CREATE UNIQUE INDEX IF NOT EXISTS ticks_uniq
    ON ticks (instrument, ts, source, ltp, volume) NULLS NOT DISTINCT;
