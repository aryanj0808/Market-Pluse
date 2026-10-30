"""TimescaleDB history: the recorder (ticks, 1m candles, option-chain snapshots) and readers.

Every row carries `source`, so recorded synthetic data can never be mistaken for real history.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from datetime import date, datetime

from psycopg.types.json import Jsonb

from mp_core.candles import resample
from mp_core.loaders.bhavcopy import FoEodRecord
from mp_core.models import Candle, InstrumentKey, Interval, OptionChain, Source, Tick
from mp_core.news.models import ScoredNewsItem
from mp_core.signals.evaluation import Outcome
from mp_core.signals.models import Signal


class HistoryUnavailable(RuntimeError):
    """The database is down (or in reconnect backoff). Callers treat recording as best-effort."""


class History:
    """Timescale access with automatic reconnects. While the database is unreachable, calls fail fast
    with HistoryUnavailable (exponential backoff, 1 s -> 60 s) instead of hanging the caller."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._conn = None
        self._next_retry = 0.0
        self._backoff = 1.0

    async def connect(self) -> History:
        await self._ensure()
        return self

    async def _ensure(self):
        c = self._conn
        if c is not None and not c.closed and not c.broken:
            return c
        now = time.monotonic()
        if now < self._next_retry:
            raise HistoryUnavailable(f"database unavailable; next reconnect in {self._next_retry - now:.0f}s")
        import psycopg

        try:
            self._conn = await psycopg.AsyncConnection.connect(self.dsn, autocommit=True, connect_timeout=5)
        except psycopg.Error as ex:
            # measured from the failure, not the attempt: a connect can take seconds to time out
            self._next_retry = time.monotonic() + self._backoff
            self._backoff = min(60.0, self._backoff * 2)
            raise HistoryUnavailable(f"cannot connect to the database: {type(ex).__name__}") from None
        self._backoff = 1.0
        return self._conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()

    @property
    def healthy(self) -> bool:
        return self._conn is not None and not self._conn.closed and not self._conn.broken

    # ------------------------------------------------------------------ recorder
    async def record_ticks(self, ticks: Sequence[Tick], source: Source) -> int:
        if not ticks:
            return 0
        async with (await self._ensure()).cursor() as cur:
            await cur.executemany(
                "INSERT INTO ticks (ts, instrument, ltp, volume, oi, source) VALUES (%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT DO NOTHING",
                [(t.ts, t.key.id, t.ltp, t.volume, t.oi, source.value) for t in ticks],
            )
        return len(ticks)

    async def record_candles(self, candles: Sequence[Candle], source: Source) -> int:
        if not candles:
            return 0
        async with (await self._ensure()).cursor() as cur:
            await cur.executemany(
                "INSERT INTO candles (ts, instrument, interval, open, high, low, close, volume, oi, source) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                [
                    (c.ts, c.key.id, c.interval.value, c.open, c.high, c.low, c.close, c.volume, c.oi, source.value)
                    for c in candles
                ],
            )
        return len(candles)

    async def record_chain(
        self, chain: OptionChain, source: Source, ivs: dict[tuple[float, str], float] | None = None
    ) -> int:
        rows = []
        for r in chain.rows:
            for q in (r.call, r.put):
                if q is None:
                    continue
                iv = (ivs or {}).get((r.strike, q.key.option_type.value))
                rows.append(
                    (
                        chain.ts,
                        chain.underlying.symbol,
                        chain.expiry,
                        r.strike,
                        q.key.option_type.value,
                        chain.spot,
                        q.ltp,
                        q.bid,
                        q.ask,
                        q.volume,
                        q.oi,
                        q.oi_change,
                        iv,
                        source.value,
                    )
                )
        async with (await self._ensure()).cursor() as cur:
            await cur.executemany(
                "INSERT INTO option_quotes (ts, underlying, expiry, strike, option_type, spot, ltp, bid, ask, volume, "
                "oi, oi_change, iv, source) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                rows,
            )
        return len(rows)

    # ------------------------------------------------------------------ readers
    async def candles(
        self, key: InstrumentKey, interval: Interval, start: datetime, end: datetime, now: datetime
    ) -> tuple[list[Candle], set[str]]:
        """Recorded 1m candles in [start, end], resampled to `interval`. Returns (candles, sources)."""
        async with (await self._ensure()).cursor() as cur:
            await cur.execute(
                "SELECT ts, open, high, low, close, volume, oi, source FROM candles "
                "WHERE instrument = %s AND interval = '1m' AND ts >= %s AND ts <= %s ORDER BY ts",
                (key.id, start, end),
            )
            rows = await cur.fetchall()
        sources = {r[7] for r in rows}
        minutes = [
            Candle(
                key=key, interval=Interval.M1, ts=r[0], open=r[1], high=r[2], low=r[3], close=r[4], volume=r[5], oi=r[6]
            )
            for r in rows
        ]
        return resample(key, interval, minutes, now), sources

    async def counts(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        async with (await self._ensure()).cursor() as cur:
            for table in ("ticks", "candles", "option_quotes"):
                await cur.execute(f"SELECT source, count(*) FROM {table} GROUP BY source")  # noqa: S608 - fixed names
                out[table] = {s: n for s, n in await cur.fetchall()}
        return out

    # ------------------------------------------------------------------ news
    async def record_news(self, item: ScoredNewsItem) -> None:
        """Insert, or upgrade an `unclassified` row. Final rows (classified/dropped) never change."""
        async with (await self._ensure()).cursor() as cur:
            await cur.execute(
                "INSERT INTO news_items (id, published_at, feed, title, status, model, prompt_version, source, doc) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status, model = EXCLUDED.model, "
                "prompt_version = EXCLUDED.prompt_version, doc = EXCLUDED.doc "
                "WHERE news_items.status = 'unclassified'",
                (
                    item.id,
                    item.item.published_at,
                    item.item.feed,
                    item.item.title,
                    item.status,
                    item.model,
                    item.prompt_version,
                    item.item.source.value,
                    Jsonb(item.model_dump(mode="json")),
                ),
            )

    async def get_news(self, item_id: str) -> ScoredNewsItem | None:
        async with (await self._ensure()).cursor() as cur:
            await cur.execute("SELECT doc FROM news_items WHERE id = %s", (item_id,))
            row = await cur.fetchone()
        return ScoredNewsItem.model_validate(row[0]) if row else None

    # ------------------------------------------------------------------ signals & evaluation
    async def record_signal(self, signal: Signal, inputs: dict) -> None:
        async with (await self._ensure()).cursor() as cur:
            await cur.execute(
                "INSERT INTO signals (ts, signal_id, symbol, bias, payload, inputs, source) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (
                    signal.ts,
                    signal.signal_id,
                    signal.symbol,
                    signal.bias,
                    Jsonb(signal.model_dump(mode="json")),
                    Jsonb(inputs),
                    signal.source.value,
                ),
            )

    async def unscored_signals(self, symbol: str, until: datetime, include_synthetic: bool = False) -> list[Signal]:
        """Signals without an outcome yet. Synthetic signals are excluded unless asked for (only so
        that callers can count them)."""
        async with (await self._ensure()).cursor() as cur:
            await cur.execute(
                "SELECT s.payload FROM signals s LEFT JOIN signal_outcomes o ON o.signal_id = s.signal_id "
                "WHERE s.symbol = %s AND s.ts <= %s AND o.signal_id IS NULL "
                + ("" if include_synthetic else "AND s.source <> 'synthetic' ")
                + "ORDER BY s.ts",
                (symbol, until),
            )
            rows = await cur.fetchall()
        return [Signal.model_validate(r[0]) for r in rows]

    async def count_synthetic_signals(self, symbol: str) -> int:
        async with (await self._ensure()).cursor() as cur:
            await cur.execute("SELECT count(*) FROM signals WHERE symbol = %s AND source = 'synthetic'", (symbol,))
            return (await cur.fetchone())[0]

    async def record_outcome(self, o: Outcome) -> None:
        async with (await self._ensure()).cursor() as cur:
            await cur.execute(
                "INSERT INTO signal_outcomes (signal_id, scored_at, horizon, hit, mfe, mae, source, outcome, "
                "horizon_end, close_at_horizon, contained_1s, contained_2s) "
                "VALUES (%s, now(), 'signal', %s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (
                    o.signal_id,
                    o.hit,
                    o.mfe,
                    o.mae,
                    o.source.value,
                    o.outcome,
                    o.horizon_end,
                    o.close_at_horizon,
                    o.contained_1s,
                    o.contained_2s,
                ),
            )

    async def outcomes(self, symbol: str, since: datetime | None = None) -> list[Outcome]:
        async with (await self._ensure()).cursor() as cur:
            await cur.execute(
                "SELECT o.signal_id, o.horizon_end, o.outcome, o.hit, o.mfe, o.mae, o.close_at_horizon, "
                "o.contained_1s, o.contained_2s, o.source FROM signal_outcomes o "
                "JOIN signals s ON s.signal_id = o.signal_id WHERE s.symbol = %s AND (%s::timestamptz IS NULL "
                "OR s.ts >= %s) ORDER BY o.horizon_end",
                (symbol, since, since),
            )
            rows = await cur.fetchall()
        keys = [
            "signal_id",
            "horizon_end",
            "outcome",
            "hit",
            "mfe",
            "mae",
            "close_at_horizon",
            "contained_1s",
            "contained_2s",
            "source",
        ]
        return [Outcome.model_validate({**dict(zip(keys, r, strict=True)), "signal_id": str(r[0])}) for r in rows]

    # ------------------------------------------------------------------ bhavcopy (fo_eod)
    async def record_fo_eod(self, records: Sequence[FoEodRecord]) -> int:
        async with (await self._ensure()).cursor() as cur:
            await cur.executemany(
                "INSERT INTO fo_eod (trade_date, instrument, underlying, expiry, strike, option_type, open, high, low, "
                "close, settle, underlying_price, oi, oi_change, volume, volume_unit, lot_size, source) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'nse_bhavcopy') ON CONFLICT DO NOTHING",
                [
                    (
                        r.trade_date,
                        r.key.id,
                        r.key.underlying,
                        r.key.expiry,
                        r.key.strike,
                        r.key.option_type.value if r.key.option_type else None,
                        r.open,
                        r.high,
                        r.low,
                        r.close,
                        r.settle,
                        r.underlying_price,
                        r.oi,
                        r.oi_change,
                        r.volume,
                        r.volume_unit,
                        r.lot_size,
                    )
                    for r in records
                ],
            )
        return len(records)

    async def fo_eod(self, underlying: str, start: date, end: date) -> list[FoEodRecord]:
        """Real bhavcopy rows only (source = nse_bhavcopy)."""
        async with (await self._ensure()).cursor() as cur:
            await cur.execute(
                "SELECT trade_date, instrument, open, high, low, close, settle, underlying_price, oi, oi_change, "
                "volume, volume_unit, lot_size FROM fo_eod WHERE underlying = %s AND trade_date BETWEEN %s AND %s "
                "AND source = 'nse_bhavcopy' ORDER BY trade_date",
                (underlying, start, end),
            )
            rows = await cur.fetchall()
        out = []
        for r in rows:
            out.append(
                FoEodRecord(
                    trade_date=r[0],
                    key=InstrumentKey.parse(r[1]),
                    open=r[2],
                    high=r[3],
                    low=r[4],
                    close=r[5],
                    settle=r[6],
                    underlying_price=r[7],
                    oi=r[8],
                    oi_change=r[9],
                    volume=r[10],
                    volume_unit=r[11],
                    lot_size=r[12],
                )
            )
        return out
