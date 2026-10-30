"""Alerts: deterministic rules over consecutive snapshots, sent to Telegram.

Rules (each with a dedupe key and cooldown, so a flapping input cannot spam):
    signal_active     a signal becomes active, or flips bias           (entry, stop, targets, reasons)
    signal_ended      an active signal is withdrawn                      (why: no_signal_reasons)
    range_breach      spot leaves the session's 1-sigma expected range   (once per side per session)
    stale_data        a critical input stays stale during market hours  (and when it recovers)
    high_impact_news  a new high-impact news item relevant to the index

Synthetic data never alerts unless explicitly allowed - a phone notification about fake prices is
worse than none. Without a bot token the service runs dry (logs what it would send).
"""

from __future__ import annotations

import asyncio
import html
import logging
import time
from collections import deque
from datetime import datetime, timedelta

import httpx

from mp_core import market_calendar as cal
from mp_core.models import AwareDatetime, Source, StrictModel
from mp_core.news.score import relevant_items
from mp_core.snapshot import MarketSnapshot

log = logging.getLogger(__name__)

COOLDOWN = {
    "signal_active": timedelta(minutes=15),
    "signal_ended": timedelta(minutes=15),
    "range_breach": timedelta(hours=8),
    "stale_data": timedelta(minutes=30),
    "data_recovered": timedelta(minutes=30),
    "high_impact_news": timedelta(days=7),
}
CRITICAL = ("quote", "expected_range", "levels")
STALE_GRACE = timedelta(minutes=2)


class Alert(StrictModel):
    kind: str
    key: str  # dedupe key (with the cooldown for its kind)
    symbol: str
    ts: AwareDatetime
    source: Source
    text: str  # Telegram HTML


def _esc(x: object) -> str:
    return html.escape(str(x), quote=False)


def _px(x: float | None) -> str:
    return "–" if x is None else f"{x:,.0f}"


class AlertRules:
    """Stateful only in what it needs to compare: the previous snapshot and stale timers."""

    def __init__(self) -> None:
        self.prev: MarketSnapshot | None = None
        self._stale_since: datetime | None = None
        self._stale_alerted = False
        self._seen_news: set[str] = set()
        self._news_primed = False

    def evaluate(self, snap: MarketSnapshot) -> list[Alert]:
        out: list[Alert] = []
        prev, self.prev = self.prev, snap
        now, sym = snap.computed_at, snap.symbol
        tag = "[SYNTHETIC] " if snap.source is Source.SYNTHETIC else ""
        day = now.astimezone(cal.IST).date()

        def alert(kind: str, key: str, text: str) -> None:
            out.append(
                Alert(kind=kind, key=f"{kind}:{sym}:{key}", symbol=sym, ts=now, source=snap.source, text=tag + text)
            )

        # --- signal transitions
        sig, psig = snap.signal, prev.signal if prev else None
        if sig is not None and prev is not None:
            was_active = psig is not None and psig.status == "active"
            if sig.status == "active" and (not was_active or psig.bias != sig.bias):
                arrow = "🟢" if sig.bias == "bullish" else "🔴"
                reasons = [r for r in sig.reasons if r.effect != 0][:3]
                targets = ", ".join(_px(t.price) for t in sig.targets)
                lines = [
                    f"{arrow} <b>{_esc(sym)} {sig.bias} signal</b> (confidence {sig.confidence:.0%})",
                    f"Entry {_px(sig.entry)} · stop {_px(sig.stop_loss)} · targets {targets} · R:R "
                    f"{sig.reward_risk:.2f}"
                    if sig.reward_risk
                    else "",
                    *[f"• {_esc(r.text)}" for r in reasons],
                    f"<i>spot {_px(sig.spot)} · rules {sig.rules_version} · analysis, not advice</i>",
                ]
                alert("signal_active", sig.bias, "\n".join(x for x in lines if x))
            elif sig.status != "active" and was_active:
                why = "; ".join(sig.no_signal_reasons[:2]) or "inputs changed"
                alert("signal_ended", psig.bias, f"⚪ <b>{_esc(sym)} {psig.bias} signal withdrawn</b>\n{_esc(why)}")

        # --- expected-range breach (session 1-sigma band)
        er = snap.expected_range.value.get("session") if snap.expected_range else None
        if er is not None and prev is not None and prev.expected_range is not None:
            spot, pspot = snap.quote.value.ltp, prev.quote.value.ltp
            if spot > er.one_sigma.upper >= pspot:
                alert(
                    "range_breach",
                    f"{day}:up",
                    f"⬆️ <b>{_esc(sym)}</b> {_px(spot)} broke above the 1σ expected "
                    f"range ({_px(er.one_sigma.lower)}–{_px(er.one_sigma.upper)})",
                )
            elif spot < er.one_sigma.lower <= pspot:
                alert(
                    "range_breach",
                    f"{day}:down",
                    f"⬇️ <b>{_esc(sym)}</b> {_px(spot)} broke below the 1σ "
                    f"expected range ({_px(er.one_sigma.lower)}–"
                    f"{_px(er.one_sigma.upper)})",
                )

        # --- stale critical data during market hours
        o, c = cal.session_bounds(day)
        stale = [s for s in CRITICAL if s in snap.stale_sections]
        in_session = cal.is_trading_day(day) and o + STALE_GRACE <= now <= c
        if stale and in_session:
            self._stale_since = self._stale_since or now
            if not self._stale_alerted and now - self._stale_since >= STALE_GRACE:
                self._stale_alerted = True
                alert(
                    "stale_data",
                    str(day),
                    f"⚠️ <b>{_esc(sym)}</b> data stale: {_esc(', '.join(stale))}. "
                    "Signals are suspended until it recovers.",
                )
        else:
            if self._stale_alerted and not stale:
                alert("data_recovered", str(day), f"✅ <b>{_esc(sym)}</b> data is fresh again.")
            self._stale_since, self._stale_alerted = None, False
        return out

    def news_alerts(self, snap: MarketSnapshot, items: list, weights: dict[str, float]) -> list[Alert]:
        """High-impact relevant items not seen before. The first call only primes the seen-set, so
        a restart does not replay yesterday's headlines."""
        rows = [r for r in relevant_items(items, weights) if r["impact"] == "high" and r["relevance"] >= 0.5]
        if not self._news_primed:
            self._seen_news |= {r["id"] for r in rows}
            self._news_primed = True
            return []
        out = []
        tag = "[SYNTHETIC] " if snap.source is Source.SYNTHETIC else ""
        for r in reversed(rows):  # oldest first
            if r["id"] in self._seen_news:
                continue
            self._seen_news.add(r["id"])
            mood = "positive" if r["sentiment"] > 0.15 else "negative" if r["sentiment"] < -0.15 else "mixed"
            out.append(
                Alert(
                    kind="high_impact_news",
                    key=f"high_impact_news:{snap.symbol}:{r['id']}",
                    symbol=snap.symbol,
                    ts=snap.computed_at,
                    source=Source(r["source"]),
                    text=f"{tag}📰 <b>High-impact news</b> ({mood}, {_esc(snap.symbol)})\n{_esc(r['title'])}\n"
                    f"<i>{_esc(r['reason'])}</i>",
                )
            )
        return out


class TelegramSender:
    """Telegram Bot API sendMessage, with a local hourly cap and one retry on 429."""

    API = "https://api.telegram.org"

    def __init__(
        self, token: str | None, chat_id: str | None, *, client: httpx.AsyncClient | None = None, max_per_hour: int = 20
    ) -> None:
        self.token, self.chat_id = token, chat_id
        self.client = client or httpx.AsyncClient(timeout=10)
        self.max_per_hour = max_per_hour
        self._sent: deque[float] = deque()
        self.stats = {"sent": 0, "dry_run": 0, "rate_limited": 0, "failed": 0}

    @property
    def dry_run(self) -> bool:
        return not (self.token and self.chat_id)

    async def send(self, text: str) -> bool:
        now = time.monotonic()
        while self._sent and now - self._sent[0] > 3600:
            self._sent.popleft()
        if len(self._sent) >= self.max_per_hour:
            self.stats["rate_limited"] += 1
            log.warning("alert suppressed: local cap of %d/hour reached", self.max_per_hour)
            return False
        self._sent.append(now)
        if self.dry_run:
            self.stats["dry_run"] += 1
            log.info("[dry run] would send: %s", text.replace("\n", " | "))
            return True
        url = f"{self.API}/bot{self.token}/sendMessage"
        body = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        for attempt in (1, 2):
            try:
                r = await self.client.post(url, json=body)
            except httpx.HTTPError as ex:
                log.warning("telegram send failed: %s", type(ex).__name__)  # never log the URL (token)
                break
            if r.status_code == 429 and attempt == 1:
                wait = float((r.json().get("parameters") or {}).get("retry_after", 1))
                await asyncio.sleep(min(wait, 30))
                continue
            if r.status_code == 200 and r.json().get("ok"):
                self.stats["sent"] += 1
                return True
            log.warning("telegram rejected the message: HTTP %s %s", r.status_code, r.text[:200])
            break
        self.stats["failed"] += 1
        return False


class AlertDispatcher:
    """Applies cooldowns and the synthetic-data policy, then sends."""

    def __init__(self, sender: TelegramSender, *, allow_synthetic: bool = False) -> None:
        self.sender = sender
        self.allow_synthetic = allow_synthetic
        self._last: dict[str, datetime] = {}
        self.stats = {"suppressed_synthetic": 0, "cooldown": 0}

    async def dispatch(self, alerts: list[Alert]) -> list[Alert]:
        sent = []
        for a in alerts:
            if a.source is Source.SYNTHETIC and not self.allow_synthetic:
                self.stats["suppressed_synthetic"] += 1
                continue
            last = self._last.get(a.key)
            if last is not None and a.ts - last < COOLDOWN.get(a.kind, timedelta(minutes=15)):
                self.stats["cooldown"] += 1
                continue
            self._last[a.key] = a.ts
            if await self.sender.send(a.text):
                sent.append(a)
        return sent
