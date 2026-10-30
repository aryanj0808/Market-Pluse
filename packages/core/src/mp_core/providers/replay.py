"""Replays a recorded or synthetic dataset through the MarketDataProvider interface.

No look-ahead: every query only sees data with timestamp <= clock.now(). Candles are
returned only once complete (bar close <= now). The provider's `source` is the
dataset's source, so synthetic data stays labelled synthetic all the way to the UI.
"""

from __future__ import annotations

import asyncio
import bisect
import functools
import math
from collections import defaultdict
from collections.abc import AsyncIterator, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path

from mp_core import market_calendar as cal
from mp_core.candles import aggregate, resample
from mp_core.clock import Clock, SimClock
from mp_core.dataset import MacroRow, iter_jsonl, read_candles, read_manifest
from mp_core.models import (
    Candle,
    Constituent,
    InstrumentKey,
    Interval,
    MacroSnapshot,
    OptionChain,
    Quote,
    Sourced,
    Tick,
    stamp,
)
from mp_core.providers.base import DataUnavailable, MarketDataProvider
from mp_core.settings import TTLs, get_settings

# Longest real-time pause between two replayed ticks (gaps between sessions are compressed to this).
MAX_GAP_SLEEP_S = 2.0

class ReplayProvider(MarketDataProvider):
    def __init__(
        self, directory: str | Path, clock: Clock, *, speed: float | None = None, ttls: TTLs | None = None
    ) -> None:
        """`speed`: playback multiplier for `stream_ticks` (60 = one market minute per
        second). None replays as fast as possible."""
        self.dir = Path(directory)
        self.clock = clock
        self.speed = speed
        self.ttl = ttls or get_settings().ttl
        self.__dict__.update(_load(self.dir.resolve(), (self.dir / "manifest.json").stat().st_mtime_ns))
        self.source = self.manifest.source
        self.ttl = _cadence_ttls(self.ttl, self.manifest.cadence_seconds)

    # ------------------------------------------------------------------ helpers
    def _now(self) -> datetime:
        return self.clock.now()

    def _stamp(self, value, as_of: datetime, ttl: float):
        return stamp(value, source=self.source, as_of=as_of, now=self._now(), ttl_seconds=ttl)

    def _last_tick(self, key: InstrumentKey) -> Tick | None:
        ts = self._tick_ts_by_key.get(key.id)
        if not ts:
            return None
        i = bisect.bisect_right(ts, self._now())
        return self._ticks_by_key[key.id][i - 1] if i else None

    def _complete_minutes(self, key: InstrumentKey, start: datetime, end: datetime) -> list[Candle]:
        ts = self._candle_ts.get(key.id, [])
        latest_open = min(end, self._now() - timedelta(minutes=1))
        lo, hi = bisect.bisect_left(ts, start), bisect.bisect_right(ts, latest_open)
        return self._candles.get(key.id, [])[lo:hi]

    def _day_close(self, key: InstrumentKey, day: date) -> float | None:
        o, c = cal.session_bounds(day)
        bars = self._complete_minutes(key, o, c)
        return bars[-1].close if bars and bars[-1].ts == c - timedelta(minutes=1) else None

    def _prev_close(self, key: InstrumentKey, today: date) -> float:
        prior = [d for d in self.days if d < today]
        if not prior:
            raise DataUnavailable(f"no previous close for {key.id} before {today} in dataset")
        close = self._day_close(key, prior[-1])
        if close is None:
            raise DataUnavailable(f"previous session for {key.id} incomplete")
        return close

    def _today(self) -> date:
        return self._now().astimezone(cal.IST).date()

    # ------------------------------------------------------------------ interface
    async def get_quote(self, key: InstrumentKey) -> Sourced[Quote]:
        today = self._today()
        o, _ = cal.session_bounds(today)
        tick = self._last_tick(key)
        if tick is not None and tick.ts >= o:
            ts_list = self._tick_ts_by_key[key.id]
            day_ticks = self._ticks_by_key[key.id][
                bisect.bisect_left(ts_list, o) : bisect.bisect_right(ts_list, self._now())
            ]
            prices = [t.ltp for t in day_ticks]
            q = Quote(
                key=key,
                ts=tick.ts,
                ltp=tick.ltp,
                open=prices[0],
                high=max(prices),
                low=min(prices),
                prev_close=self._prev_close(key, today),
                volume=tick.volume,
                oi=tick.oi,
            )
            return self._stamp(q, tick.ts, self.ttl.quote)
        bars = self._complete_minutes(key, o, self._now())
        if not bars:
            # before today's open (or no ticks): last completed bar of any prior day
            bars = self._complete_minutes(key, datetime.min.replace(tzinfo=o.tzinfo), self._now())
            if not bars:
                raise DataUnavailable(f"no data for {key.id} at {self._now()}")
            last_day = bars[-1].ts.astimezone(cal.IST).date()
            bars = self._complete_minutes(key, cal.session_bounds(last_day)[0], self._now())
            today = last_day
        as_of = bars[-1].ts + timedelta(minutes=1)
        q = Quote(
            key=key,
            ts=as_of,
            ltp=bars[-1].close,
            open=bars[0].open,
            high=max(b.high for b in bars),
            low=min(b.low for b in bars),
            prev_close=self._prev_close(key, today),
            volume=sum(b.volume for b in bars),
            oi=bars[-1].oi,
        )
        return self._stamp(q, as_of, self.ttl.quote)

    async def get_candles(
        self, key: InstrumentKey, interval: Interval, start: datetime, end: datetime
    ) -> Sourced[list[Candle]]:
        if interval is Interval.D1:
            out = []
            for d in self.days:
                o, c = cal.session_bounds(d)
                if not (start <= o <= end) or self._now() < c:
                    continue
                bars = self._complete_minutes(key, o, c)
                if bars:
                    out.append(aggregate(key, Interval.D1, o, bars))
            ttl = self.ttl.candles_daily
        else:
            minutes = self._complete_minutes(key, start, end)
            out = list(minutes) if interval is Interval.M1 else resample(key, interval, minutes, self._now())
            ttl = self.ttl.candles_intraday
        if not out:
            raise DataUnavailable(f"no {interval} candles for {key.id} in [{start}, {end}] at {self._now()}")
        as_of = out[-1].ts + timedelta(minutes=interval.minutes if interval is not Interval.D1 else 0)
        if interval is Interval.D1:
            as_of = cal.session_bounds(out[-1].ts.astimezone(cal.IST).date())[1]
        return self._stamp(out, as_of, ttl)

    async def get_expiries(self, underlying: InstrumentKey) -> list[date]:
        self._check_underlying(underlying)
        today, now = self._today(), self._now()
        return sorted(
            e for e, snaps in self._chains.items() if e >= today and snaps[0][0] <= now and now <= cal.expiry_close(e)
        )

    async def get_option_chain(self, underlying: InstrumentKey, expiry: date) -> Sourced[OptionChain]:
        self._check_underlying(underlying)
        snaps = self._chains.get(expiry, [])
        i = bisect.bisect_right([s[0] for s in snaps], self._now())
        if not i:
            raise DataUnavailable(f"no option chain for {underlying.id} {expiry} at {self._now()}")
        ts, raw = snaps[i - 1]
        return self._stamp(OptionChain.model_validate(raw), ts, self.ttl.option_chain)

    async def get_constituents(self, underlying: InstrumentKey) -> Sourced[list[Constituent]]:
        self._check_underlying(underlying)
        if not self.manifest.constituents:
            raise DataUnavailable(f"dataset has no constituents for {underlying.id}")
        quotes, as_of = [], None
        for info in self.manifest.constituents:
            key = InstrumentKey(exchange=underlying.exchange, underlying=info.symbol, is_index=False)
            q = (await self.get_quote(key)).value
            quotes.append((info, key, q))
            as_of = q.ts if as_of is None else min(as_of, q.ts)
        # Weights as of the previous close (as NSE publishes them), drifted from base_price by
        # relative performance. With these, index % change == sum(weight_i * change_i) / 100.
        raw = [info.weight * (q.prev_close / info.base_price if info.base_price else 1.0) for info, _, q in quotes]
        total = sum(raw)
        out = [
            Constituent(key=key, name=info.name, weight=100.0 * r / total, ltp=q.ltp, prev_close=q.prev_close)
            for (info, key, q), r in zip(quotes, raw, strict=True)
        ]
        return self._stamp(out, as_of, self.ttl.constituents)

    async def get_macro(self) -> MacroSnapshot:
        i = bisect.bisect_right(self._macro_ts, self._now())
        if not i:
            raise DataUnavailable(f"no macro data at {self._now()}")
        row = self._macro[i - 1]

        def f(v: float | None, ttl: float, as_of: datetime | None = None):
            return (
                None if v is None or (isinstance(v, float) and math.isnan(v)) else self._stamp(v, as_of or row.ts, ttl)
            )

        return MacroSnapshot(
            india_vix=f(row.india_vix, self.ttl.india_vix),
            usdinr=f(row.usdinr, self.ttl.usdinr),
            in10y_yield=f(row.in10y_yield, self.ttl.in10y_yield),
            fii_net_cr=f(row.fii_net_cr, self.ttl.fii_dii, row.flows_as_of),
            dii_net_cr=f(row.dii_net_cr, self.ttl.fii_dii, row.flows_as_of),
        )

    async def stream_ticks(self, keys: Sequence[InstrumentKey]) -> AsyncIterator[Tick]:
        """Yield ticks after clock.now() in time order, advancing a SimClock as it goes."""
        wanted = {k.id for k in keys}
        i = bisect.bisect_right(self._tick_ts, self._now())
        prev_ts = self._now()
        for t in self._ticks[i:]:
            if t.key.id not in wanted:
                continue
            if self.speed:
                # Overnight/weekend gaps would otherwise stall playback for 18+ minutes at 60x.
                await asyncio.sleep(min((t.ts - prev_ts).total_seconds() / self.speed, MAX_GAP_SLEEP_S))
            else:
                await asyncio.sleep(0)
            prev_ts = t.ts
            if isinstance(self.clock, SimClock):
                self.clock.set(t.ts)
            yield t

    def _check_underlying(self, underlying: InstrumentKey) -> None:
        if underlying.underlying != self.index_key.underlying:
            raise DataUnavailable(f"dataset is for {self.index_key.symbol}, not {underlying.symbol}")

    def validate_all(self) -> int:
        """Fully validate every chain snapshot (normally lazy). Returns the count."""
        n = 0
        for snaps in self._chains.values():
            for _, raw in snaps:
                OptionChain.model_validate(raw)
                n += 1
        return n


@functools.lru_cache(maxsize=4)
def _load(directory: Path, _mtime_ns: int) -> dict:
    """Parse + validate a dataset once; providers with different clocks share it.
    Keyed on the manifest mtime so a regenerated dataset is reloaded."""
    manifest = read_manifest(directory)
    index_key = InstrumentKey.parse(manifest.symbol)

    candles: dict[str, list[Candle]] = defaultdict(list)
    for c in read_candles(directory / "candles_1m.csv", index_key.underlying):
        candles[c.key.id].append(c)
    for lst in candles.values():
        lst.sort(key=lambda c: c.ts)

    ticks = sorted((Tick.model_validate(r) for r in iter_jsonl(directory / "ticks.jsonl.gz")), key=lambda t: t.ts)
    ticks_by_key: dict[str, list[Tick]] = defaultdict(list)
    for t in ticks:
        ticks_by_key[t.key.id].append(t)

    # Chains are validated lazily (on access) - they are the bulk of the dataset.
    chains: dict[date, list[tuple[datetime, dict]]] = defaultdict(list)
    for raw in iter_jsonl(directory / "chains.jsonl.gz"):
        chains[date.fromisoformat(raw["expiry"])].append((datetime.fromisoformat(raw["ts"]), raw))
    for snaps in chains.values():
        snaps.sort(key=lambda x: x[0])

    macro = sorted((MacroRow.model_validate(r) for r in iter_jsonl(directory / "macro.jsonl")), key=lambda m: m.ts)
    return {
        "manifest": manifest,
        "index_key": index_key,
        "days": list(manifest.days),
        "_candles": dict(candles),
        "_candle_ts": {k: [c.ts for c in v] for k, v in candles.items()},
        "_ticks": ticks,
        "_tick_ts": [t.ts for t in ticks],
        "_ticks_by_key": dict(ticks_by_key),
        "_tick_ts_by_key": {k: [t.ts for t in v] for k, v in ticks_by_key.items()},
        "_chains": dict(chains),
        "_macro": macro,
        "_macro_ts": [m.ts for m in macro],
    }


def _cadence_ttls(ttl: TTLs, cadence: dict[str, float]) -> TTLs:
    """Never judge data stale faster than the dataset can update it."""
    floors = {
        "tick": ["tick", "quote"],
        "option_chain": ["option_chain"],
        "macro": ["india_vix", "usdinr", "in10y_yield"],
    }
    upd = {}
    for kind, secs in cadence.items():
        for field in floors.get(kind, []):
            upd[field] = max(getattr(ttl, field), 1.2 * secs)
    return ttl.model_copy(update=upd)
