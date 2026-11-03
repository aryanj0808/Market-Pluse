"""Intraday backtest of fuse() over a replay dataset (same path as the engine)."""

from datetime import date, timedelta

import pytest

from mp_core.signals.evaluation import score_signal
from mp_core.signals.replay_backtest import run_replay_backtest

STEP = timedelta(minutes=5)  # coarse steps keep the test quick


@pytest.fixture(scope="module")
async def result(synth_dir):
    return await run_replay_backtest(
        synth_dir, start=date(2026, 9, 15), end=date(2026, 9, 25), step=STEP, allow_synthetic=True
    )


async def test_refuses_synthetic_without_opt_in(synth_dir):
    with pytest.raises(ValueError, match="synthetic"):
        await run_replay_backtest(synth_dir, step=STEP)


def test_counts_add_up(result):
    assert result.source == "synthetic" and result.rules_version == "fusion-v2"
    assert result.days > 0 and result.snapshots > 0 and result.signals_logged >= result.active
    assert result.filled == len(result.trades) == result.targets + result.stops + result.open
    assert result.total_pnl_inr == pytest.approx(sum(t.pnl_inr for t in result.trades))
    assert any("SYNTHETIC DATA" in n for n in result.notes) and any("news not included" in n for n in result.notes)


def test_one_position_at_a_time(result):
    ts = [t.ts for t in result.trades]
    assert ts == sorted(ts) and len(set(ts)) == len(ts)


def test_costs_are_charged():
    from test_evaluation import T0, bars, signal

    from mp_core.signals.models import Sizing
    from mp_core.signals.replay_backtest import _trade

    # entry 100, stop 95, target 110 on the index; the future trades 2 points higher
    sz = Sizing(
        instrument="NSE:BANKNIFTY:2026-10-27",
        lot_size=30,
        entry=100,
        stop_loss=95,
        future_entry=102,
        future_stop_loss=97,
        costs_per_lot=60,
        risk_per_lot=(5 + 2) * 30,
        max_loss=1000,
        lots=3,
        loss_at_stop=3 * 210,
    )
    sig = signal().model_copy(update={"sizing": sz})
    win = bars([(104, 101.5, 103), (113, 103, 112)], start=T0)
    _, t = _trade(sig, win, None)
    assert t.outcome == "target" and t.pnl_points == pytest.approx(10 - 2) and t.pnl_inr == pytest.approx(8 * 90)
    loss = bars([(104, 101.5, 103), (103, 96, 97)], start=T0)
    _, t = _trade(sig, loss, None)
    assert t.outcome == "stop" and t.pnl_points == pytest.approx(-5 - 2)


def test_holidays_are_skipped(result):
    assert date(2026, 9, 14) not in {t.ts.date() for t in result.trades}  # Ganesh Chaturthi


async def test_deterministic(synth_dir, result):
    again = await run_replay_backtest(
        synth_dir, start=date(2026, 9, 15), end=date(2026, 9, 25), step=STEP, allow_synthetic=True
    )
    assert again == result


def test_cancelled_entry_never_fills():
    from test_evaluation import bars, signal

    # the dip to the entry comes after the order was withdrawn
    b = bars([(105, 101, 104), (104, 101, 103), (101, 99, 100), (111, 100, 110)])
    assert score_signal(signal(), b).outcome == "target"
    o = score_signal(signal(), b, cancel_at=b[2].ts)
    assert o.outcome == "not_filled" and o.fill_ts is None
