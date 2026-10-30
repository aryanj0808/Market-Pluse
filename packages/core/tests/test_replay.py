import time as _time
from datetime import date, timedelta

import pytest

from mp_core.clock import SimClock
from mp_core.models import InstrumentKey, Interval, Source
from mp_core.providers.base import DataUnavailable, MarketDataProvider
from mp_core.providers.replay import ReplayProvider

from conftest import ist

BN = InstrumentKey.parse("NSE:BANKNIFTY")
D2 = date(2026, 9, 2)


def provider(synth_dir, at, **kw) -> ReplayProvider:
    return ReplayProvider(synth_dir, SimClock(at), **kw)


def test_is_a_market_data_provider_without_order_methods(synth_dir):
    p = provider(synth_dir, ist(D2, 10, 0))
    assert isinstance(p, MarketDataProvider)
    assert not [m for m in dir(p) if any(w in m.lower() for w in ("order", "position", "fund", "margin"))]
    assert p.source is Source.SYNTHETIC


@pytest.mark.parametrize("hh, mm", [(9, 16), (11, 7), (15, 29)])
async def test_no_look_ahead(synth_dir, hh, mm):
    at = ist(D2, hh, mm)
    p = provider(synth_dir, at)
    q = await p.get_quote(BN)
    assert q.as_of <= at and q.value.ts <= at
    bars = (await p.get_candles(BN, Interval.M1, ist(D2, 9, 0), ist(D2, 16, 0))).value
    assert all(b.ts + timedelta(minutes=1) <= at for b in bars)
    assert bars[-1].ts + timedelta(minutes=2) > at  # ...and nothing older is missing
    for e in await p.get_expiries(BN):
        assert (await p.get_option_chain(BN, e)).as_of <= at
    mac = await p.get_macro()
    assert mac.india_vix.as_of <= at and mac.fii_net_cr.as_of < ist(D2, 9, 15)
    cons = await p.get_constituents(BN)
    assert cons.as_of <= at and len(cons.value) == 14


async def test_quote_matches_ticks_and_prev_close(synth_dir):
    p = provider(synth_dir, ist(D2, 12, 0, 7))
    q = (await p.get_quote(BN)).value
    assert q.ts == ist(D2, 12, 0)  # last 15s tick at or before now
    d1 = (await p.get_candles(BN, Interval.D1, ist(date(2026, 9, 1), 0, 0), ist(D2, 0, 0))).value
    assert q.prev_close == d1[-1].close


async def test_resampled_bars_complete_only(synth_dir):
    p = provider(synth_dir, ist(D2, 10, 44))
    bars = (await p.get_candles(BN, Interval.M15, ist(D2, 9, 0), ist(D2, 16, 0))).value
    assert [b.ts.astimezone(ist(D2, 0, 0).tzinfo).strftime("%H:%M") for b in bars] == [
        "09:15",
        "09:30",
        "09:45",
        "10:00",
        "10:15",
    ]  # 10:30 bar not closed until 10:45
    one_min = (await p.get_candles(BN, Interval.M1, ist(D2, 10, 0), ist(D2, 10, 14))).value
    assert bars[3].high == max(b.high for b in one_min) and bars[3].close == one_min[-1].close


async def test_hourly_last_bucket_is_short(synth_dir):
    p = provider(synth_dir, ist(D2, 16, 0))
    bars = (await p.get_candles(BN, Interval.H1, ist(D2, 9, 0), ist(D2, 16, 0))).value
    assert len(bars) == 7  # 09:15..14:15 full hours + 15:15-15:30 stub


async def test_daily_bars_only_after_close(synth_dir):
    rng = (ist(date(2026, 9, 1), 0, 0), ist(D2, 23, 0))
    during = (await provider(synth_dir, ist(D2, 15, 0)).get_candles(BN, Interval.D1, *rng)).value
    after = (await provider(synth_dir, ist(D2, 15, 31)).get_candles(BN, Interval.D1, *rng)).value
    assert len(during) == 1 and len(after) == 2


async def test_stale_flags(synth_dir):
    p = provider(synth_dir, ist(D2, 11, 0))
    assert not (await p.get_quote(BN)).stale
    p.clock.set(ist(D2, 17, 0))  # after the close: last tick is 90 minutes old
    q = await p.get_quote(BN)
    assert q.stale and q.as_of == ist(D2, 15, 30)
    assert (await p.get_option_chain(BN, date(2026, 9, 29))).stale


async def test_expiry_disappears_after_expiry_close(synth_dir):
    exp = date(2026, 9, 29)
    assert exp in await provider(synth_dir, ist(exp, 15, 0)).get_expiries(BN)
    assert exp not in await provider(synth_dir, ist(date(2026, 9, 30), 10, 0)).get_expiries(BN)


async def test_stream_ticks_advances_clock_in_order(synth_dir):
    p = provider(synth_dir, ist(D2, 9, 59, 50))
    seen = []
    async for t in p.stream_ticks([BN]):
        assert p.clock.now() == t.ts
        seen.append(t.ts)
        if len(seen) == 8:
            break
    assert seen[0] == ist(D2, 10, 0) and seen == sorted(seen)
    assert all(b - a == timedelta(seconds=15) for a, b in zip(seen, seen[1:], strict=False))


async def test_stream_speed_multiplier(synth_dir):
    p = provider(synth_dir, ist(D2, 10, 0), speed=1500)  # 15 s tick -> 10 ms
    t0, n = _time.perf_counter(), 0
    async for _ in p.stream_ticks([BN]):
        n += 1
        if n == 10:
            break
    assert 0.08 < _time.perf_counter() - t0 < 2.0


async def test_errors_are_explicit(synth_dir):
    p = provider(synth_dir, ist(date(2026, 9, 1), 10, 0))
    with pytest.raises(DataUnavailable, match="no previous close"):
        await p.get_quote(BN)  # first day of dataset
    with pytest.raises(DataUnavailable, match="not NSE:NIFTY"):
        await p.get_expiries(InstrumentKey.parse("NSE:NIFTY"))
    with pytest.raises(DataUnavailable):
        await p.get_option_chain(BN, date(2027, 1, 26))
    with pytest.raises(DataUnavailable):
        await provider(synth_dir, ist(date(2026, 8, 1), 10, 0)).get_macro()


def test_simclock_cannot_go_backwards(synth_dir):
    p = provider(synth_dir, ist(D2, 10, 0))
    with pytest.raises(ValueError, match="backwards"):
        p.clock.set(ist(D2, 9, 0))


def test_every_chain_snapshot_validates(synth_dir):
    assert provider(synth_dir, ist(D2, 10, 0)).validate_all() == 25 * 75 * 2
