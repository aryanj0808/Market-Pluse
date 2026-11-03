"""Pre-trade risk gate. Every *entry* passes through `check_entry`; risk-reducing orders (stop,
target, exit) are never blocked - a gate that can stop you from closing a position is a hazard.

Limits (all INR unless stated):
    max_loss_per_trade   loss at the stop, costs included (the signal's per-lot figure x lots)
    daily_loss_limit     today's realized + unrealized - charges; at or beyond it: no new entries
                         (and the trader's breaker halts and flattens)
    max_drawdown         from the equity high-water mark
    max_open_lots        per position
    max_trades_per_day   entries filled today
    last_entry_time      no new entries after this IST time; flatten_time: everything closed
    freeze_qty           exchange freeze quantity (units per order); larger orders are capped
"""

from __future__ import annotations

import math
from datetime import datetime, time

from pydantic import Field

from mp_core import market_calendar as cal
from mp_core.models import StrictModel
from mp_core.trading.models import HaltState, OrderIntent, RiskDecision
from mp_core.trading.portfolio import Portfolio


class RiskLimits(StrictModel):
    max_loss_per_trade: float = Field(gt=0)
    daily_loss_limit: float = Field(gt=0)
    max_drawdown: float = Field(gt=0)
    max_open_lots: int = Field(default=1, gt=0)
    max_trades_per_day: int = Field(default=3, gt=0)
    last_entry_time: time = time(15, 0)
    flatten_time: time = time(15, 20)
    freeze_qty: int | None = None


class RiskGate:
    def __init__(self, limits: RiskLimits) -> None:
        self.limits = limits

    def check_entry(
        self, intent: OrderIntent, loss_per_lot: float, book: Portfolio, now: datetime, halt: HaltState
    ) -> RiskDecision:
        lim, reasons = self.limits, []
        ist = now.astimezone(cal.IST)
        o, _ = cal.session_bounds(ist.date())
        if halt.halted:
            reasons.append(f"trading halted: {halt.reason} ({halt.by})")
        if not cal.is_trading_day(ist.date()):
            reasons.append(f"{ist.date()} is not a trading day")
        elif now < o or ist.time() >= lim.last_entry_time:
            reasons.append(f"outside entry hours (09:15-{lim.last_entry_time:%H:%M} IST)")
        if book.open_qty() != 0:
            reasons.append("a position is already open (one at a time)")
        if book.trades_today >= lim.max_trades_per_day:
            reasons.append(f"{book.trades_today} trades today, limit {lim.max_trades_per_day}")
        today = book.net_today()
        if today <= -lim.daily_loss_limit:
            reasons.append(f"daily loss INR {-today:,.0f} at/over the limit INR {lim.daily_loss_limit:,.0f}")
        if book.drawdown() >= lim.max_drawdown:
            reasons.append(f"drawdown INR {book.drawdown():,.0f} at/over the limit INR {lim.max_drawdown:,.0f}")
        if loss_per_lot <= 0:
            reasons.append("loss per lot must be positive")
        if reasons:
            return RiskDecision(approved=False, reasons=reasons)

        room_today = lim.daily_loss_limit + min(today, 0.0)  # what is left of today's budget
        lots = min(
            intent.lots,
            lim.max_open_lots,
            math.floor(lim.max_loss_per_trade / loss_per_lot),
            math.floor(room_today / loss_per_lot),
        )
        if lim.freeze_qty is not None:
            lots = min(lots, lim.freeze_qty // intent.lot_size)
        if lots < 1:
            return RiskDecision(
                approved=False,
                reasons=[
                    f"one lot risks INR {loss_per_lot:,.0f}: over the per-trade limit "
                    f"INR {lim.max_loss_per_trade:,.0f} or today's remaining budget INR {room_today:,.0f}"
                ],
            )
        return RiskDecision(approved=True, qty=lots * intent.lot_size)
