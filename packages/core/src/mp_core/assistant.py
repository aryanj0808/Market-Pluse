"""Ask-Claude: answers questions about the market from the precomputed digest only.

Claude writes the explanation; every number must come from the digest it is given (the same
payload `get_market_snapshot` returns). After the answer has streamed, `verify_numbers` checks each
number in it against the digest and reports the ones it cannot find - shown next to the answer.

Single-turn by design: every question is answered against fresh data, and no conversation history
has to be rebuilt (which would drop the model's thinking blocks between turns).
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import anthropic

log = logging.getLogger(__name__)

MODEL = "claude-sonnet-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_QUESTION_CHARS = 1000

SYSTEM = """You are the analyst inside Market Pulse, an analysis tool for Indian index markets \
(NSE:BANKNIFTY, NSE:NIFTY, NSE:FINNIFTY). You explain; you do not compute.

Rules:
- Use only the data inside <market_data>. Every number you state must appear there (you may round \
it). Never estimate, extrapolate or recall prices, levels, probabilities or dates from memory.
- After each claim, cite the field it came from in square brackets, e.g. [expected_range.session], \
[signal.reasons], [levels], [news.top].
- If something the user asks about is missing, stale or listed in stale_sections/errors, say so \
plainly instead of filling the gap.
- If "source" is "synthetic", say once at the start that the data is synthetic test data, not \
the real market.
- The signal comes from a fixed rule set; explain it through its own reasons. A "no_signal" status \
is a valid answer - give its no_signal_reasons.
- This is analysis, not advice: do not tell the user to buy or sell, and do not invent targets or \
stops beyond those in the data.
- Times are IST. Be concise: short paragraphs or bullets, no tables unless asked."""


def build_user_message(question: str, digest: dict) -> str:
    return (
        "<market_data>\n" + json.dumps(digest, separators=(",", ":"), ensure_ascii=False) + "\n</market_data>\n\n"
        f"Question: {question.strip()}"
    )


@dataclass
class AskEvent:
    kind: str  # "text" | "done" | "refusal"
    text: str = ""
    info: dict[str, Any] = field(default_factory=dict)


class Assistant:
    def __init__(
        self,
        client: anthropic.AsyncAnthropic,
        *,
        model: str = MODEL,
        effort: str = "low",
        max_tokens: int = 4000,
        fallbacks: bool = True,
    ) -> None:
        self.client = client
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.fallbacks = fallbacks

    @classmethod
    def from_env(cls, **kw: Any) -> Assistant | None:
        """None when no credentials resolve (API key, auth token or `ant auth login` profile)."""
        try:
            client = anthropic.AsyncAnthropic(max_retries=2, timeout=120.0)
        except anthropic.AnthropicError as ex:  # e.g. a configured profile that cannot be read
            log.warning("Ask-Claude disabled: %s", ex)
            return None
        if not (client.api_key or client.auth_token or client.credentials is not None):
            return None
        return cls(client, **kw)

    async def ask(self, question: str, digest: dict) -> AsyncIterator[AskEvent]:
        """Stream the answer: text deltas, then one `done` (or `refusal`) event."""
        if not question.strip():
            raise ValueError("empty question")
        if len(question) > MAX_QUESTION_CHARS:
            raise ValueError(f"question longer than {MAX_QUESTION_CHARS} characters")
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": [{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
            "output_config": {"effort": self.effort},
            "messages": [{"role": "user", "content": build_user_message(question, digest)}],
        }
        if self.fallbacks:  # server-side retry on another model for the declines it covers
            kwargs |= {"betas": [FALLBACK_BETA], "fallbacks": "default"}
        async with self.client.beta.messages.stream(**kwargs) as stream:
            async for text in stream.text_stream:
                yield AskEvent("text", text)
            final = await stream.get_final_message()
        if final.stop_reason == "refusal":
            category = getattr(final.stop_details, "category", None) if final.stop_details else None
            yield AskEvent("refusal", info={"category": category})
            return
        usage = final.usage
        yield AskEvent(
            "done",
            info={
                "stop_reason": final.stop_reason,
                "model": final.model,
                "input_tokens": getattr(usage, "input_tokens", None),
                "output_tokens": getattr(usage, "output_tokens", None),
            },
        )


# ------------------------------------------------------------------ number verification
NUM = re.compile(r"(?<![\w.])[-+−]?(?:\d{1,3}(?:,\d{2,3})+|\d+)(?:\.\d+)?(?:\s?%)?")
DATE_OR_TIME = re.compile(
    r"\d{4}-\d{2}-\d{2}|\d{1,2}:\d{2}|\d{1,2}\s?(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)", re.IGNORECASE
)
CITATION = re.compile(r"\[[^\]]*\]")


def _flatten_numbers(obj: Any) -> list[float]:
    out: list[float] = []
    if isinstance(obj, bool):
        return out
    if isinstance(obj, int | float):
        if math.isfinite(obj):
            out.append(float(obj))
    elif isinstance(obj, str):  # numbers inside reason texts, labels, dates count too
        out += [_to_float(m) for m in NUM.findall(obj)]
    elif isinstance(obj, dict):
        for v in obj.values():
            out += _flatten_numbers(v)
    elif isinstance(obj, list | tuple):
        for v in obj:
            out += _flatten_numbers(v)
    return out


def _to_float(tok: str) -> float:
    return float(tok.replace(",", "").replace("%", "").replace("−", "-").strip())


def _rounding_tolerance(tok: str) -> float:
    """Half a unit of the precision the answer was written at: "18.2" -> 0.05, "56,780" -> 5,
    "56,800" -> 50 (trailing zeros of an integer mean it was rounded; capped at thousands)."""
    t = tok.replace("%", "").replace(",", "").strip().lstrip("+-−")
    if "." in t:
        return 0.5 * 10 ** -len(t.split(".")[1])
    zeros = min(len(t) - len(t.rstrip("0")), 3) if t.strip("0") else 0
    return 0.5 * 10**zeros


def verify_numbers(answer: str, digest: dict, small_int_ok: int = 10) -> list[str]:
    """Numbers in `answer` that cannot be found in `digest`, allowing only for the answer's own
    rounding, sign, and percent <-> fraction (18.2% vs 0.182). Dates, times, [citations] and small
    counts (<= 10, e.g. "2 lots", "3 bars") are skipped. An empty list means every number checked out."""
    known = _flatten_numbers(digest)
    text = DATE_OR_TIME.sub(" ", CITATION.sub(" ", answer))
    unverified = []
    for tok in NUM.findall(text):
        try:
            x = _to_float(tok)
        except ValueError:
            continue
        if x == int(x) and abs(x) <= small_int_ok:
            continue
        tol = _rounding_tolerance(tok)
        candidates = ((x, tol), (-x, tol), (x / 100, tol / 100), (x * 100, tol * 100))
        if not any(abs(c - k) <= t + 1e-9 for c, t in candidates for k in known):
            unverified.append(tok.strip())
    return list(dict.fromkeys(unverified))
