"""Deterministic news score: impact-weighted, time-decayed, weighted by index relevance.

For each classified, relevant item published at or before `now`:

    relevance = min(1, sum of entity weights)   ticker -> constituent index weight (0.29 for a
                                                29% constituent); tag -> per-index tag weight
    decay     = 0.5 ** (age_hours / half_life)  half-life by horizon: intraday 3h, days 24h, weeks 96h
    weight    = impact_weight * decay * relevance     impact: low 0.25, med 0.5, high 1.0
    score     = sum(weight * sentiment) / (sum(weight) + damping)     in (-1, 1)

`damping` keeps a single low-weight headline from producing an extreme score.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime

from pydantic import Field

from mp_core.models import AwareDatetime, StrictModel
from mp_core.news.models import Horizon, Impact, ScoredNewsItem
from mp_core.registry import IndexSpec

IMPACT_WEIGHT = {Impact.LOW: 0.25, Impact.MED: 0.5, Impact.HIGH: 1.0}
HALF_LIFE_HOURS = {Horizon.INTRADAY: 3.0, Horizon.DAYS: 24.0, Horizon.WEEKS: 96.0}
DAMPING = 1.0
MAX_HALF_LIVES = 5  # older than this contributes < 3% and is ignored
LABEL_THRESHOLD = 0.15


class Contribution(StrictModel):
    id: str
    title: str
    feed: str
    url: str
    published_at: AwareDatetime
    entities: list[str]
    sentiment: float
    impact: Impact
    horizon: Horizon
    relevance: float
    decay: float
    weight: float
    contribution: float  # weight * sentiment
    reason: str


class NewsScore(StrictModel):
    symbol: str
    score: float = Field(ge=-1, le=1)
    label: str  # bearish | neutral | bullish
    n_items_seen: int  # items in the window (any status)
    n_used: int  # classified, relevant to this index, inside the decay window
    n_unclassified: int  # waiting for a classification (no key / API outage)
    n_dropped: int
    total_weight: float
    top: list[Contribution]  # largest |contribution| first
    formula: str = "sum(w*s)/(sum(w)+damping); w = impact*decay*relevance"


def entity_weights(spec: IndexSpec, constituent_weights: Mapping[str, float] | None = None) -> dict[str, float]:
    """Entity -> relevance for one index. Constituent weights (percent) default to the registry's
    and should be the live weights when available."""
    cw = constituent_weights if constituent_weights is not None else {c.symbol: c.weight for c in spec.constituents}
    return {**{sym: w / 100.0 for sym, w in cw.items()}, **spec.news_tags}


def relevance(entities: Iterable[str], weights: Mapping[str, float]) -> float:
    return min(1.0, sum(weights.get(e, 0.0) for e in set(entities)))


def contributions(items: Iterable[ScoredNewsItem], weights: Mapping[str, float], now: datetime) -> list[Contribution]:
    out = []
    for s in items:
        c = s.classification
        if s.status != "classified" or c is None or not c.relevant:
            continue
        age_h = (now - s.item.published_at).total_seconds() / 3600.0
        half_life = HALF_LIFE_HOURS[c.horizon]
        if age_h < 0 or age_h > MAX_HALF_LIVES * half_life:  # future items: never look ahead
            continue
        rel = relevance(c.entities, weights)
        if rel <= 0:
            continue
        decay = 0.5 ** (age_h / half_life)
        w = IMPACT_WEIGHT[c.impact] * decay * rel
        out.append(
            Contribution(
                id=s.id,
                title=s.item.title,
                feed=s.item.feed,
                url=s.item.url,
                published_at=s.item.published_at,
                entities=c.entities,
                sentiment=c.sentiment,
                impact=c.impact,
                horizon=c.horizon,
                relevance=rel,
                decay=decay,
                weight=w,
                contribution=w * c.sentiment,
                reason=c.reason,
            )
        )
    return out


def news_score(
    symbol: str, items: list[ScoredNewsItem], weights: Mapping[str, float], now: datetime, top_n: int = 5
) -> NewsScore:
    contribs = contributions(items, weights, now)
    total_w = sum(c.weight for c in contribs)
    score = sum(c.contribution for c in contribs) / (total_w + DAMPING)
    in_window = [s for s in items if s.item.published_at <= now]
    return NewsScore(
        symbol=symbol,
        score=score,
        label="bullish" if score >= LABEL_THRESHOLD else "bearish" if score <= -LABEL_THRESHOLD else "neutral",
        n_items_seen=len(in_window),
        n_used=len(contribs),
        n_unclassified=sum(s.status == "unclassified" for s in in_window),
        n_dropped=sum(s.status == "dropped" for s in in_window),
        total_weight=total_w,
        top=sorted(contribs, key=lambda c: -abs(c.contribution))[:top_n],
    )


IMPACT_RANK = {Impact.LOW: 0, Impact.MED: 1, Impact.HIGH: 2}


def relevant_items(
    items: Iterable[ScoredNewsItem], weights: Mapping[str, float], min_impact: Impact = Impact.LOW
) -> list[dict]:
    """Classified items relevant to one index, newest first, as plain dicts (for UIs and tools)."""
    rows = []
    for s in items:
        c = s.classification
        if s.status != "classified" or c is None or not c.relevant or IMPACT_RANK[c.impact] < IMPACT_RANK[min_impact]:
            continue
        rel = relevance(c.entities, weights)
        if rel <= 0:
            continue
        rows.append(
            {
                "id": s.id,
                "published_at": s.item.published_at.isoformat(),
                "feed": s.item.feed,
                "title": s.item.title,
                "url": s.item.url,
                "sentiment": c.sentiment,
                "impact": c.impact.value,
                "horizon": c.horizon.value,
                "entities": c.entities,
                "relevance": rel,
                "reason": c.reason,
                "model": s.model,
                "source": s.item.source.value,
            }
        )
    return sorted(rows, key=lambda r: r["published_at"], reverse=True)
