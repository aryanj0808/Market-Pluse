"""Positions and P&L from fills - the system's own view, which the reconciler checks against the
broker's every cycle. Charges are estimated with the same cost model the signal used for sizing,
so paper P&L is net of costs rather than flattering."""

from __future__ import annotations

from datetime import date, datetime

from mp_core import market_calendar as cal
from mp_core.trading.models import Fill, Position


class Portfolio:
    def __init__(self, *, cost_bps: float = 3.0, brokerage_per_order: float = 20.0) -> None:
        self.cost_bps = cost_bps  # round trip, so half is charged per fill
        self.brokerage_per_order = brokerage_per_order
        self.positions: dict[str, Position] = {}
        self.marks: dict[str, float] = {}
        self.charges_today = 0.0
        self.realized_today = 0.0
        self.trades_today = 0  # entries filled (a trade counts once, however many partial fills)
        self.equity_peak = 0.0  # high-water mark of total P&L (all days)
        self.realized_total = 0.0
        self.charges_total = 0.0
        self._day: date | None = None
        self._counted_trades: set[str] = set()
        self._orders_charged: set[str] = set()

    def roll_day(self, now: datetime) -> None:
        d = now.astimezone(cal.IST).date()
        if d != self._day:
            self._day = d
            self.charges_today = self.realized_today = 0.0
            self.trades_today = 0

    def apply(self, f: Fill) -> float:
        """Book one fill; returns the realized P&L it produced (INR, gross)."""
        self.roll_day(f.ts)
        charge = f.price * f.qty * (self.cost_bps / 2) / 10_000
        if f.broker_order_id not in self._orders_charged:  # brokerage is per order, not per partial fill
            self._orders_charged.add(f.broker_order_id)
            charge += self.brokerage_per_order
        self.charges_today += charge
        self.charges_total += charge
        if f.leg == "entry" and f.trade_id not in self._counted_trades:
            self._counted_trades.add(f.trade_id)
            self.trades_today += 1
        p = self.positions.get(f.instrument) or Position(instrument=f.instrument, qty=0, avg_price=0.0, realized_pnl=0)
        q, dq = p.qty, f.signed_qty
        realized = 0.0
        if q == 0 or (q > 0) == (dq > 0):  # opening or adding
            new_q = q + dq
            avg = (p.avg_price * abs(q) + f.price * abs(dq)) / abs(new_q)
        else:  # reducing, closing or flipping
            closing = min(abs(q), abs(dq))
            realized = closing * (f.price - p.avg_price) * (1 if q > 0 else -1)
            new_q = q + dq
            avg = p.avg_price if new_q and (new_q > 0) == (q > 0) else (f.price if new_q else 0.0)
        self.positions[f.instrument] = Position(
            instrument=f.instrument, qty=new_q, avg_price=avg, realized_pnl=round(p.realized_pnl + realized, 2)
        )
        self.realized_today += realized
        self.realized_total += realized
        self.marks.setdefault(f.instrument, f.price)
        self._update_peak()
        return realized

    def mark(self, instrument: str, price: float) -> None:
        self.marks[instrument] = price
        self._update_peak()

    def unrealized(self) -> float:
        return sum(p.qty * (self.marks.get(i, p.avg_price) - p.avg_price) for i, p in self.positions.items() if p.qty)

    def net_today(self) -> float:
        """Realized + unrealized - charges, today (INR). The daily loss limit is judged on this."""
        return self.realized_today + self.unrealized() - self.charges_today

    def equity(self) -> float:
        return self.realized_total + self.unrealized() - self.charges_total

    def drawdown(self) -> float:
        return max(0.0, self.equity_peak - self.equity())

    def _update_peak(self) -> None:
        self.equity_peak = max(self.equity_peak, self.equity())

    def open_qty(self, instrument: str | None = None) -> int:
        if instrument is not None:
            p = self.positions.get(instrument)
            return p.qty if p else 0
        return sum(abs(p.qty) for p in self.positions.values())

    def net_positions(self) -> dict[str, int]:
        return {i: p.qty for i, p in self.positions.items() if p.qty}
