"""Builds a Trader from settings - shared by the trader service and tests."""

from __future__ import annotations

from mp_core.clock import Clock
from mp_core.settings import Settings
from mp_core.signals.fusion import RiskConfig
from mp_core.store.kv import KV
from mp_core.trading.audit import Audit
from mp_core.trading.killswitch import KillSwitch
from mp_core.trading.oms import OrderManager
from mp_core.trading.paper_broker import PaperBroker
from mp_core.trading.portfolio import Portfolio
from mp_core.trading.risk import RiskGate, RiskLimits
from mp_core.trading.trader import Trader


class TradingRefused(SystemExit):
    pass


def limits_from_settings(s: Settings) -> RiskLimits:
    risk = RiskConfig.from_settings(s)
    return RiskLimits(
        max_loss_per_trade=risk.max_loss,
        daily_loss_limit=s.capital * s.daily_loss_limit_pct / 100,
        max_drawdown=s.capital * s.max_drawdown_pct / 100,
        max_open_lots=s.max_open_lots,
        max_trades_per_day=s.max_trades_per_day,
        freeze_qty=s.freeze_qty,
    )


def paper_trader(s: Settings, kv: KV, clock: Clock, audit: Audit) -> tuple[Trader, PaperBroker]:
    if s.trading_mode == "live":
        raise TradingRefused(
            "MP_TRADING_MODE=live is refused: there is no live broker adapter. Paper-trade first - "
            "see README 'Trading' for the rollout gates."
        )
    if s.trading_mode != "paper":
        raise TradingRefused(f"MP_TRADING_MODE={s.trading_mode!r}: set it to 'paper' to run the trader")
    lim = limits_from_settings(s)
    if lim.daily_loss_limit < lim.max_loss_per_trade:
        raise TradingRefused(
            f"daily loss limit INR {lim.daily_loss_limit:,.0f} is below the per-trade loss "
            f"INR {lim.max_loss_per_trade:,.0f}: no trade could ever be taken"
        )
    broker = PaperBroker(clock, slippage_bps=s.slippage_bps, freeze_qty=s.freeze_qty)
    trader = Trader(
        s.default_symbol,
        OrderManager(broker, audit),
        Portfolio(cost_bps=s.cost_bps, brokerage_per_order=s.brokerage_per_order),
        RiskGate(lim),
        KillSwitch(kv),
        audit,
    )
    return trader, broker
