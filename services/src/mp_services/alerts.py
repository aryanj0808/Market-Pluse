"""alerts: watches engine updates and sends Telegram alerts (dry-run without a bot token)."""

from __future__ import annotations

import logging
from datetime import timedelta

from mp_core.alerts import AlertDispatcher, AlertRules, TelegramSender
from mp_core.health import heartbeat
from mp_core.news.score import entity_weights
from mp_core.news.store import NewsStore
from mp_core.registry import get_index
from mp_core.settings import get_settings
from mp_core.store import MarketStore, RedisKV
from mp_services._common import run_async, setup_logging

log = logging.getLogger("alerts")


class AlertService:
    def __init__(self, store: MarketStore, symbol: str, dispatcher: AlertDispatcher) -> None:
        self.store, self.symbol, self.dispatcher = store, symbol, dispatcher
        self.rules = AlertRules()
        self.news = NewsStore(store.kv)
        self._news_version = -1
        self.sent = 0

    async def on_update(self) -> int:
        snap = await self.store.get_snapshot(self.symbol)
        if snap is None:
            return 0
        alerts = self.rules.evaluate(snap)
        v = await self.news.version()
        if v != self._news_version:
            self._news_version = v
            items = await self.news.recent(snap.computed_at - timedelta(hours=24), snap.computed_at)
            live = {c.key.underlying: c.weight for c in snap.constituents.value} if snap.constituents else None
            alerts += self.rules.news_alerts(snap, items, entity_weights(get_index(self.symbol), live))
        sent = await self.dispatcher.dispatch(alerts)
        self.sent += len(sent)
        return len(sent)

    async def run(self) -> None:
        await self.on_update()
        async for _ in self.store.updates(self.symbol):
            try:
                await self.on_update()
            except Exception:  # one bad update must not stop alerting
                log.exception("alert evaluation failed")


async def run() -> None:
    s = get_settings()
    kv = RedisKV(s.redis_url)
    sender = TelegramSender(s.telegram_bot_token, s.telegram_chat_id, max_per_hour=s.alerts_max_per_hour)
    log.info(
        "alerts for %s: %s, synthetic alerts %s",
        s.default_symbol,
        "DRY RUN (set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)" if sender.dry_run else "Telegram",
        "allowed" if s.alerts_synthetic else "suppressed",
    )
    service = AlertService(
        MarketStore(kv), s.default_symbol, AlertDispatcher(sender, allow_synthetic=s.alerts_synthetic)
    )
    try:
        async with heartbeat(kv, "alerts", lambda: {"sent": service.sent, **sender.stats}):
            await service.run()
    finally:
        await sender.client.aclose()
        await kv.close()


def main() -> None:
    setup_logging()
    run_async(run())
