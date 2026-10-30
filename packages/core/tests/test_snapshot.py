from datetime import UTC, date, datetime, timedelta

import pytest

from mp_core.analytics.expected_move import days_to_next_close, expected_move
from mp_core.clock import SimClock
from mp_core.models import Source, derive, derived_source, stamp
from mp_core.providers.replay import ReplayProvider
from mp_core.settings import TTLs
from mp_core.snapshot import EngineInputs, MarketSnapshot, build_snapshot, gather_inputs

from conftest import ist

SYM = "NSE:BANKNIFTY"
NOW = ist(date(2026, 9, 23), 11, 0)
R = 0.065
NO_NEWS = "news: no news poll yet (is ingest-news running?)"


@pytest.fixture(scope="module")
async def inputs(synth_dir) -> EngineInputs:
    p = ReplayProvider(synth_dir, SimClock(NOW))
    return await gather_inputs(p, SYM, NOW)


class TestDerive:
    def test_synthetic_is_contagious(self):
        assert derived_source([Source.KITE, Source.SYNTHETIC]) is Source.SYNTHETIC
        assert derived_source([Source.KITE, Source.KITE]) is Source.KITE
        assert derived_source([Source.KITE, Source.NSE_BHAVCOPY]) is Source.DERIVED

    def test_oldest_input_and_stalest_wins(self):
        t = datetime(2026, 9, 23, 5, 0, tzinfo=UTC)
        fresh = stamp(1.0, source=Source.KITE, as_of=t, now=t, ttl_seconds=60)
        old = stamp(2.0, source=Source.KITE, as_of=t - timedelta(seconds=30), now=t, ttl_seconds=10)
        d = derive(3.0, [fresh, old], now=t)
        assert d.as_of == old.as_of and d.ttl_seconds == 10 and d.stale and d.source is Source.KITE

    def test_mixed_cadence_inputs_expire_with_the_first_input(self):
        t = datetime(2026, 9, 23, 5, 0, tzinfo=UTC)
        quote = stamp(1.0, source=Source.KITE, as_of=t, now=t, ttl_seconds=5)  # just arrived
        bar = stamp(2.0, source=Source.KITE, as_of=t - timedelta(seconds=40), now=t, ttl_seconds=120)
        d = derive(3.0, [quote, bar], now=t)
        assert not d.stale and d.as_of == bar.as_of
        assert not d.restamp(t + timedelta(seconds=4)).stale
        assert d.restamp(t + timedelta(seconds=6)).stale  # the quote expired
        late = derive(3.0, [quote.restamp(t + timedelta(seconds=6)), bar], now=t + timedelta(seconds=6))
        assert late.stale == late.restamp(t + timedelta(seconds=6)).stale

    def test_needs_inputs(self):
        with pytest.raises(ValueError):
            derive(1, [], now=NOW)


async def test_snapshot_complete_and_synthetic(inputs):
    s = build_snapshot(inputs, NOW, rate=R, version=7)
    assert s.version == 7 and s.source is Source.SYNTHETIC and s.errors == [NO_NEWS]  # no news poll in this setup
    assert s.future is not None and s.levels is not None and s.constituents is not None and s.macro is not None
    assert {e.value.expiry for e in s.expiries} == {date(2026, 9, 29), date(2026, 10, 27)}
    assert set(s.expected_range.value) == {"session", "next_session", "expiry"}
    assert all(sec.source is Source.SYNTHETIC for sec in (s.quote, s.future, s.levels, s.expected_range))
    assert s.compute_ms < 50


async def test_expected_range_matches_analytics(inputs):
    s = build_snapshot(inputs, NOW, rate=R)
    front_iv = s.expiries[0].value.iv.atm_iv
    em = expected_move(s.quote.value.ltp, front_iv, days_to_next_close(NOW, 2))
    assert s.expected_range.value["next_session"] == em


async def test_staleness_propagates(inputs):
    later = NOW + timedelta(minutes=30)  # nothing new has arrived for 30 minutes
    s = build_snapshot(inputs, later, rate=R)
    assert {"quote", "future", "expected_range"} <= set(s.stale_sections)
    assert s.quote.stale and s.expected_range.stale


async def test_real_source_is_kept(inputs):
    def relabel(x):
        return x.model_copy(update={"source": Source.RECORDED}) if x is not None else None

    rec = inputs.model_copy(
        update={
            "quote": relabel(inputs.quote),
            "future_quote": relabel(inputs.future_quote),
            "chains": {k: relabel(v) for k, v in inputs.chains.items()},
            "prev_chains": {},
            "daily": relabel(inputs.daily),
            "future_1m_today": relabel(inputs.future_1m_today),
            "constituents": relabel(inputs.constituents),
            "macro": None,
        }
    )
    s = build_snapshot(rec, NOW, rate=R)
    assert s.source is Source.RECORDED and s.levels.source is Source.RECORDED


async def test_missing_inputs_are_reported_not_guessed(inputs):
    bare = EngineInputs(symbol=SYM, quote=inputs.quote)
    s = build_snapshot(bare, NOW, rate=R)
    assert s.expected_range is None and s.levels is None and s.future is None and s.expiries == []
    joined = " | ".join(s.errors)
    for needle in (
        "expected_range: no option chain",
        "levels: no daily candles",
        "future: no front-month",
        "constituents: unavailable",
    ):
        assert needle in joined


async def test_expired_chain_is_ignored(inputs):
    s = build_snapshot(inputs, ist(date(2026, 9, 29), 15, 31), rate=R)
    assert date(2026, 9, 29) not in {e.value.expiry for e in s.expiries}


async def test_deterministic_and_roundtrips(inputs):
    a = build_snapshot(inputs, NOW, rate=R)
    b = build_snapshot(inputs, NOW, rate=R)
    assert a.model_copy(update={"compute_ms": 0}) == b.model_copy(update={"compute_ms": 0})
    assert MarketSnapshot.model_validate_json(a.model_dump_json()) == a


async def test_cadence_ttl_floor(synth_dir):
    # chains arrive every 5 min in this dataset: 4 minutes after a snapshot it is not stale...
    p = ReplayProvider(synth_dir, SimClock(ist(date(2026, 9, 23), 11, 4)))
    assert p.ttl.option_chain == pytest.approx(360)
    assert not (await p.get_option_chain(p.index_key, date(2026, 9, 29))).stale
    # ...but 7 minutes after it is, and the configured TTL is untouched for other kinds
    p.clock.set(ist(date(2026, 9, 23), 11, 7, 30))
    p2 = ReplayProvider(synth_dir, SimClock(ist(date(2026, 9, 23), 11, 0)))
    chain = await p2.get_option_chain(p2.index_key, date(2026, 9, 29))
    assert chain.restamp(ist(date(2026, 9, 23), 11, 7)).stale
    assert p.ttl.constituents == TTLs().constituents


async def test_ivs_are_evaluated_at_chain_time_not_now(inputs):
    a = build_snapshot(inputs, NOW, rate=R)
    b = build_snapshot(inputs, NOW + timedelta(seconds=45), rate=R)  # same chain, later clock
    assert a.expiries[0].value.iv == b.expiries[0].value.iv  # no new prices -> identical IVs (small diffs)
    assert a.expiries[0].value.iv.ts == inputs.chains[a.expiries[0].value.expiry].value.ts
