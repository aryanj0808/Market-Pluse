"""The trader: snapshots in, orders out - signal -> risk gate -> order manager -> broker, with the
kill switch, breakers and reconciliation around it. Broker-agnostic: paper today, a live adapter
later on exactly this path.

Per snapshot, in this order:
  1. sync orders from the broker -> fills -> portfolio; reconcile against broker positions
     (before any new order this cycle, so in-flight fills cannot look like a mismatch)
  2. breakers: daily loss, drawdown, consecutive rejections, stale data with a position open,
     a reconciliation mismatch seen twice in a row -> halt (the kill switch)
  3. halted: cancel pending entries; flatten if the halt asks for it
  4. square-off at `flatten_time`; bracket upkeep (stop + target sized to the open quantity,
     one-cancels-other)
  5. signal: acted on only when the SignalLogger logs it (status/bias change, 15-min heartbeat) -
     the same moments the replay backtest trades on. A new logged signal replaces an unfilled
     entry; an active one opens a trade through the risk gate.

Every decision is written to the audit trail.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from mp_core import market_calendar as cal
from mp_core.registry import get_index
from mp_core.signals.logger import SignalLogger
from mp_core.signals.models import Signal
from mp_core.snapshot import MarketSnapshot
from mp_core.trading.audit import Audit
from mp_core.trading.killswitch import KillSwitch
from mp_core.trading.models import HaltState, OrderIntent, intent_key
from mp_core.trading.oms import OrderManager, OrderUncertain
from mp_core.trading.portfolio import Portfolio
from mp_core.trading.risk import RiskGate

STALE_SECTIONS = ("quote", "future", "levels", "expected_range")


@dataclass
class TraderConfig:
    reconcile_every: timedelta = timedelta(seconds=30)
    stale_halt_after: timedelta = timedelta(seconds=60)
    max_consecutive_rejects: int = 3


@dataclass
class ActiveTrade:
    trade_id: str
    signal_id: str
    bias: str
    instrument: str
    lot_size: int
    tick: float
    entry_id: str
    stop_px: float
    target_px: float
    stop_id: str | None = None
    target_id: str | None = None
    bracket_qty: int = 0
    closing: bool = False
    exits: int = 0
    opened: bool = False  # any entry quantity filled
    notes: list[str] = field(default_factory=list)

    @property
    def exit_side(self) -> str:
        return "SELL" if self.bias == "bullish" else "BUY"


class _NoHistory:
    async def record_signal(self, signal, inputs) -> None:
        return None


def _round_toward(price: float, tick: float, toward: float) -> float:
    steps = price / tick
    steps = int(steps) if price > toward else -int(-steps)  # truncate toward `toward`
    return round(steps * tick, 10)


class Trader:
    def __init__(
        self,
        symbol: str,
        oms: OrderManager,
        book: Portfolio,
        gate: RiskGate,
        kill: KillSwitch,
        audit: Audit,
        cfg: TraderConfig | None = None,
    ) -> None:
        self.symbol = symbol
        self.spec = get_index(symbol)
        self.oms = oms
        self.book = book
        self.gate = gate
        self.kill = kill
        self.audit = audit
        self.cfg = cfg or TraderConfig()
        self.signals = SignalLogger(_NoHistory())
        self.trade: ActiveTrade | None = None
        self.closed: list[dict] = []
        self._last_reconcile: datetime | None = None
        self._mismatches = 0
        self._stale_since: datetime | None = None
        self.halt = HaltState(halted=False)

    # ------------------------------------------------------------------ entry point
    async def on_snapshot(self, snap: MarketSnapshot) -> None:
        now = snap.computed_at
        self.book.roll_day(now)
        try:
            await self._step(snap, now)
        except OrderUncertain as ex:
            # the broker's state is unknown: stop everything, do not try to trade our way out
            self.audit.record("order_uncertain", now, error=str(ex))
            self.halt = await self.kill.halt(f"order state uncertain: {ex}", "auto:oms", now)

    async def _step(self, snap: MarketSnapshot, now: datetime) -> None:
        for f in await self.oms.sync(now):
            self.book.apply(f)
            if self.trade is not None and f.trade_id == self.trade.trade_id and f.leg == "entry":
                self.trade.opened = True
        if snap.future is not None:
            self.book.mark(snap.future.value.key.id, snap.future.value.ltp)
        await self._reconcile(now)

        self.halt = await self.kill.state()
        if not self.halt.halted:
            trip = self._breaker(snap, now)
            if trip is not None:
                reason, flatten = trip
                self.audit.record("breaker", now, reason=reason, flatten=flatten)
                self.halt = await self.kill.halt(reason, "auto:breaker", now, flatten=flatten)

        if self.halt.halted:
            await self._cancel_pending_entry(now, f"halted: {self.halt.reason}")
            if self.halt.flatten:
                await self._flatten(now, f"halted: {self.halt.reason}")

        if now.astimezone(cal.IST).time() >= self.gate.limits.flatten_time:
            await self._cancel_pending_entry(now, "square-off time")
            await self._flatten(now, "square-off time")

        await self._bracket(now)

        if await self.signals.maybe_log(snap) and snap.signal is not None:
            await self._on_signal(snap, snap.signal, now)

    # ------------------------------------------------------------------ breakers
    def _breaker(self, snap: MarketSnapshot, now: datetime) -> tuple[str, bool] | None:
        lim = self.gate.limits
        if self.book.net_today() <= -lim.daily_loss_limit:
            return f"daily loss limit: INR {-self.book.net_today():,.0f}", True
        if self.book.drawdown() >= lim.max_drawdown:
            return f"max drawdown: INR {self.book.drawdown():,.0f}", True
        if self.oms.consecutive_rejects >= self.cfg.max_consecutive_rejects:
            return f"{self.oms.consecutive_rejects} orders rejected in a row", True
        stale = [s for s in STALE_SECTIONS if s in snap.stale_sections]
        if stale and self.book.open_qty():
            self._stale_since = self._stale_since or now
            if now - self._stale_since >= self.cfg.stale_halt_after:
                # no flatten: we cannot price an exit, and the stop is resting at the broker
                return f"market data stale for {(now - self._stale_since).seconds}s with a position open", False
        else:
            self._stale_since = None
        if self._mismatches >= 2:
            # no flatten: our own position record is what is in doubt
            return "positions differ from the broker's (twice in a row)", False
        return None

    async def _reconcile(self, now: datetime) -> None:
        due = self._last_reconcile is None or now - self._last_reconcile >= self.cfg.reconcile_every
        if not (due or self._mismatches):  # after a mismatch: check again at once
            return
        self._last_reconcile = now
        try:
            theirs = await self.oms.broker.positions()
        except Exception as ex:
            raise OrderUncertain(f"cannot read broker positions: {ex!r}") from None
        ours = self.book.net_positions()
        diff = {
            i: {"ours": ours.get(i, 0), "broker": theirs.get(i, 0)}
            for i in set(ours) | set(theirs)
            if ours.get(i, 0) != theirs.get(i, 0)
        }
        self._mismatches = self._mismatches + 1 if diff else 0
        self.audit.record("reconcile", now, ok=not diff, diff=diff)

    # ------------------------------------------------------------------ orders
    def _intent(self, t: ActiveTrade, leg: str, key: str, side: str, qty: int, now: datetime, **kw) -> OrderIntent:
        return OrderIntent(
            intent_id=intent_key(t.trade_id, leg, key),
            trade_id=t.trade_id,
            leg=leg,
            instrument=t.instrument,
            side=side,
            qty=qty,
            lot_size=t.lot_size,
            tick_size=t.tick,
            created_at=now,
            **kw,
        )

    async def _on_signal(self, snap: MarketSnapshot, sig: Signal, now: datetime) -> None:
        if self.trade is not None and not self.trade.opened:
            await self._cancel_pending_entry(now, f"signal changed: {sig.status} {sig.bias}")
        if sig.status != "active" or self.trade is not None:
            return
        sz = sig.sizing
        if sz is None or sz.future_entry is None or sz.future_stop_loss is None or not sig.targets:
            return
        basis = sz.future_entry - sz.entry
        up = sig.bias == "bullish"
        trade_id = intent_key(self.symbol, sig.signal_id)
        t = ActiveTrade(
            trade_id=trade_id,
            signal_id=sig.signal_id,
            bias=sig.bias,
            instrument=sz.instrument,
            lot_size=sz.lot_size,
            tick=self.spec.tick_size,
            entry_id=intent_key(trade_id, "entry", ""),
            stop_px=sz.future_stop_loss,
            target_px=_round_toward(sig.targets[0].price + basis, self.spec.tick_size, sz.future_entry),
        )
        entry = self._intent(
            t,
            "entry",
            "",
            "BUY" if up else "SELL",
            sz.lots * sz.lot_size,
            now,
            order_type="LIMIT",
            price=sz.future_entry,
            reason=f"{sig.bias} signal {sig.signal_id} ({sig.rules_version})",
        )
        decision = self.gate.check_entry(entry, sz.risk_per_lot, self.book, now, self.halt)
        self.audit.record(
            "risk_decision",
            now,
            signal_id=sig.signal_id,
            approved=decision.approved,
            reasons=decision.reasons,
            requested_qty=entry.qty,
            qty=decision.qty,
            loss_per_lot=sz.risk_per_lot,
        )
        if not decision.approved:
            return
        entry = entry.model_copy(update={"qty": decision.qty})
        self.audit.record(
            "signal_acted",
            now,
            signal_id=sig.signal_id,
            trade_id=trade_id,
            snapshot_version=snap.version,
            bias=sig.bias,
            entry=entry.price,
            stop=t.stop_px,
            target=t.target_px,
            qty=entry.qty,
        )
        self.trade = t
        await self.oms.submit(entry, now)

    async def _cancel_pending_entry(self, now: datetime, why: str) -> None:
        t = self.trade
        if t is None:
            return
        await self.oms.cancel(t.entry_id, now, why)
        if not t.opened and self.book.open_qty(t.instrument) == 0:
            self.audit.record("trade_abandoned", now, trade_id=t.trade_id, why=why)
            self.trade = None

    async def _flatten(self, now: datetime, why: str) -> None:
        t = self.trade
        if t is None:
            return
        for o in self.oms.open_orders(t.trade_id):
            if o.intent.leg != "exit":
                await self.oms.cancel(o.intent.intent_id, now, why)
        qty = self.book.open_qty(t.instrument)
        if qty == 0 or t.closing:
            return
        t.closing = True
        t.exits += 1
        side = "SELL" if qty > 0 else "BUY"
        await self.oms.submit(
            self._intent(t, "exit", str(t.exits), side, abs(qty), now, order_type="MARKET", reason=why), now
        )

    async def _bracket(self, now: datetime) -> None:
        """Keep a stop and a target resting for exactly the open quantity; close the trade when flat."""
        t = self.trade
        if t is None:
            return
        qty = abs(self.book.open_qty(t.instrument))
        if qty == 0:
            if t.opened:
                for o in self.oms.open_orders(t.trade_id):  # one-cancels-other, and any entry remainder
                    await self.oms.cancel(o.intent.intent_id, now, "trade closed")
                pnl = self.book.positions[t.instrument].realized_pnl if t.instrument in self.book.positions else 0
                self.closed.append({"trade_id": t.trade_id, "closed_at": now.isoformat(), "realized_total": pnl})
                self.audit.record("trade_closed", now, trade_id=t.trade_id, net_today=round(self.book.net_today(), 2))
                self.trade = None
            elif (o := self.oms.orders.get(t.entry_id)) is not None and not o.is_open:
                self.audit.record("trade_abandoned", now, trade_id=t.trade_id, why=f"entry {o.status}")
                self.trade = None
            return
        if t.closing or qty == t.bracket_qty:
            return
        for leg_id in (t.stop_id, t.target_id):  # resize: cancel, then place for the new quantity
            if leg_id is not None:
                await self.oms.cancel(leg_id, now, f"resize bracket to {qty}")
        stop = self._intent(
            t, "stop", str(qty), t.exit_side, qty, now, order_type="SL-M", trigger=t.stop_px, reason="protective stop"
        )
        target = self._intent(
            t, "target", str(qty), t.exit_side, qty, now, order_type="LIMIT", price=t.target_px, reason="target"
        )
        t.stop_id, t.target_id, t.bracket_qty = stop.intent_id, target.intent_id, qty
        await self.oms.submit(stop, now)
        await self.oms.submit(target, now)

    # ------------------------------------------------------------------ state for the dashboard
    def state(self) -> dict:
        t = self.trade
        return {
            "halt": self.halt.model_dump(mode="json"),
            "positions": self.book.net_positions(),
            "net_today": round(self.book.net_today(), 2),
            "charges_today": round(self.book.charges_today, 2),
            "trades_today": self.book.trades_today,
            "drawdown": round(self.book.drawdown(), 2),
            "trade": None
            if t is None
            else {
                "trade_id": t.trade_id,
                "bias": t.bias,
                "instrument": t.instrument,
                "stop": t.stop_px,
                "target": t.target_px,
                "opened": t.opened,
                "closing": t.closing,
            },
            "open_orders": [o.model_dump(mode="json") for o in self.oms.open_orders()],
            "stats": self.oms.stats,
        }
