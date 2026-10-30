"""News models. The LLM only fills `NewsClassification`; every number derived from it (relevance,
decay, score) is computed by `mp_core.news.score`."""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, field_validator

from mp_core.models import AwareDatetime, Source, StrictModel


class Impact(StrEnum):
    LOW = "low"
    MED = "med"
    HIGH = "high"


class Horizon(StrEnum):
    INTRADAY = "intraday"  # matters for today's session
    DAYS = "days"  # the next few sessions
    WEEKS = "weeks"  # structural / slow-moving


class NewsClassification(StrictModel):
    """Exactly what Claude Haiku returns (validated - out-of-range values are rejected)."""

    relevant: bool
    entities: list[str]  # tickers / tags from the vocabulary given in the prompt
    sentiment: float = Field(ge=-1.0, le=1.0)
    impact: Impact
    horizon: Horizon
    reason: str = Field(min_length=1, max_length=600)


class RawNewsItem(StrictModel):
    feed: str  # e.g. "et_markets", "rbi_press", "synthetic"
    url: str = Field(min_length=1)
    title: str = Field(min_length=1)
    summary: str = ""
    published_at: AwareDatetime
    published_estimated: bool = False  # feed gave no date; fetch time used instead
    fetched_at: AwareDatetime
    source: Source

    @field_validator("url")
    @classmethod
    def _web_link(cls, v: str) -> str:
        # Feeds are untrusted and the dashboard renders this as a link: no javascript:/data: URLs.
        u = urlparse(v)
        if u.scheme.lower() not in ("http", "https") or not u.netloc:
            raise ValueError("news url must be an http(s) link")
        return v

    @property
    def id(self) -> str:
        from mp_core.news.dedupe import canonical_url

        return hashlib.sha1(canonical_url(self.url).encode()).hexdigest()[:16]


NewsStatus = Literal["classified", "dropped", "unclassified", "duplicate"]


class ScoredNewsItem(StrictModel):
    """A news item and what happened to it. `dropped` (invalid model output after one retry) and
    `classified` are final - the item is never sent to the model again. `unclassified` (no key,
    API outage) may be classified on a later poll."""

    id: str
    item: RawNewsItem
    title_tokens: list[str]  # normalised, for fuzzy de-duplication
    status: NewsStatus
    classification: NewsClassification | None = None
    model: str | None = None
    prompt_version: str | None = None
    classified_at: AwareDatetime | None = None
    error: str | None = None
    duplicate_of: str | None = None


class FeedHealth(StrictModel):
    feed: str
    url: str
    verified: bool  # has this feed's format been checked against a real response?
    last_attempt: AwareDatetime | None = None
    last_ok: AwareDatetime | None = None
    last_error: str | None = None
    items_last_poll: int = 0
