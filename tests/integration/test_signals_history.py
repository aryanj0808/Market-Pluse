"""Signals, outcomes and bhavcopy rows in Timescale (needs migration 005 applied)."""

from datetime import UTC, date, datetime

import psycopg
import pytest

from mp_core.loaders.bhavcopy import FoEodRecord
from mp_core.models import InstrumentKey, Source
from mp_core.signals.evaluation import Outcome
from mp_core.signals.models import Signal
from mp_core.store.history import History

from test_infra import _env

pytestmark = pytest.mark.integration
T = datetime(2026, 3, 5, 5, 0, tzinfo=UTC)
SYM = "NSE:ITESTIDX"  # not a real symbol: keeps test rows apart from real ones


def sig(sid, source):
    return Signal(
        signal_id=sid,
        symbol=SYM,
        ts=T,
        snapshot_version=1,
        source=source,
        status="no_signal",
        bias="neutral",
        composite=0.0,
        confidence=0.0,
        horizon="session",
        spot=100,
        sigma_points=1,
        components=[],
        reasons=[],
        rules_version="test",
    )


@pytest.fixture
async def hist():
    env = _env()
    h = await History(env["DATABASE_URL"]).connect()
    yield h
    with psycopg.connect(env["DATABASE_URL"], autocommit=True) as conn:
        conn.execute(
            "DELETE FROM signal_outcomes WHERE signal_id IN (SELECT signal_id FROM signals WHERE symbol = %s)", (SYM,)
        )
        conn.execute("DELETE FROM signals WHERE symbol = %s", (SYM,))
        conn.execute("DELETE FROM fo_eod WHERE underlying = 'ITESTIDX'")
    await h.close()


async def test_signal_roundtrip_and_synthetic_never_scored(hist):
    real, synth = (
        sig("00000000-0000-0000-0000-0000000000a1", Source.KITE),
        sig("00000000-0000-0000-0000-0000000000a2", Source.SYNTHETIC),
    )
    await hist.record_signal(real, {"k": 1})
    await hist.record_signal(synth, {"k": 2})
    pending = await hist.unscored_signals(SYM, T)
    assert [s.signal_id for s in pending] == [real.signal_id]
    assert await hist.count_synthetic_signals(SYM) == 1
    o = Outcome(
        signal_id=real.signal_id,
        horizon_end=T,
        outcome="no_trade",
        hit=None,
        mfe=None,
        mae=None,
        close_at_horizon=100,
        contained_1s=True,
        contained_2s=True,
        source=Source.KITE,
    )
    await hist.record_outcome(o)
    assert await hist.unscored_signals(SYM, T) == []
    assert (await hist.outcomes(SYM))[0].outcome == "no_trade"
    with pytest.raises(psycopg.errors.CheckViolation):
        await hist.record_outcome(o.model_copy(update={"signal_id": synth.signal_id, "source": Source.SYNTHETIC}))


async def test_fo_eod_roundtrip(hist):
    rec = FoEodRecord(
        trade_date=date(2026, 3, 5),
        key=InstrumentKey.parse("NSE:ITESTIDX:2026-03-26:100:CE"),
        open=1,
        high=2,
        low=1,
        close=1.5,
        settle=1.5,
        underlying_price=99,
        oi=10,
        oi_change=1,
        volume=5,
        volume_unit="units",
        lot_size=30,
    )
    await hist.record_fo_eod([rec, rec])  # idempotent
    got = await hist.fo_eod("ITESTIDX", date(2026, 3, 1), date(2026, 3, 31))
    assert got == [rec]
