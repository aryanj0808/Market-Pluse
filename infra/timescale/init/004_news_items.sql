-- Every news item seen, with its classification. An item that is `classified` or `dropped` is final:
-- the recorder only ever upgrades `unclassified` rows, so an item is never sent to the model twice
-- (even after the Redis cache is flushed).
CREATE TABLE IF NOT EXISTS news_items (
    id             TEXT PRIMARY KEY,           -- sha1(canonical url)[:16]
    published_at   TIMESTAMPTZ NOT NULL,
    feed           TEXT NOT NULL,
    title          TEXT NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('classified', 'dropped', 'unclassified', 'duplicate')),
    model          TEXT,
    prompt_version TEXT,
    source         TEXT NOT NULL,
    doc            JSONB NOT NULL               -- the full ScoredNewsItem
);
CREATE INDEX IF NOT EXISTS news_items_published ON news_items (published_at DESC);
