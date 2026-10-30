"""Candle aggregation: 1-minute bars -> session-aligned higher intervals."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime, timedelta

from mp_core import market_calendar as cal
from mp_core.models import Candle, InstrumentKey, Interval


def aggregate(key: InstrumentKey, interval: Interval, ts: datetime, bars: Sequence[Candle]) -> Candle:
    return Candle(
        key=key,
        interval=interval,
        ts=ts,
        open=bars[0].open,
        high=max(b.high for b in bars),
        low=min(b.low for b in bars),
        close=bars[-1].close,
        volume=sum(b.volume for b in bars),
        oi=bars[-1].oi,
    )


def resample(key: InstrumentKey, interval: Interval, minutes: Sequence[Candle], now: datetime) -> list[Candle]:
    """Bucket 1m bars into session-aligned bars (09:15 anchor; the last bucket of the day may be
    short). Incomplete buckets - missing minutes, or not yet closed at `now` - are dropped."""
    if interval is Interval.M1:
        return [b for b in minutes if b.ts + timedelta(minutes=1) <= now]
    buckets: dict[datetime, list[Candle]] = defaultdict(list)
    for b in minutes:
        day = b.ts.astimezone(cal.IST).date()
        if interval is Interval.D1:
            buckets[cal.session_bounds(day)[0]].append(b)
            continue
        o, _ = cal.session_bounds(day)
        idx = int((b.ts - o).total_seconds() // 60) // interval.minutes
        buckets[o + timedelta(minutes=idx * interval.minutes)].append(b)
    out = []
    for ts in sorted(buckets):
        bars = sorted(buckets[ts], key=lambda c: c.ts)
        _, close = cal.session_bounds(ts.astimezone(cal.IST).date())
        bucket_end = close if interval is Interval.D1 else min(ts + timedelta(minutes=interval.minutes), close)
        expected = int((bucket_end - ts).total_seconds() // 60)
        if len(bars) == expected and bucket_end <= now:
            out.append(aggregate(key, interval, ts, bars))
    return out
