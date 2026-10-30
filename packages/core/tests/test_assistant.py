"""Ask-Claude core: request shape, streaming, refusals, and number verification."""

import json
from datetime import date
from types import SimpleNamespace

import pytest

from mp_core.assistant import FALLBACK_BETA, MODEL, SYSTEM, Assistant, verify_numbers
from mp_core.clock import SimClock
from mp_core.digest import market_digest
from mp_core.providers.replay import ReplayProvider
from mp_core.signals.fusion import fuse
from mp_core.snapshot import build_snapshot, gather_inputs

from conftest import ist

NOW = ist(date(2026, 9, 22), 11, 15)


@pytest.fixture(scope="module")
async def digest(synth_dir):
    p = ReplayProvider(synth_dir, SimClock(NOW))
    snap = build_snapshot(await gather_inputs(p, "NSE:BANKNIFTY", NOW), NOW, rate=0.065, version=1)
    return market_digest(snap.model_copy(update={"signal": fuse(snap)}), NOW)


def fake_client(chunks, stop_reason="end_turn", category=None):
    calls = []

    class Stream:
        def __init__(self):
            self.final = SimpleNamespace(
                stop_reason=stop_reason,
                model=MODEL,
                stop_details=SimpleNamespace(category=category) if category else None,
                usage=SimpleNamespace(input_tokens=1200, output_tokens=80),
            )

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        @property
        def text_stream(self):
            async def gen():
                for c in chunks:
                    yield c

            return gen()

        async def get_final_message(self):
            return self.final

    def stream(**kw):
        calls.append(kw)
        return Stream()

    return SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(stream=stream))), calls


async def collect(assistant, q, digest):
    return [e async for e in assistant.ask(q, digest)]


class TestAsk:
    async def test_request_shape(self, digest):
        client, calls = fake_client(["ok"])
        await collect(Assistant(client), "What is the setup?", digest)
        kw = calls[0]
        assert kw["model"] == "claude-sonnet-5-5"
        assert kw["betas"] == [FALLBACK_BETA] and kw["fallbacks"] == "default"
        assert kw["output_config"] == {"effort": "low"} and "thinking" not in kw
        assert kw["system"][0]["text"] == SYSTEM and "Never estimate" in SYSTEM
        content = kw["messages"][0]["content"]
        assert content.startswith("<market_data>") and content.endswith("Question: What is the setup?")
        sent = json.loads(content.split("<market_data>\n")[1].split("\n</market_data>")[0])
        assert sent == digest  # exactly the get_market_snapshot payload

    async def test_streams_text_then_done(self, digest):
        client, _ = fake_client(["The ", "spot is ", "57,846."])
        events = await collect(Assistant(client), "spot?", digest)
        assert [e.kind for e in events] == ["text", "text", "text", "done"]
        assert events[-1].info["output_tokens"] == 80

    async def test_refusal(self, digest):
        client, _ = fake_client([], stop_reason="refusal", category="general_harms")
        events = await collect(Assistant(client), "q", digest)
        assert events[-1].kind == "refusal" and events[-1].info["category"] == "general_harms"

    async def test_fallbacks_can_be_turned_off(self, digest):
        client, calls = fake_client(["x"])
        await collect(Assistant(client, fallbacks=False), "q", digest)
        assert "betas" not in calls[0] and "fallbacks" not in calls[0]

    @pytest.mark.parametrize("q", ["", "   ", "x" * 1001])
    async def test_question_validated(self, digest, q):
        client, calls = fake_client(["x"])
        with pytest.raises(ValueError):
            await collect(Assistant(client), q, digest)
        assert calls == []

    def test_no_credentials(self, monkeypatch):
        for v in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
            monkeypatch.delenv(v, raising=False)
        assert Assistant.from_env() is None

    def test_unreadable_profile_disables_instead_of_crashing(self, monkeypatch):
        from mp_core.news.classifier import NewsClassifier

        for v in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            monkeypatch.delenv(v, raising=False)
        monkeypatch.setenv("ANTHROPIC_PROFILE", "default")
        monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", "/nonexistent-market-pulse")
        assert Assistant.from_env() is None and NewsClassifier.from_env() is None


class TestVerifyNumbers:
    D = {
        "quote": {"ltp": 57845.81, "change_pct": 0.4237},
        "expected_range": {"iv": 0.1823, "sigma_points": 213.4},
        "signal": {"reward_risk": 1.6123, "reasons": [{"text": "put-call OI ratio 1.20 (supportive)"}]},
    }

    @pytest.mark.parametrize(
        "answer",
        [
            "Spot is 57,845.81 [quote].",
            "Spot is about 57,846 [quote].",
            "Spot is around 57,850 [quote].",
            "Spot is roughly 57,800.",  # rounded to hundreds
            "IV is 18.2% [expected_range.iv] and the 1-sigma move is 213 points.",
            "Reward:risk is 1.61; PCR is 1.20 [signal.reasons].",
            "The index is up 0.42%.",
            "On 2026-09-22 at 11:15 IST, 2 lots, 3 bars, 1 sigma.",  # dates, times, small counts
        ],
    )
    def test_supported_numbers_pass(self, answer):
        assert verify_numbers(answer, self.D) == []

    @pytest.mark.parametrize(
        "answer, bad",
        [
            ("Support sits at 57,500.", "57,500"),
            ("IV is 21.5%.", "21.5%"),
            ("A target of 58,120 looks likely.", "58,120"),
            ("Reward:risk is 2.4.", "2.4"),
        ],
    )
    def test_invented_numbers_are_flagged(self, answer, bad):
        assert verify_numbers(answer, self.D) == [bad]

    def test_on_a_real_digest(self, digest):
        spot = digest["data"]["quote"]["ltp"]
        assert verify_numbers(f"Spot is {spot:,.2f} [quote].", digest) == []
        assert verify_numbers(f"Spot is {spot + 400:,.0f}.", digest) != []
