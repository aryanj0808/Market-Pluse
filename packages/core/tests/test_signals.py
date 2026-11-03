"""Fusion, risk, zones and signal logging."""

from datetime import date, timedelta

import pytest

from mp_core import market_calendar as cal
from mp_core.analytics.levels import Level, LevelKind, vwap
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
FREE = {"cost_bps": 0, "slippage_bps": 0, "brokerage_per_order": 0}  # isolates the geometry from costs


@pytest.fixture(scope="module")
async def snap(synth_dir):
    p = ReplayProvider(synth_dir, SimClock(NOW))
    return build_snapshot(await gather_inputs(p, SYM, NOW), NOW, rate=0.065, version=3)


@pytest.fixture(scope="module")
async def tradeable(synth_dir):
    """A moment where a bullish setup clears reward:risk even after costs (seed 2)."""
    t = ist(date(2026, 9, 21), 14, 0)
    return build_snapshot(await gather_inputs(ReplayProvider(synth_dir, SimClock(t)), SYM, t), t, rate=0.065)


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
    def test_entry_stop_targets_sizing(self, tradeable, fixed, scores, up):
        fixed(*scores)
        risk_cfg = RiskConfig(capital=5_000_000, max_risk_pct=1, **(FREE if up else {}))
        sig = fuse(tradeable, risk_cfg)
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
        cost = risk_cfg.cost_points(sig.spot, 30)
        reward, risk = abs(sig.targets[0].price - sig.entry), abs(sig.entry - sig.stop_loss)
        assert sig.reward_risk == pytest.approx((reward - cost) / (risk + cost))
        s = sig.sizing
        assert s.lot_size == 30 and s.risk_per_lot == pytest.approx((risk + cost) * 30)
        assert s.costs_per_lot == pytest.approx(cost * 30)
        assert s.lots == int(s.max_loss // s.risk_per_lot) and s.loss_at_stop <= s.max_loss

    def test_loss_budget_blocks(self, snap, fixed):
        fixed(0.8, 0.6, 0.4)
        sig = fuse(snap, RiskConfig(capital=10_000, max_risk_pct=0.5))  # INR 50 budget
        assert sig.status == "no_signal" and any("loss budget" in r for r in sig.no_signal_reasons)

    def test_costs_shrink_reward_and_widen_risk(self, tradeable, fixed):
        fixed(0.8, 0.6, 0.4)
        free = fuse(tradeable, RiskConfig(5_000_000, 1, cost_bps=0, slippage_bps=0, brokerage_per_order=0))
        paid = fuse(tradeable, RiskConfig(5_000_000, 1))
        assert free.status == "active" and paid.reward_risk < free.reward_risk
        assert free.reward_risk == pytest.approx(
            abs(free.targets[0].price - free.entry) / abs(free.entry - free.stop_loss)
        )

    def test_cost_points(self):
        r = RiskConfig(cost_bps=3, slippage_bps=1, brokerage_per_order=20)
        # 50,000 x 5 bps = 25 pts, plus 2 x INR 20 over a 30-unit lot
        assert r.cost_points(50_000, 30) == pytest.approx(25 + 40 / 30)

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
        assert a == b and a.source is Source.SYNTHETIC and a.rules_version == "fusion-v2"
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


class TestV2:
    """fusion-v2: one price scale, broker lot size, futures order prices, holidays."""

    async def test_vwap_levels_are_on_the_spot_scale(self, synth_dir, snap):
        inputs = await gather_inputs(ReplayProvider(synth_dir, SimClock(NOW)), SYM, NOW)
        today = [b for b in inputs.future_1m_today.value if b.ts.astimezone(cal.IST).date() == NOW.date()]
        fut_vwap = vwap(today)
        basis = inputs.future_quote.value.ltp - inputs.quote.value.ltp
        assert abs(basis) > 1  # the synthetic future trades at a premium, so the shift is visible
        got = {lv.basis: lv.price for lv in snap.levels.value}
        assert got["vwap.session"] == pytest.approx(fut_vwap.vwap - basis)
        assert got["vwap.upper_1sd"] == pytest.approx(fut_vwap.upper_1sd - basis)

    async def test_vwap_needs_the_futures_quote(self, synth_dir):
        inputs = await gather_inputs(ReplayProvider(synth_dir, SimClock(NOW)), SYM, NOW)
        s = build_snapshot(inputs.model_copy(update={"future_quote": None}), NOW, rate=0.065)
        assert not any(lv.basis.startswith("vwap.") for lv in s.levels.value)  # never on the wrong scale
        assert any("levels.vwap: no futures quote" in e for e in s.errors)

    def test_futures_order_prices_are_shifted_and_on_tick(self, tradeable, fixed):
        fixed(0.8, 0.6, 0.4)
        sig = fuse(tradeable, RiskConfig(capital=5_000_000, max_risk_pct=1, **FREE))
        assert sig.status == "active" and sig.sizing is not None
        s, basis = sig.sizing, tradeable.future.value.basis_points
        assert s.future_entry == pytest.approx(s.entry + basis, abs=0.05)
        assert s.future_stop_loss == pytest.approx(s.stop_loss + basis, abs=0.05)
        for px in (s.future_entry, s.future_stop_loss):
            assert abs(px / 0.05 - round(px / 0.05)) < 1e-6
        assert s.future_stop_loss <= s.stop_loss + basis  # bullish: stop never tighter after rounding

    def test_broker_lot_size_wins_over_registry(self, tradeable, fixed):
        fixed(0.8, 0.6, 0.4)
        fut = tradeable.future.model_copy(update={"value": tradeable.future.value.model_copy(update={"lot_size": 35})})
        sig = fuse(tradeable.model_copy(update={"future": fut}), RiskConfig(capital=5_000_000, max_risk_pct=1, **FREE))
        assert sig.sizing.lot_size == 35
        assert not any(r.input == "registry.lot_size" for r in sig.reasons)

    def test_unverified_registry_lot_size_is_cited(self, tradeable, fixed):
        fixed(0.8, 0.6, 0.4)
        sig = fuse(tradeable, RiskConfig(capital=5_000_000, max_risk_pct=1, **FREE))
        assert any(r.input == "registry.lot_size" for r in sig.reasons)

    def test_no_signal_on_a_holiday(self, snap, fixed):
        fixed(0.8, 0.6, 0.4)
        holiday = snap.model_copy(update={"computed_at": ist(date(2026, 10, 2), 11, 15)})  # Gandhi Jayanti
        sig = fuse(holiday)
        assert sig.status == "no_signal" and any("not an NSE trading day" in r for r in sig.no_signal_reasons)


def test_to_tick():
    assert fusion._to_tick(100.03, 0.05, -1) == pytest.approx(100.0)
    assert fusion._to_tick(100.03, 0.05, 1) == pytest.approx(100.05)
    assert fusion._to_tick(100.05, 0.05, -1) == pytest.approx(100.05)  # already on the grid
