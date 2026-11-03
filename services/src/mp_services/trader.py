"""trader: engine snapshots -> paper orders (signal -> risk gate -> order manager -> paper broker),
with the kill switch, breakers and reconciliation. MP_TRADING_MODE=paper; live is refused.

State for the dashboard goes to `mp:{SYM}:trading`; every decision to the audit trail
(MP_AUDIT_DIR, one hash-chained JSONL file per day); halts and closed trades to Telegram."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from mp_core.alerts import TelegramSender
from mp_core.health import heartbeat
from mp_core.settings import get_settings
from mp_core.store import MarketStore, RedisKV
from mp_core.trading.audit import JsonlAudit
from mp_core.trading.setup import paper_trader
from mp_services._common import run_async, setup_logging

log = logging.getLogger("trader")


class SnapshotClock:
    """The paper broker's clock: the time of the snapshot being processed (replay time in a replay)."""

    def __init__(self) -> None:
        self.t = datetime.now(UTC)

    def now(self) -> datetime:
        return self.t


async def run() -> None:
    s = get_settings()
    kv = RedisKV(s.redis_url)
    store = MarketStore(kv)
    clock = SnapshotClock()
    trader, broker = paper_trader(s, kv, clock, JsonlAudit(s.audit_dir))
    sender = TelegramSender(s.telegram_bot_token, s.telegram_chat_id, max_per_hour=s.alerts_max_per_hour)
    lim = trader.gate.limits
    log.info(
        "PAPER trading %s: per trade INR %s, daily INR %s, drawdown INR %s, %d lot(s), %d trades/day; audit in %s",
        s.default_symbol,
        f"{lim.max_loss_per_trade:,.0f}",
        f"{lim.daily_loss_limit:,.0f}",
        f"{lim.max_drawdown:,.0f}",
        lim.max_open_lots,
        lim.max_trades_per_day,
        s.audit_dir,
    )
    last_version, was_halted, closed = -1, False, 0
    try:
        async with heartbeat(kv, "trader", lambda: {"mode": "paper", **trader.oms.stats}):
            async for _ in store.updates(s.default_symbol):
                snap = await store.get_snapshot(s.default_symbol)
                if snap is None or snap.version == last_version:
                    continue
                last_version = snap.version
                clock.t = snap.computed_at
                if snap.future is not None:
                    broker.on_price(snap.future.value.key.id, snap.future.value.ltp, snap.computed_at)
                try:
                    await trader.on_snapshot(snap)
                except Exception:  # a bug must not leave orders unattended: halt and keep watching
                    log.exception("trader step failed - halting")
                    await trader.kill.halt("trader error (see logs)", "auto:trader", snap.computed_at)
                state = trader.state() | {"source": snap.source, "as_of": snap.computed_at.isoformat()}
                await kv.set(f"mp:{snap.symbol}:trading", json.dumps(state, default=str))
                synthetic_ok = s.alerts_synthetic or snap.source != "synthetic"
                if trader.halt.halted and not was_halted and synthetic_ok:
                    await sender.send(f"<b>Paper trading HALTED</b>\n{trader.halt.reason} ({trader.halt.by})")
                was_halted = trader.halt.halted
                for c in trader.closed[closed:]:
                    if synthetic_ok:
                        await sender.send(
                            f"<b>Paper trade closed</b> {c['trade_id']}\nnet today INR {trader.book.net_today():,.0f}"
                        )
                closed = len(trader.closed)
    finally:
        await kv.close()


def main() -> None:
    setup_logging()
    run_async(run())
