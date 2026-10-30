"""The ingest -> cache -> engine pipeline. Services are thin `main()`s around these classes.

Ingestor: drives a MarketDataProvider, writes raw data to the MarketStore (Redis), records it to
          Timescale, and announces each update on `mp:{SYM}:raw`.
Engine:   on raw updates (debounced), loads inputs from the store, runs `build_snapshot`, writes
          the snapshot and announces it on `mp:{SYM}:updates`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from datetime import date, datetime, timedelta

from mp_core import market_calendar as cal
from mp_core.analytics.options import iv_summary
from mp_core.clock import SimClock
from mp_core.models import InstrumentKey, Interval, Tick
from mp_core.news.store import NewsStore
from mp_core.providers.base import DataUnavailable, MarketDataProvider
from mp_core.registry import get_index
from mp_core.signals.fusion import RiskConfig, fuse
from mp_core.signals.logger import SignalLogger
from mp_core.snapshot import MarketSnapshot, build_snapshot, front_future_key
from mp_core.store.history import History
from mp_core.store.market_store import MarketStore

log = logging.getLogger(__name__)


class Ingestor:
    def __init__(
        self,
        provider: MarketDataProvider,
        store: MarketStore,
        symbol: str,
        *,
        history: History | None = None,
        rate: float = 0.065,
        chain_poll: timedelta = timedelta(seconds=60),
        slow_poll: timedelta = timedelta(seconds=60),
        tick_flush: int = 500,
    ) -> None:
        self.p = provider
        self.store = store
        self.key = InstrumentKey.parse(symbol)
        self.symbol = self.key.symbol
        self.spec = get_index(symbol)
        self.history = history
        self.rate = rate
        self.chain_poll = chain_poll
        self.slow_poll = slow_poll
        self.tick_flush = tick_flush
        self.mode = "replay" if isinstance(getattr(provider, "clock", None), SimClock) else "live"
        self._day: date | None = None
        self._last_chain: datetime | None = None
        self._last_slow: datetime | None = None
        self._last_minute: datetime | None = None
        self._recorded_upto: dict[str, datetime] = {}
        self._tick_buf: list[Tick] = []
        self._started = False
        self.stats = {
            "ticks": 0,
            "chains": 0,
            "candles": 0,
            "recorded_ticks": 0,
            "recorded_quotes": 0,
            "record_errors": 0,
        }

    def _now(self) -> datetime:
        return self.p.clock.now()  # type: ignore[attr-defined]

    def _futures(self, day: date) -> list[InstrumentKey]:
        front = cal.front_future_expiry(self.spec, day)
        nxt = cal.front_future_expiry(self.spec, front + timedelta(days=1))
        return [
            InstrumentKey(exchange=self.key.exchange, underlying=self.key.underlying, expiry=e) for e in (front, nxt)
        ]

    async def _opt(self, coro):
        try:
            return await coro
        except DataUnavailable as ex:
            log.debug("unavailable: %s", ex)
            return None

    # ------------------------------------------------------------------ refreshers
    async def _on_new_day(self, now: datetime) -> None:
        self._day = now.astimezone(cal.IST).date()
        fut = front_future_key(self.symbol, self._day)
        await self.store.put_future_key(self.symbol, fut)
        daily = await self._opt(self.p.get_candles(self.key, Interval.D1, now - timedelta(days=14), now))
        if daily is not None:
            await self.store.put_candles(self.symbol, "daily", self.key, daily)
        self._last_minute = None
        self._last_chain = None
        self._last_slow = None

    async def _refresh_minute(self, now: datetime) -> None:
        """Today's completed 1m bars for the futures (VWAP, build-up) + record new bars."""
        o, _ = cal.session_bounds(self._day)
        fut = front_future_key(self.symbol, self._day)
        bars = await self._opt(self.p.get_candles(fut, Interval.M1, o, now))
        if bars is not None:
            await self.store.put_candles(self.symbol, "candles1m", fut, bars)
            await self.store.notify_raw(self.symbol, "candles")
        idx = await self._opt(self.p.get_candles(self.key, Interval.M1, o, now))
        if idx is not None:  # today's index bars: the dashboard chart's initial history
            await self.store.put_candles(self.symbol, "candles1m", self.key, idx)
        if self.history is not None:
            keys = [self.key, fut] + [
                InstrumentKey(exchange=self.key.exchange, underlying=c.symbol, is_index=False)
                for c in self.spec.constituents
            ]
            for k in keys:
                got = await self._opt(self.p.get_candles(k, Interval.M1, self._recorded_upto.get(k.id, o), now))
                new = [c for c in (got.value if got else []) if c.ts >= self._recorded_upto.get(k.id, o)]
                if new:
                    n = await self._record("candles", self.history.record_candles(new, self.p.source))
                    if n >= 0:  # on failure the pointer stays put: the bars are written once the DB is back
                        self.stats["candles"] += n
                        self._recorded_upto[k.id] = new[-1].ts + timedelta(minutes=1)

    async def _refresh_chains(self, now: datetime) -> None:
        expiries = await self.p.get_expiries(self.key)
        await self.store.put_expiries(self.symbol, expiries)
        for e in expiries:
            ch = await self._opt(self.p.get_option_chain(self.key, e))
            if ch is None or not await self.store.put_chain(self.symbol, ch):
                continue
            self.stats["chains"] += 1
            if self.history is not None:
                s = iv_summary(ch.value, ch.value.ts, self.rate)
                ivs = {(x.strike, "CE"): x.call_iv for x in s.strikes if x.call_iv} | {
                    (x.strike, "PE"): x.put_iv for x in s.strikes if x.put_iv
                }
                n = await self._record("option chain", self.history.record_chain(ch.value, self.p.source, ivs))
                self.stats["recorded_quotes"] += max(n, 0)
        await self.store.notify_raw(self.symbol, "chain")

    async def _refresh_slow(self, now: datetime) -> None:
        cons = await self._opt(self.p.get_constituents(self.key))
        if cons is not None:
            await self.store.put_constituents(self.symbol, cons)
        macro = await self._opt(self.p.get_macro())
        if macro is not None:
            await self.store.put_macro(macro)
        await self.store.notify_raw(self.symbol, "slow")

    async def _flush_ticks(self) -> None:
        if self.history is not None and self._tick_buf:
            n = await self._record("ticks", self.history.record_ticks(self._tick_buf, self.p.source))
            self.stats["recorded_ticks"] += max(n, 0)
        self._tick_buf.clear()

    async def _record(self, what: str, call) -> int:
        """Recording is best-effort: a database outage must never stop live data. Returns the row
        count, or -1 on failure (counted, and logged at 1, 10, 100, then every 1000 failures)."""
        try:
            return await call
        except Exception as ex:
            self.stats["record_errors"] += 1
            n = self.stats["record_errors"]
            if n in (1, 10, 100) or n % 1000 == 0:
                log.warning("recording %s failed (%d failures so far): %s", what, n, ex)
            return -1

    # ------------------------------------------------------------------ main loop
    async def on_tick(self, tick: Tick) -> None:
        now = self._now()
        await self.store.set_clock(now, self.mode)
        if self._day != now.astimezone(cal.IST).date():
            await self._on_new_day(now)
        q = await self._opt(self.p.get_quote(tick.key))
        if q is not None:
            await self.store.put_quote(self.symbol, q)
        self._tick_buf.append(tick)
        self.stats["ticks"] += 1
        minute = now.replace(second=0, microsecond=0)
        if self._last_minute != minute:
            self._last_minute = minute
            await self._refresh_minute(now)
        if self._last_chain is None or now - self._last_chain >= self.chain_poll:
            self._last_chain = now
            await self._refresh_chains(now)
        if self._last_slow is None or now - self._last_slow >= self.slow_poll:
            self._last_slow = now
            await self._refresh_slow(now)
        if len(self._tick_buf) >= self.tick_flush:
            await self._flush_ticks()
        await self.store.notify_raw(self.symbol, "quote", key=tick.key.id)

    async def run(self, max_ticks: int | None = None, until: datetime | None = None) -> dict[str, int]:
        if self.mode == "replay" and not self._started:
            await self.store.reset(self.symbol)
            # publish the replay clock at once: a clock left over from an earlier run must not be
            # mistaken for "now" by the other services while the first tick is on its way
            await self.store.set_clock(self._now(), self.mode)
        self._started = True
        keys = [self.key, *self._futures(self._now().astimezone(cal.IST).date())]
        try:
            async for tick in self.p.stream_ticks(keys):
                if until is not None and tick.ts > until:
                    break
                await self.on_tick(tick)
                if max_ticks is not None and self.stats["ticks"] >= max_ticks:
                    break
        finally:
            await self._flush_ticks()
        return self.stats


class Engine:
    def __init__(
        self,
        store: MarketStore,
        symbol: str,
        *,
        rate: float = 0.065,
        debounce_s: float = 0.01,  # catches the index + futures ticks that arrive together
        min_interval: float = 0.26,  # ~3.85/s: inside the gateway's 4/s window, never held back there
        risk: RiskConfig | None = None,
        history: History | None = None,
    ) -> None:
        self.store = store
        self.risk = risk or RiskConfig()
        self.signal_log = SignalLogger(history) if history is not None else None
        self.log_errors = 0
        self.symbol = InstrumentKey.parse(symbol).symbol
        self.rate = rate
        self.debounce_s = debounce_s
        self.min_interval = min_interval
        self._expiry_cache: dict = {}
        self._tick_wall: float | None = None  # arrival of the oldest raw update not yet in a snapshot
        self.version = 0
        self.last: MarketSnapshot | None = None
        self.news = NewsStore(store.kv)
        self._news_version = -1
        self._news_items: list = []

    async def _load_news(self, now):
        """Reload news items only when ingest-news has changed something (cheap version check)."""
        v = await self.news.version()
        if v != self._news_version:
            self._news_items = await self.news.recent(now - timedelta(days=20), now + timedelta(days=1))
            self._news_version = v
        return [i for i in self._news_items if i.item.published_at <= now], await self.news.last_poll()

    async def compute_once(self) -> MarketSnapshot | None:
        inputs = await self.store.load_inputs(self.symbol)
        if inputs is None:
            return None
        if self.version == 0 and (prev := await self.store.get_snapshot(self.symbol)) is not None:
            self.version = prev.version  # keep versions monotonic across engine restarts
        self.version += 1
        now = await self.store.now()
        items, poll = await self._load_news(now)
        inputs = inputs.model_copy(update={"news": items, "news_poll": poll})
        snap = build_snapshot(inputs, now, rate=self.rate, version=self.version, cache=self._expiry_cache)
        snap = snap.model_copy(update={"signal": fuse(snap, self.risk)})
        tick_wall, self._tick_wall = self._tick_wall, None
        await self.store.put_snapshot(snap, tick_wall)
        if self.signal_log is not None:
            try:
                await self.signal_log.maybe_log(snap)
            except Exception as ex:  # logging signals is best-effort; the snapshot is already published
                self.log_errors += 1
                if self.log_errors in (1, 10, 100) or self.log_errors % 1000 == 0:
                    log.warning("signal logging failed (%d so far): %s", self.log_errors, ex)
        self.last = snap
        return snap

    async def run(self, stop: asyncio.Event | None = None) -> None:
        """Recompute on raw updates until `stop` is set. Leading-edge throttle: a quiet engine reacts
        within `debounce_s`; under a stream of ticks it recomputes at most every `min_interval`, always
        from the newest state. This is the only throttle on the path, so delays never stack."""
        dirty = asyncio.Event()

        async def listen() -> None:
            async for raw in self.store.raw_events(self.symbol):
                if self._tick_wall is None:
                    with contextlib.suppress(ValueError, TypeError, KeyError):
                        self._tick_wall = json.loads(raw)["wall"]
                dirty.set()

        listener = asyncio.create_task(listen())
        last = 0.0
        try:
            await self.compute_once()
            last = time.perf_counter()
            while stop is None or not stop.is_set():
                waiter = asyncio.create_task(dirty.wait())
                stopper = asyncio.create_task(stop.wait()) if stop else None
                await asyncio.wait({waiter, *([stopper] if stopper else [])}, return_when=asyncio.FIRST_COMPLETED)
                for t in (waiter, stopper):
                    if t is not None and not t.done():
                        t.cancel()
                if stop is not None and stop.is_set():
                    break
                await asyncio.sleep(max(self.debounce_s, self.min_interval - (time.perf_counter() - last)))
                dirty.clear()
                last = time.perf_counter()  # period measured from compute start: compute time doesn't stretch it
                try:
                    await self.compute_once()
                except Exception:  # a bad input must not kill the engine; it is logged and retried
                    log.exception("snapshot computation failed")
        finally:
            listener.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await listener


__all__ = ["Engine", "Ingestor"]
