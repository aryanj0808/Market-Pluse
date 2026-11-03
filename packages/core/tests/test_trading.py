"""Paper trading: order validation, duplicate-order safety, partial fills, P&L, risk gate, kill
switch, reconciliation, audit trail - and whole trades driven by real replay snapshots."""

from datetime import UTC, date, datetime, time, timedelta

import pytest
from pydantic import ValidationError

from mp_core.clock import SimClock
from mp_core.providers.replay import ReplayProvider
from mp_core.signals.fusion import RiskConfig, fuse
from mp_core.snapshot import build_snapshot, gather_inputs
from mp_core.store import MemoryKV
from mp_core.trading.audit import JsonlAudit, MemoryAudit, verify
from mp_core.trading.killswitch import KillSwitch
from mp_core.trading.models import Fill, HaltState, OrderIntent, intent_key
from mp_core.trading.oms import OrderManager, OrderUncertain
from mp_core.trading.paper_broker import PaperBroker
from mp_core.trading.portfolio import Portfolio
from mp_core.trading.risk import RiskGate, RiskLimits
from mp_core.trading.trader import Trader, TraderConfig

from conftest import ist

FUT = "NSE:BANKNIFTY:2026-09-29"
T0 = ist(date(2026, 9, 21), 11, 0)


def intent(leg="entry", side="BUY", qty=60, order_type="LIMIT", price=50_000.0, trigger=None, key="a", **kw):
    return OrderIntent(
        intent_id=intent_key("t1", leg, key),
        trade_id="t1",
        leg=leg,
        instrument=FUT,
        side=side,
        qty=qty,
        lot_size=30,
        tick_size=0.05,
        order_type=order_type,
        price=price if order_type == "LIMIT" else None,
        trigger=trigger,
        created_at=T0,
        reason="test",
        **kw,
    )


@pytest.fixture
def clock():
    return SimClock(T0)


@pytest.fixture
def paper(clock):
    return PaperBroker(clock, slippage_bps=0)


@pytest.fixture
def oms(paper):
    return OrderManager(paper, MemoryAudit(), timeout=1, attempts=3)


# ---------------------------------------------------------------------- models
class TestModels:
    def test_whole_lots_only(self):
        with pytest.raises(ValidationError, match="whole number of lots"):
            intent(qty=45)

    def test_tick_grid(self):
        with pytest.raises(ValidationError, match="tick grid"):
            intent(price=50_000.03)
        assert intent(price=50_000.05).price == 50_000.05

    def test_order_type_fields(self):
        with pytest.raises(ValidationError):
            intent(order_type="SL-M", trigger=None)
        assert intent(order_type="SL-M", trigger=49_900.0).trigger == 49_900.0

    def test_intent_key_fits_a_broker_tag(self):
        k = intent_key("some-trade", "entry", "")
        assert len(k) == 16 and k == intent_key("some-trade", "entry", "")


# ---------------------------------------------------------------------- order manager
class TestOrderManager:
    async def test_same_intent_twice_is_one_order(self, oms, paper):
        i = intent()
        a, b = await oms.submit(i, T0), await oms.submit(i, T0)
        assert a.broker_order_id == b.broker_order_id and paper.placed == 1
        assert oms.stats["duplicates_suppressed"] == 1

    async def test_lost_response_is_adopted_not_resent(self, oms, paper):
        paper.inject("timeout_after_place")
        o = await oms.submit(intent(), T0)
        assert paper.placed == 1 and o.status == "open" and oms.stats["adopted_after_timeout"] == 1
        assert "order_adopted" in oms.audit.kinds()

    async def test_timeout_before_placing_is_retried_once(self, oms, paper):
        paper.inject("timeout_before_place", "error")
        o = await oms.submit(intent(), T0)
        assert paper.placed == 1 and o.status == "open"
        assert oms.audit.kinds().count("order_retry") == 2

    async def test_persistent_failure_is_uncertain(self, oms, paper):
        paper.inject("error", "error", "error")
        with pytest.raises(OrderUncertain):
            await oms.submit(intent(), T0)
        assert paper.placed == 0

    async def test_resubmit_after_uncertain_never_resends_blindly(self, oms, paper):
        paper.inject("error", "error", "error")
        with pytest.raises(OrderUncertain):
            await oms.submit(intent(), T0)
        with pytest.raises(OrderUncertain, match="unknown outcome"):
            await oms.submit(intent(), T0)
        assert paper.placed == 0

    async def test_broker_unreachable_is_uncertain(self, oms, paper, monkeypatch):
        async def down():
            raise ConnectionError("broker down")

        paper.inject("timeout_before_place")
        monkeypatch.setattr(paper, "orders", down)
        with pytest.raises(OrderUncertain, match="cannot list"):
            await oms.submit(intent(), T0)

    async def test_partial_fills_become_separate_fills(self, clock):
        paper = PaperBroker(clock, slippage_bps=0, max_fill_per_update=30)
        oms = OrderManager(paper, MemoryAudit())
        await oms.submit(intent(qty=90, price=50_000.0), T0)
        paper.on_price(FUT, 49_990.0)
        f1 = await oms.sync(T0)
        paper.on_price(FUT, 49_995.0)
        paper.on_price(FUT, 49_980.0)
        f2 = await oms.sync(T0)
        assert [f.qty for f in f1] == [30] and sum(f.qty for f in f2) == 60
        assert all(f.price == 50_000.0 for f in f1 + f2)  # a limit fills at its price, never better
        assert oms.orders[intent().intent_id].status == "filled"

    async def test_freeze_quantity_rejection_counts(self, clock):
        oms = OrderManager(PaperBroker(clock, freeze_qty=60), MemoryAudit())
        o = await oms.submit(intent(qty=90), T0)
        assert o.status == "rejected" and "freeze" in o.reject_reason and oms.consecutive_rejects == 1


class TestPaperFills:
    async def test_stop_fills_at_the_worse_price_with_slippage(self, clock):
        paper = PaperBroker(clock, slippage_bps=1)
        await paper.place(intent(leg="stop", side="SELL", order_type="SL-M", trigger=50_000.0))
        paper.on_price(FUT, 50_010.0)
        assert (await paper.orders())[0].status == "open"
        paper.on_price(FUT, 49_980.0)  # gapped through the trigger
        o = (await paper.orders())[0]
        assert o.status == "filled" and o.avg_price == pytest.approx(49_980.0 * (1 - 1e-4))

    async def test_marketable_limit_fills_at_market(self, paper):
        paper.on_price(FUT, 49_950.0)
        o = await paper.place(intent(price=50_000.0))
        assert o.status == "filled" and o.avg_price == 49_950.0


# ---------------------------------------------------------------------- portfolio
def fill(side, qty, price, leg="entry", oid="o1", trade="t1"):
    return Fill(
        fill_id=f"{oid}:{qty}",
        broker_order_id=oid,
        intent_id="i" * 16,
        trade_id=trade,
        leg=leg,
        instrument=FUT,
        side=side,
        qty=qty,
        price=price,
        ts=T0,
    )


class TestPortfolio:
    def test_round_trip_pnl_and_charges(self):
        b = Portfolio(cost_bps=0, brokerage_per_order=20)
        b.apply(fill("BUY", 60, 50_000))
        b.mark(FUT, 50_050)
        assert b.unrealized() == pytest.approx(60 * 50) and b.open_qty() == 60
        r = b.apply(fill("SELL", 60, 50_100, leg="target", oid="o2"))
        assert r == pytest.approx(60 * 100) and b.open_qty() == 0
        assert b.net_today() == pytest.approx(6000 - 40) and b.trades_today == 1

    def test_partial_close_keeps_average(self):
        b = Portfolio(cost_bps=0, brokerage_per_order=0)
        b.apply(fill("SELL", 60, 50_000))  # short
        b.apply(fill("BUY", 30, 49_900, leg="exit", oid="o2"))
        p = b.positions[FUT]
        assert p.qty == -30 and p.avg_price == 50_000 and p.realized_pnl == pytest.approx(3000)

    def test_drawdown_from_peak(self):
        b = Portfolio(cost_bps=0, brokerage_per_order=0)
        b.apply(fill("BUY", 30, 50_000))
        b.mark(FUT, 50_200)
        b.mark(FUT, 49_900)
        assert b.drawdown() == pytest.approx(30 * 300)

    def test_brokerage_once_per_order_not_per_partial(self):
        b = Portfolio(cost_bps=0, brokerage_per_order=20)
        b.apply(fill("BUY", 30, 50_000))
        b.apply(fill("BUY", 30, 50_000))
        assert b.charges_today == 20


# ---------------------------------------------------------------------- risk gate
LIMITS = RiskLimits(max_loss_per_trade=5_000, daily_loss_limit=10_000, max_drawdown=25_000, max_open_lots=2)
OK = HaltState(halted=False)


class TestRiskGate:
    def test_approves_and_caps_lots(self):
        d = RiskGate(LIMITS).check_entry(intent(qty=150), 1_500, Portfolio(), T0, OK)
        assert d.approved and d.qty == 60  # 5 lots asked, max_open_lots 2

    def test_per_trade_loss_caps_lots(self):
        d = RiskGate(LIMITS).check_entry(intent(qty=60), 3_000, Portfolio(), T0, OK)
        assert d.approved and d.qty == 30  # two lots would risk 6,000 > 5,000

    @pytest.mark.parametrize(
        "now, why",
        [
            (ist(date(2026, 9, 21), 15, 5), "outside entry hours"),
            (ist(date(2026, 9, 21), 9, 0), "outside entry hours"),
            (ist(date(2026, 10, 2), 11, 0), "not a trading day"),
            (ist(date(2026, 9, 20), 11, 0), "not a trading day"),  # Sunday
        ],
    )
    def test_time_rules(self, now, why):
        d = RiskGate(LIMITS).check_entry(intent(), 1_000, Portfolio(), now, OK)
        assert not d.approved and any(why in r for r in d.reasons)

    def test_halt_blocks(self):
        d = RiskGate(LIMITS).check_entry(
            intent(), 1_000, Portfolio(), T0, HaltState(halted=True, reason="x", by="manual:me")
        )
        assert not d.approved and "trading halted" in d.reasons[0]

    def test_daily_loss_and_position_and_trade_count(self):
        b = Portfolio(cost_bps=0, brokerage_per_order=0)
        b.apply(fill("BUY", 30, 50_000))
        b.mark(FUT, 49_600)  # -12,000 today and a position open
        b.trades_today = 3
        d = RiskGate(LIMITS).check_entry(intent(), 1_000, b, T0, OK)
        text = " | ".join(d.reasons)
        assert not d.approved and "daily loss" in text and "already open" in text and "3 trades today" in text

    def test_remaining_daily_budget_caps_lots(self):
        b = Portfolio(cost_bps=0, brokerage_per_order=0)
        b.apply(fill("BUY", 30, 50_000))
        b.apply(fill("SELL", 30, 49_750, leg="stop", oid="o2"))  # -7,500 realized, flat
        d = RiskGate(LIMITS).check_entry(intent(qty=60), 2_000, b, T0, OK)
        assert d.approved and d.qty == 30  # 2,500 left today: one lot of 2,000
        d = RiskGate(LIMITS).check_entry(intent(qty=60), 3_000, b, T0, OK)
        assert not d.approved and "remaining budget" in d.reasons[0]

    def test_freeze_quantity_caps(self):
        lim = LIMITS.model_copy(update={"max_open_lots": 50, "max_loss_per_trade": 1e9, "freeze_qty": 90})
        d = RiskGate(lim).check_entry(intent(qty=600), 10, Portfolio(), T0, OK)
        assert d.approved and d.qty == 90


# ---------------------------------------------------------------------- kill switch / audit
class TestKillSwitch:
    async def test_first_reason_wins_and_only_people_resume(self):
        ks = KillSwitch(MemoryKV())
        await ks.halt("daily loss", "auto:breaker", T0)
        st = await ks.halt("something later", "manual:me", T0, flatten=True)
        assert st.reason == "daily loss" and st.flatten  # later flatten request honoured
        with pytest.raises(ValueError):
            await ks.resume("auto:breaker")
        assert not (await ks.resume("manual:me")).halted


class TestAudit:
    def test_chain_detects_tampering(self, tmp_path):
        a = JsonlAudit(tmp_path)
        for i in range(5):
            a.record("fill", T0, n=i)
        path = tmp_path / "2026-09-21.jsonl"
        assert verify(path) == (True, 5, "intact")
        lines = path.read_text().splitlines()
        lines[2] = lines[2].replace('"n": 2', '"n": 9')
        path.write_text("\n".join(lines) + "\n")
        ok, at, _ = verify(path)
        assert not ok and at == 4  # the line after the edited one no longer matches

    def test_chain_survives_restart(self, tmp_path):
        JsonlAudit(tmp_path).record("a", T0)
        JsonlAudit(tmp_path).record("b", T0)
        assert verify(tmp_path / "2026-09-21.jsonl")[0]


# ---------------------------------------------------------------------- whole trades on replay snapshots
@pytest.fixture(scope="module")
async def setup_snap(synth_dir):
    """09-21 14:00 on the seed-2 dataset: an active bullish signal that clears costs."""
    t = ist(date(2026, 9, 21), 14, 0)
    s = build_snapshot(await gather_inputs(ReplayProvider(synth_dir, SimClock(t)), "NSE:BANKNIFTY", t), t, rate=0.065)
    s = s.model_copy(update={"signal": fuse(s, RiskConfig())})
    assert s.signal.status == "active" and s.signal.bias == "bullish" and s.signal.sizing.lots >= 1
    return s


class Rig:
    def __init__(self, snap, limits=None, **paper_kw):
        self.snap = snap
        self.t = snap.computed_at
        self.clock = SimClock(self.t)
        self.paper = PaperBroker(self.clock, slippage_bps=0, **paper_kw)
        self.audit = MemoryAudit()
        self.kill = KillSwitch(MemoryKV())
        lim = limits or RiskLimits(
            max_loss_per_trade=10_000, daily_loss_limit=20_000, max_drawdown=50_000, max_open_lots=2
        )
        self.trader = Trader(
            "NSE:BANKNIFTY",
            OrderManager(self.paper, self.audit, timeout=1),
            Portfolio(),
            RiskGate(lim),
            self.kill,
            self.audit,
            TraderConfig(reconcile_every=timedelta(seconds=0)),
        )
        self.sz = snap.signal.sizing

    async def tick(self, fut_price, *, after=timedelta(seconds=1), **update):
        self.t += after
        self.clock.set(self.t)
        self.paper.on_price(self.sz.instrument, fut_price, self.t)
        fv = self.snap.future.value.model_copy(update={"ltp": fut_price})
        s = self.snap.model_copy(
            update={"computed_at": self.t, "future": self.snap.future.model_copy(update={"value": fv}), **update}
        )
        await self.trader.on_snapshot(s)
        return s


class TestTrader:
    async def test_entry_bracket_target(self, setup_snap):
        r = Rig(setup_snap)
        fut = setup_snap.future.value.ltp
        await r.tick(fut, after=timedelta(0))  # signal logged -> entry limit placed
        assert r.trader.trade is not None and r.paper.placed == 1
        await r.tick(r.sz.future_entry - 5)  # trades through the entry
        await r.tick(r.sz.future_entry - 2)  # bracket placed on the next cycle
        legs = sorted(o.intent.leg for o in r.trader.oms.open_orders())
        assert legs == ["stop", "target"] and r.trader.book.open_qty() == r.sz.lots * 30
        await r.tick(r.trader.trade.target_px + 1)  # target fills
        await r.tick(r.trader.trade.target_px if r.trader.trade else fut)  # OCO: stop cancelled, trade closed
        assert r.trader.trade is None and r.trader.book.open_qty() == 0 and not r.trader.oms.open_orders()
        assert r.trader.book.realized_today > 0 and r.trader.book.charges_today > 0
        kinds = r.audit.kinds()
        for k in ("risk_decision", "signal_acted", "order_submit", "fill", "reconcile", "trade_closed"):
            assert k in kinds, k
        assert not (await r.kill.state()).halted

    async def test_stop_loss_closes_and_loss_is_bounded(self, setup_snap):
        r = Rig(setup_snap)
        await r.tick(setup_snap.future.value.ltp, after=timedelta(0))
        await r.tick(r.sz.future_entry - 1)
        await r.tick(r.sz.future_entry)
        await r.tick(r.sz.future_stop_loss - 1)  # stop triggers
        await r.tick(r.sz.future_stop_loss - 2)
        b = r.trader.book
        assert r.trader.trade is None and b.open_qty() == 0
        assert -b.net_today() <= r.sz.loss_at_stop + 2 * r.sz.lots * 30 + 1  # plan + the 1-2 pts we gapped

    async def test_budget_too_small_for_one_lot_is_refused(self, setup_snap):
        lim = RiskLimits(max_loss_per_trade=10_000, daily_loss_limit=1_000, max_drawdown=50_000, max_open_lots=2)
        r = Rig(setup_snap, limits=lim)
        await r.tick(setup_snap.future.value.ltp, after=timedelta(0))
        assert r.paper.placed == 0  # one lot risks ~1,876 incl. costs; the whole day may lose 1,000
        d = next(x for x in r.audit.rows if x["kind"] == "risk_decision")
        assert not d["approved"] and "remaining budget" in d["reasons"][0]

    async def test_gap_through_stop_trips_daily_loss_breaker(self, setup_snap):
        lim = RiskLimits(max_loss_per_trade=10_000, daily_loss_limit=4_000, max_drawdown=50_000, max_open_lots=2)
        r = Rig(setup_snap, limits=lim)
        await r.tick(setup_snap.future.value.ltp, after=timedelta(0))
        await r.tick(r.sz.future_entry - 1)
        await r.tick(r.sz.future_entry + 1)
        assert r.trader.book.open_qty() == 60
        await r.tick(r.sz.future_entry - 100)  # gaps 70 points through the stop
        await r.tick(r.sz.future_entry - 100)
        st = await r.kill.state()
        assert st.halted and st.by == "auto:breaker" and "daily loss" in st.reason
        assert r.trader.book.open_qty() == 0 and "breaker" in r.audit.kinds()

    async def test_manual_halt_cancels_pending_entry(self, setup_snap):
        r = Rig(setup_snap)
        await r.tick(setup_snap.future.value.ltp, after=timedelta(0))
        await r.kill.halt("going to lunch", "manual:me", r.t)
        await r.tick(setup_snap.future.value.ltp)
        assert r.trader.trade is None and not r.trader.oms.open_orders()
        await r.tick(r.sz.future_entry - 5)  # would have filled
        assert r.trader.book.open_qty() == 0

    async def test_reconciliation_mismatch_halts_without_flattening(self, setup_snap):
        r = Rig(setup_snap)
        await r.tick(setup_snap.future.value.ltp, after=timedelta(0))
        r.paper.adjust_position(r.sz.instrument, 30)  # someone traded by hand
        await r.tick(setup_snap.future.value.ltp)
        assert not (await r.kill.state()).halted  # one mismatch may be a fill in flight
        await r.tick(setup_snap.future.value.ltp)
        st = await r.kill.state()
        assert st.halted and "differ" in st.reason and not st.flatten

    async def test_unreachable_broker_halts(self, setup_snap, monkeypatch):
        r = Rig(setup_snap)
        await r.tick(setup_snap.future.value.ltp, after=timedelta(0))

        async def down():
            raise ConnectionError("broker down")

        monkeypatch.setattr(r.paper, "orders", down)
        await r.tick(setup_snap.future.value.ltp)
        st = await r.kill.state()
        assert st.halted and st.by == "auto:oms" and "order_uncertain" in r.audit.kinds()

    async def test_square_off_time(self, setup_snap):
        r = Rig(setup_snap)
        await r.tick(setup_snap.future.value.ltp, after=timedelta(0))
        await r.tick(r.sz.future_entry - 1)
        await r.tick(r.sz.future_entry)
        assert r.trader.book.open_qty() > 0
        until = datetime.combine(r.t.astimezone(ist(T0.date(), 0, 0).tzinfo).date(), time(15, 20), r.t.tzinfo)
        await r.tick(r.sz.future_entry + 1, after=until - r.t)
        await r.tick(r.sz.future_entry + 1)
        assert r.trader.book.open_qty() == 0 and r.trader.trade is None

    async def test_partial_fills_resize_the_bracket(self, setup_snap):
        r = Rig(setup_snap, max_fill_per_update=30)
        if r.sz.lots < 2:
            pytest.skip("needs a 2-lot signal")
        await r.tick(setup_snap.future.value.ltp, after=timedelta(0))
        await r.tick(r.sz.future_entry - 1)  # first lot
        await r.tick(r.sz.future_entry + 1)
        stops = [o for o in r.trader.oms.open_orders() if o.intent.leg == "stop"]
        assert [o.intent.qty for o in stops] == [30]
        await r.tick(r.sz.future_entry - 1)  # second lot
        await r.tick(r.sz.future_entry + 1)
        stops = [o for o in r.trader.oms.open_orders() if o.intent.leg == "stop"]
        assert [o.intent.qty for o in stops] == [60]  # resized, the 30-lot stop cancelled

    async def test_stale_data_with_position_halts(self, setup_snap):
        r = Rig(setup_snap)
        await r.tick(setup_snap.future.value.ltp, after=timedelta(0))
        await r.tick(r.sz.future_entry - 1)
        await r.tick(r.sz.future_entry)
        await r.tick(r.sz.future_entry, stale_sections=["quote"])
        await r.tick(r.sz.future_entry, after=timedelta(seconds=61), stale_sections=["quote"])
        st = await r.kill.state()
        assert st.halted and "stale" in st.reason and not st.flatten

    async def test_halted_before_signal_means_no_order(self, setup_snap):
        r = Rig(setup_snap)
        await r.kill.halt("not today", "manual:me", datetime(2026, 9, 21, tzinfo=UTC))
        await r.tick(setup_snap.future.value.ltp, after=timedelta(0))
        assert r.paper.placed == 0
        d = next(x for x in r.audit.rows if x["kind"] == "risk_decision")
        assert not d["approved"] and "halted" in d["reasons"][0]
