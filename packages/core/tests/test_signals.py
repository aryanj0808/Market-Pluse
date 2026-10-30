"""Fusion, risk, zones and signal logging."""

from datetime import date, timedelta

import pytest

from mp_core.analytics.levels import Level, LevelKind
from mp_core.clock import SimClock
from mp_core.models import Source
from mp_core.providers.replay import ReplayProvider
from mp_core.signals import fusion
from mp_core.signals.fusion import WEIGHTS, RiskConfig, _zones, fuse, horizon_end
from mp_core.signals.logger import SignalLogger
from mp_core.signals.models import ComponentScore, Reason
from mp_core.snapshot import build_snapshot, gather_inputs

from conftest import ist

SYM = "NSE:BANKNIFTY"
NOW = ist(date(2026, 9, 22), 11, 15)


@pytest.fixture(scope="module")
async def snap(synth_dir):
    p = ReplayProvider(synth_dir, SimClock(NOW))
    return build_snapshot(await gather_inputs(p, SYM, NOW), NOW, rate=0.065, version=3)


def comp(name, score, available=True):
    return ComponentScore(
        name=name,
        score=score,
        weight=WEIGHTS[name] if available else 0,
        available=available,
        reasons=[Reason(component=name, input=f"test.{name}", value=score, effect=score, text="t")],
    )


@pytest.fixture
def fixed(monkeypatch):
    """Pin the three component scores so the aggregation rules can be tested exactly."""

    def set_scores(tech, opts, news, news_available=True):
        monkeypatch.setattr(fusion, "_technical", lambda s: comp("technical", tech))
        monkeypatch.setattr(fusion, "_options", lambda s: comp("options", opts))
        monkeypatch.setattr(fusion, "_news", lambda s: comp("news", news, news_available))

    return set_scores


class TestAggregation:
    def test_composite_and_bias(self, snap, fixed):
        fixed(0.6, 0.4, 0.2)
        sig = fuse(snap)
        expected = (0.35 * 0.6 + 0.35 * 0.4 + 0.30 * 0.2) / 1.0
        assert sig.composite == pytest.approx(expected) and sig.bias == "bullish"
        assert sig.confidence == pytest.approx(min(1, expected / 0.6) * 1.0 * 1.0, abs=1e-3) or sig.status != "active"

    def test_neutral_band_means_no_signal(self, snap, fixed):
        fixed(0.2, 0.1, -0.1)
        sig = fuse(snap)
        assert sig.bias == "neutral" and sig.status == "no_signal" and sig.entry is None
        assert any("neutral band" in r for r in sig.no_signal_reasons)
        assert sig.buy_zone is not None and sig.sell_zone is not None  # context still given

    def test_conflict_means_no_signal(self, snap, fixed):
        fixed(0.9, 0.8, -0.5)
        sig = fuse(snap)
        assert sig.status == "no_signal" and sig.bias == "bullish"
        assert any("conflicting inputs: technical, options bullish vs news bearish" in r for r in sig.no_signal_reasons)

    def test_missing_component_lowers_confidence_not_direction(self, snap, fixed):
        fixed(0.6, 0.6, 0.0, news_available=False)
        sig = fuse(snap)
        assert sig.composite == pytest.approx(0.6) and sig.bias == "bullish"
        completeness = (0.35 + 0.35) / 1.0
        assert sig.confidence <= completeness + 1e-9

    def test_stale_critical_input_blocks(self, snap, fixed):
        fixed(0.8, 0.6, 0.4)
        sig = fuse(snap.model_copy(update={"stale_sections": ["quote"]}))
        assert sig.status == "no_signal" and any("stale inputs: quote" in r for r in sig.no_signal_reasons)
        assert sig.entry is None and sig.targets == [] and sig.sizing is None

    def test_missing_levels_blocks(self, snap, fixed):
        fixed(0.8, 0.6, 0.4)
        sig = fuse(snap.model_copy(update={"levels": None}))
        assert sig.status == "no_signal" and "missing input: levels" in sig.no_signal_reasons


class TestTrade:
    @pytest.mark.parametrize("scores, up", [((0.8, 0.6, 0.4), True), ((-0.8, -0.6, -0.4), False)])
    def test_entry_stop_targets_sizing(self, snap, fixed, scores, up):
        fixed(*scores)
        sig = fuse(snap, RiskConfig(capital=5_000_000, max_risk_pct=1))
        assert sig.reward_risk is not None  # computed even when the signal is blocked
        if sig.status != "active":  # blocked signals never publish entry/stop/targets
            assert sig.entry is None and sig.stop_loss is None and sig.targets == []
            assert any("reward:risk" in r or "loss budget" in r for r in sig.no_signal_reasons)
            return
        zone = sig.buy_zone if up else sig.sell_zone
        assert sig.entry == pytest.approx((zone.low + zone.high) / 2)
        if up:
            assert sig.stop_loss < zone.low <= sig.entry < sig.targets[0].price
        else:
            assert sig.stop_loss > zone.high >= sig.entry > sig.targets[0].price
        assert sig.reward_risk == pytest.approx(abs(sig.targets[0].price - sig.entry) / abs(sig.entry - sig.stop_loss))
        s = sig.sizing
        assert s.lot_size == 30 and s.risk_per_lot == pytest.approx(abs(sig.entry - sig.stop_loss) * 30)
        assert s.lots == int(s.max_loss // s.risk_per_lot) and s.loss_at_stop <= s.max_loss

    def test_loss_budget_blocks(self, snap, fixed):
        fixed(0.8, 0.6, 0.4)
        sig = fuse(snap, RiskConfig(capital=10_000, max_risk_pct=0.5))  # INR 50 budget
        assert sig.status == "no_signal" and any("loss budget" in r for r in sig.no_signal_reasons)

    def test_risk_config_validated(self):
        with pytest.raises(ValueError):
            RiskConfig(capital=0)
        with pytest.raises(ValueError):
            RiskConfig(max_risk_pct=10)


class TestZones:
    def lv(self, price, basis, kind=LevelKind.REFERENCE):
        return Level(name=basis, price=price, kind=kind, basis=basis)

    def test_levels_are_judged_by_position(self):
        levels = [
            self.lv(9900, "oi.put_rank1", LevelKind.SUPPORT),
            self.lv(10080, "pivot.classic.S1", LevelKind.SUPPORT),
            self.lv(10100, "oi.call_rank1", LevelKind.RESISTANCE),
        ]
        buy, sell = _zones(levels, spot=10000, sigma=150)
        assert buy.basis == ["oi.put_rank1"] and buy.low < 9900 < buy.high
        assert sell.basis[0] in ("oi.call_rank1", "pivot.classic.S1")  # an "S1" above price is overhead supply
        assert sell.low > 10000

    def test_falls_back_to_expected_range(self):
        buy, sell = _zones([], spot=10000, sigma=100)
        assert buy.basis == sell.basis == ["expected_range"]
        assert (buy.low, buy.high, sell.low, sell.high) == (9940, 9960, 10040, 10060)


class TestRealSnapshot:
    def test_every_reason_cites_an_input(self, snap):
        sig = fuse(snap)
        assert sig.reasons and all(r.input and r.text for r in sig.reasons)
        bases = {lv.basis for lv in snap.levels.value}
        for r in sig.reasons:
            if r.input.startswith("levels[") and r.component == "technical" and r.input == "levels[vwap.session]":
                assert "vwap.session" in bases

    def test_deterministic_and_synthetic(self, snap):
        a, b = fuse(snap), fuse(snap)
        assert a == b and a.source is Source.SYNTHETIC and a.rules_version == "fusion-v1"
        assert a.signal_id != fuse(snap.model_copy(update={"version": 99})).signal_id

    def test_horizon_end(self, snap):
        s = fuse(snap)
        assert horizon_end(s) == ist(date(2026, 9, 22), 15, 30)
        late = s.model_copy(update={"ts": ist(date(2026, 9, 25), 16, 0), "horizon": "session"})  # Friday evening
        assert horizon_end(late) == ist(date(2026, 9, 28), 15, 30)
        nxt = s.model_copy(update={"horizon": "next_session"})
        assert horizon_end(nxt) == ist(date(2026, 9, 23), 15, 30)


class FakeSignalHistory:
    def __init__(self):
        self.rows = []

    async def record_signal(self, signal, inputs):
        self.rows.append((signal, inputs))


async def test_signal_logger_logs_changes_and_heartbeats(snap):
    hist = FakeSignalHistory()
    log = SignalLogger(hist)
    base = snap.model_copy(update={"signal": fuse(snap)})
    assert await log.maybe_log(base)
    assert not await log.maybe_log(base)  # same status/bias, < 15 min later
    later = base.signal.model_copy(update={"ts": base.signal.ts + timedelta(minutes=16)})
    assert await log.maybe_log(base.model_copy(update={"signal": later}))
    flipped = base.signal.model_copy(
        update={
            "ts": later.ts + timedelta(minutes=1),
            "bias": "bearish" if base.signal.bias != "bearish" else "bullish",
        }
    )
    assert await log.maybe_log(base.model_copy(update={"signal": flipped}))
    sig, inputs = hist.rows[0]
    assert {"quote", "expected_range", "levels", "front_expiry", "news", "stale_sections"} <= set(inputs)
    assert "strikes" not in inputs["front_expiry"]["value"]["iv"]  # per-strike IVs are not logged (bulk)
    assert inputs["front_expiry"]["value"]["iv"]["atm_iv"] is not None
    assert len(hist.rows) == 3
