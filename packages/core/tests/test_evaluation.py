"""Signal scoring, statistics and backtests on hand-built data with known answers."""

from datetime import UTC, date, datetime, timedelta

import pytest

from mp_core import market_calendar as cal
from mp_core.analytics.black_scholes import price
from mp_core.loaders.bhavcopy import FoEodRecord
from mp_core.models import Candle, InstrumentKey, Interval, OptionType, Source
from mp_core.signals.backtest import run_backtest
from mp_core.signals.evaluation import (
    Outcome,
    SyntheticDataRefused,
    compute_stats,
    evaluate_pending,
    score_signal,
)
from mp_core.signals.models import Signal, Target

BN = InstrumentKey.parse("NSE:BANKNIFTY")
DAY = date(2026, 10, 20)
T0 = datetime(2026, 10, 20, 5, 0, tzinfo=UTC)  # 10:30 IST


def signal(
    status="active",
    bias="bullish",
    entry=100.0,
    stop=95.0,
    target=110.0,
    source=Source.KITE,
    sigma=8.0,
    spot=101.0,
    ts=T0,
    sid="00000000-0000-0000-0000-000000000001",
):
    return Signal(
        signal_id=sid,
        symbol="NSE:BANKNIFTY",
        ts=ts,
        snapshot_version=1,
        source=source,
        status=status,
        bias=bias,
        composite=0.5,
        confidence=0.5,
        horizon="session",
        spot=spot,
        sigma_points=sigma,
        components=[],
        entry=entry if status == "active" else None,
        stop_loss=stop if status == "active" else None,
        targets=[Target(price=target, basis="test")] if status == "active" else [],
        reasons=[],
        rules_version="test",
    )


def bars(path, start=T0, until_close=True):
    """1m bars through `path` [(high, low, close)...], then flat bars at the last close until 15:30."""
    out, t = [], start
    for h, lo, c in path:
        out.append(Candle(key=BN, interval=Interval.M1, ts=t, open=c, high=h, low=lo, close=c))
        t += timedelta(minutes=1)
    close = cal.session_bounds(DAY)[1]
    while until_close and t < close:
        c = path[-1][2]
        out.append(Candle(key=BN, interval=Interval.M1, ts=t, open=c, high=c, low=c, close=c))
        t += timedelta(minutes=1)
    return out


class TestScore:
    def test_target_before_stop(self):
        o = score_signal(signal(), bars([(102, 99.5, 100), (111, 100, 109)]))
        assert o.outcome == "target" and o.hit is True and o.mfe == 11 and o.mae == pytest.approx(0.5)

    def test_stop(self):
        o = score_signal(signal(), bars([(101, 99, 100), (100, 94, 96)]))
        assert o.outcome == "stop" and o.hit is False and o.mae == 6

    def test_stop_and_target_in_one_bar_counts_as_stop(self):
        assert score_signal(signal(), bars([(101, 99, 100), (112, 94, 100)])).outcome == "stop"

    def test_not_filled(self):
        o = score_signal(signal(), bars([(105, 101, 104)]))
        assert o.outcome == "not_filled" and o.hit is None and o.mfe is None

    def test_open_until_close_and_containment(self):
        o = score_signal(signal(), bars([(101, 99.5, 100), (104, 100, 103)]))
        assert o.outcome == "open" and o.close_at_horizon == 103
        assert o.contained_1s and o.contained_2s  # |103 - 101| <= 8

    def test_bearish_mirror(self):
        s = signal(bias="bearish", entry=100, stop=105, target=90)
        assert score_signal(s, bars([(100.5, 98, 99), (99, 89, 90)])).outcome == "target"

    def test_no_trade_still_scores_containment(self):
        o = score_signal(signal(status="no_signal"), bars([(130, 100, 125)]))
        assert o.outcome == "no_trade" and not o.contained_1s and not o.contained_2s  # 24 pts > 2 sigma

    def test_not_ready_before_horizon(self):
        assert score_signal(signal(), bars([(101, 99, 100)], until_close=False)) is None

    def test_synthetic_refused(self):
        with pytest.raises(SyntheticDataRefused):
            score_signal(signal(source=Source.SYNTHETIC), bars([(101, 99, 100)]))


def outcome(kind, src=Source.KITE, c1=True, c2=True, mfe=5.0, mae=2.0):
    return Outcome(
        signal_id="x",
        horizon_end=T0,
        outcome=kind,
        hit={"target": True, "stop": False}.get(kind),
        mfe=mfe if kind in ("target", "stop", "open") else None,
        mae=mae if kind in ("target", "stop", "open") else None,
        close_at_horizon=100,
        contained_1s=c1,
        contained_2s=c2,
        source=src,
    )


class TestStats:
    def test_math(self):
        os_ = [
            outcome("target"),
            outcome("target"),
            outcome("stop", c1=False),
            outcome("not_filled"),
            outcome("no_trade", c2=False),
        ]
        s = compute_stats("NSE:BANKNIFTY", os_)
        assert (s.n_scored, s.n_active, s.n_filled, s.n_target, s.n_stop) == (5, 4, 3, 2, 1)
        assert s.hit_rate == pytest.approx(2 / 3) and s.fill_rate == pytest.approx(3 / 4)
        assert s.containment_1s == pytest.approx(4 / 5) and s.containment_2s == pytest.approx(4 / 5)
        assert "anecdotal" in s.message

    def test_refuses_when_everything_is_synthetic(self):
        s = compute_stats("NSE:BANKNIFTY", [outcome("target", src=Source.SYNTHETIC)], n_synthetic_signals=40)
        assert s.n_scored == 0 and s.hit_rate is None and s.containment_1s is None
        assert s.message.startswith("refusing to report statistics: all 41 signals")

    def test_synthetic_never_mixed_in(self):
        s = compute_stats("NSE:BANKNIFTY", [outcome("target"), outcome("stop", src=Source.SYNTHETIC)])
        assert s.n_scored == 1 and s.hit_rate == 1.0 and s.n_excluded_synthetic == 1


class FakeEvalHistory:
    def __init__(self, signals, bar_list, sources):
        self.signals, self.bars, self.sources, self.recorded = signals, bar_list, sources, []

    async def unscored_signals(self, symbol, until, include_synthetic=False):
        return [s for s in self.signals if include_synthetic or s.source is not Source.SYNTHETIC]

    async def candles(self, key, interval, start, end, now):
        return self.bars, self.sources

    async def record_outcome(self, o):
        self.recorded.append(o)


async def test_evaluate_pending():
    after_close = datetime(2026, 10, 20, 11, 0, tzinfo=UTC)
    h = FakeEvalHistory(
        [signal(), signal(source=Source.SYNTHETIC, sid="00000000-0000-0000-0000-000000000002")],
        bars([(101, 99, 100), (111, 100, 110)]),
        {"kite"},
    )
    stats = await evaluate_pending(h, "NSE:BANKNIFTY", after_close)
    assert stats == {"pending": 1, "scored": 1, "not_ready": 0, "refused_synthetic_bars": 0}
    assert h.recorded[0].outcome == "target"
    before_close = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)
    assert (await evaluate_pending(h, "NSE:BANKNIFTY", before_close))["not_ready"] == 1
    synth_bars = FakeEvalHistory([signal()], bars([(101, 99, 100)]), {"synthetic"})
    assert (await evaluate_pending(synth_bars, "NSE:BANKNIFTY", after_close))["refused_synthetic_bars"] == 1


# ------------------------------------------------------------------ backtests (hand-built bhavcopy)
R = 0.065


def eod_day(d, spot, iv, expiry, call_oi, put_oi, fut_hl, legacy=False):
    t = cal.years_to_expiry(cal.session_bounds(d)[1], expiry)
    rows = []
    base = round(spot / 100) * 100  # NSE strikes sit on round hundreds
    for k in (base - 200, base - 100, base, base + 100, base + 200):
        for ot in (OptionType.CE, OptionType.PE):
            px = price(spot, k, t, R, iv, ot)
            oi = (call_oi if ot is OptionType.CE else put_oi).get(k, 1000)
            rows.append(
                FoEodRecord(
                    trade_date=d,
                    key=InstrumentKey(exchange="NSE", underlying="BANKNIFTY", expiry=expiry, strike=k, option_type=ot),
                    open=px,
                    high=px,
                    low=px,
                    close=px,
                    settle=px,
                    underlying_price=None if legacy else spot,
                    oi=oi,
                    oi_change=0,
                    volume=1,
                    volume_unit="units",
                    lot_size=30,
                )
            )
    rows.append(
        FoEodRecord(
            trade_date=d,
            key=InstrumentKey.parse(f"NSE:BANKNIFTY:{expiry}"),
            open=spot,
            high=fut_hl[0],
            low=fut_hl[1],
            close=spot,
            settle=spot,
            oi=1,
            oi_change=0,
            volume=1,
            volume_unit="units",
        )
    )
    return rows


EXP = date(2026, 10, 27)
DAYS = [
    # day, spot, iv, call OI wall, put OI wall, future (high, low)
    (date(2026, 10, 19), 10000, 0.16, {10200: 9000}, {9800: 9000}, (10050, 9950)),
    (date(2026, 10, 20), 10050, 0.16, {10200: 9000}, {9900: 9000}, (10150, 9900)),  # inside 1s; walls held
    (
        date(2026, 10, 21),
        10400,
        0.16,
        {10500: 9000},
        {10200: 9000},
        (10450, 10000),
    ),  # +350: outside 2s; call wall broke
    (date(2026, 10, 22), 10380, 0.16, {10500: 9000}, {10200: 9000}, (10480, 10300)),  # inside; held
]


@pytest.fixture
def eod():
    return [r for d, s, iv, c, p, hl in DAYS for r in eod_day(d, s, iv, EXP, c, p, hl)]


def test_backtest_expected_range(eod):
    res = run_backtest("expected_range", eod, "BANKNIFTY", DAYS[0][0], DAYS[-1][0], R)
    assert res.n_days == 3 and res.metrics["avg_atm_iv"] == pytest.approx(0.16, abs=1e-3)
    # 1-day sigma at 16% IV on ~10,000 = ~84 pts: moves +50 (in), +350 (out of 2s), -20 (in)
    assert res.metrics["containment_1s"] == pytest.approx(2 / 3) and res.metrics["containment_2s"] == pytest.approx(
        2 / 3
    )
    assert res.source == "nse_bhavcopy" and any("anecdotal" in n for n in res.notes)


def test_backtest_oi_levels(eod):
    res = run_backtest("oi_levels", eod, "BANKNIFTY", DAYS[0][0], DAYS[-1][0], R)
    assert res.n_days == 3
    assert res.metrics["resistance_held"] == pytest.approx(2 / 3)  # broken on 10-21
    assert res.metrics["support_held"] == pytest.approx(3 / 3)
    assert any("proxied" in n for n in res.notes)


def test_backtest_skips_legacy_days_and_rejects_unknown():
    rows = eod_day(date(2024, 1, 2), 48000, 0.15, date(2024, 1, 3), {}, {}, (48100, 47900), legacy=True)
    res = run_backtest("expected_range", rows, "BANKNIFTY", date(2024, 1, 1), date(2024, 1, 5), R)
    assert res.n_days == 0 and res.n_skipped == 1 and "legacy" in res.notes[1]
    with pytest.raises(ValueError, match="unknown strategy"):
        run_backtest("momentum", rows, "BANKNIFTY", date(2024, 1, 1), date(2024, 1, 5))
