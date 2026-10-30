"""Alert rules, dispatch policy and the Telegram sender (mocked API)."""

import json
import logging
from datetime import date, timedelta

import httpx
import pytest

from mp_core.alerts import AlertDispatcher, AlertRules, TelegramSender
from mp_core.clock import SimClock
from mp_core.models import Source
from mp_core.news.dedupe import title_tokens
from mp_core.news.models import NewsClassification, RawNewsItem, ScoredNewsItem
from mp_core.providers.replay import ReplayProvider
from mp_core.signals.fusion import fuse
from mp_core.snapshot import build_snapshot, gather_inputs

from conftest import ist

NOW = ist(date(2026, 9, 22), 11, 15)


@pytest.fixture(scope="module")
async def base(synth_dir):
    p = ReplayProvider(synth_dir, SimClock(NOW))
    snap = build_snapshot(await gather_inputs(p, "NSE:BANKNIFTY", NOW), NOW, rate=0.065, version=1)
    return snap.model_copy(update={"signal": fuse(snap)})


def with_signal(snap, status, bias, minutes=0, **kw):
    sig = snap.signal.model_copy(update={"status": status, "bias": bias, **kw})
    return snap.model_copy(update={"signal": sig, "computed_at": snap.computed_at + timedelta(minutes=minutes)})


def with_spot(snap, ltp, minutes=0):
    q = snap.quote.model_copy(
        update={
            "value": snap.quote.value.model_copy(
                update={"ltp": ltp, "high": max(ltp, snap.quote.value.high), "low": min(ltp, snap.quote.value.low)}
            )
        }
    )
    return snap.model_copy(update={"quote": q, "computed_at": snap.computed_at + timedelta(minutes=minutes)})


class TestRules:
    def test_signal_activation_flip_and_withdrawal(self, base):
        r = AlertRules()
        assert r.evaluate(with_signal(base, "no_signal", "neutral")) == []  # first snapshot only primes
        a = r.evaluate(with_signal(base, "active", "bullish", 1, entry=57833.0, stop_loss=57768.0, reward_risk=1.6))
        assert [x.kind for x in a] == ["signal_active"]
        text = a[0].text
        assert text.startswith("[SYNTHETIC] 🟢") and "bullish signal" in text and "57,833" in text
        assert "analysis, not advice" in text
        assert r.evaluate(with_signal(base, "active", "bullish", 2)) == []  # unchanged: nothing
        flip = r.evaluate(with_signal(base, "active", "bearish", 3))
        assert [x.kind for x in flip] == ["signal_active"] and "🔴" in flip[0].text
        end = r.evaluate(with_signal(base, "no_signal", "neutral", 4, no_signal_reasons=["stale inputs: quote"]))
        assert [x.kind for x in end] == ["signal_ended"] and "stale inputs: quote" in end[0].text

    def test_range_breach_once_per_side(self, base):
        er = base.expected_range.value["session"]
        r = AlertRules()
        r.evaluate(with_spot(base, er.spot))
        up = r.evaluate(with_spot(base, er.one_sigma.upper + 5, 1))
        assert [x.kind for x in up] == ["range_breach"] and "above" in up[0].text
        assert r.evaluate(with_spot(base, er.one_sigma.upper + 9, 2)) == []  # still outside: no repeat
        down = r.evaluate(with_spot(base, er.one_sigma.lower - 5, 3))
        assert [x.kind for x in down] == ["range_breach"] and "below" in down[0].text

    def test_stale_data_after_grace_and_recovery(self, base):
        r = AlertRules()
        stale = base.model_copy(update={"stale_sections": ["quote"]})
        r.evaluate(base)
        assert r.evaluate(stale.model_copy(update={"computed_at": NOW + timedelta(minutes=1)})) == []  # grace
        a = r.evaluate(stale.model_copy(update={"computed_at": NOW + timedelta(minutes=4)}))
        assert [x.kind for x in a] == ["stale_data"] and "quote" in a[0].text
        assert r.evaluate(stale.model_copy(update={"computed_at": NOW + timedelta(minutes=6)})) == []
        b = r.evaluate(base.model_copy(update={"computed_at": NOW + timedelta(minutes=7)}))
        assert [x.kind for x in b] == ["data_recovered"]

    def test_stale_outside_market_hours_is_quiet(self, base):
        r = AlertRules()
        night = base.model_copy(update={"stale_sections": ["quote"], "computed_at": ist(date(2026, 9, 22), 20, 0)})
        r.evaluate(night)
        assert r.evaluate(night.model_copy(update={"computed_at": ist(date(2026, 9, 22), 21, 0)})) == []

    def test_high_impact_news_after_priming(self, base):
        def item(n, impact="high", ents=("SECTOR_BANKS",)):
            raw = RawNewsItem(
                feed="w",
                url=f"https://news.synthetic.invalid/a/{n}",
                title=f"Story {n} <b>x</b>",
                published_at=NOW,
                fetched_at=NOW,
                source=Source.SYNTHETIC,
            )
            c = NewsClassification(
                relevant=True, entities=list(ents), sentiment=-0.6, impact=impact, horizon="days", reason="r"
            )
            return ScoredNewsItem(
                id=raw.id,
                item=raw,
                title_tokens=title_tokens(raw.title),
                status="classified",
                classification=c,
                model="t",
            )

        w = {"SECTOR_BANKS": 1.0, "YESBANK": 0.005}
        r = AlertRules()
        assert r.news_alerts(base, [item(1)], w) == []  # prime: no replay of old headlines
        a = r.news_alerts(base, [item(1), item(2), item(3, impact="med"), item(4, ents=("YESBANK",))], w)
        assert [x.key.split(":")[-1] for x in a] == [item(2).id]  # only new, high impact, relevant
        assert "&lt;b&gt;" in a[0].text and "negative" in a[0].text  # escaped HTML


class FakeTelegram:
    def __init__(self, statuses=(200,)):
        self.statuses = list(statuses)
        self.bodies = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(req.content))
        st = self.statuses.pop(0) if self.statuses else 200
        if st == 429:
            return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 0}})
        return httpx.Response(st, json={"ok": st == 200})


class TestSender:
    async def test_sends_html_message(self):
        fake = FakeTelegram()
        s = TelegramSender("123:SECRET", "42", client=httpx.AsyncClient(transport=httpx.MockTransport(fake)))
        assert await s.send("<b>hi</b>")
        assert fake.bodies[0] == {
            "chat_id": "42",
            "text": "<b>hi</b>",
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

    async def test_retries_once_on_429(self):
        fake = FakeTelegram([429, 200])
        s = TelegramSender("t", "c", client=httpx.AsyncClient(transport=httpx.MockTransport(fake)))
        assert await s.send("x") and len(fake.bodies) == 2

    async def test_failure_is_reported_without_leaking_the_token(self, caplog):
        def boom(req):
            raise httpx.ConnectError("blocked")

        s = TelegramSender("123:SECRET", "c", client=httpx.AsyncClient(transport=httpx.MockTransport(boom)))
        with caplog.at_level(logging.WARNING):
            assert not await s.send("x")
        assert "SECRET" not in caplog.text and s.stats["failed"] == 1

    async def test_hourly_cap(self):
        s = TelegramSender(None, None, max_per_hour=3)  # dry run
        results = [await s.send("x") for _ in range(5)]
        assert results == [True, True, True, False, False] and s.dry_run and s.stats["rate_limited"] == 2


class TestDispatcher:
    async def test_synthetic_suppressed_by_default(self, base):
        r = AlertRules()
        r.evaluate(with_signal(base, "no_signal", "neutral"))
        alerts = r.evaluate(with_signal(base, "active", "bullish", 1))
        d = AlertDispatcher(TelegramSender(None, None))
        assert await d.dispatch(alerts) == [] and d.stats["suppressed_synthetic"] == 1
        opted_in = AlertDispatcher(TelegramSender(None, None), allow_synthetic=True)
        assert len(await opted_in.dispatch(alerts)) == 1

    async def test_cooldown(self, base):
        r = AlertRules()
        r.evaluate(with_signal(base, "no_signal", "neutral"))
        d = AlertDispatcher(TelegramSender(None, None), allow_synthetic=True)
        sent = []
        for i, st in enumerate(["active", "no_signal", "active", "no_signal", "active"]):
            snap = with_signal(base, st, "bullish" if st == "active" else "neutral", 1 + i)
            sent += await d.dispatch(r.evaluate(snap))
        kinds = [a.kind for a in sent]
        assert kinds.count("signal_active") == 1 and d.stats["cooldown"] >= 2  # flapping within 15 min
