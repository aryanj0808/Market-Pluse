"""The one-call market digest: what `get_market_snapshot` returns and what Ask-Claude is given.

Shared by signals-mcp and the gateway so Claude always sees exactly the same data, whichever door
the question came through.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from mp_core.models import derived_source
from mp_core.snapshot import MarketSnapshot


def compact(x: Any, digits: int = 4) -> Any:
    """JSON-ready, with floats rounded - read by a model, not a calculator."""
    if isinstance(x, BaseModel):
        x = x.model_dump(mode="json", exclude_none=True)
    if isinstance(x, float):
        if not math.isfinite(x):
            return None
        return round(x, 2) if abs(x) >= 100 else round(x, digits)
    if isinstance(x, dict):
        return {k: compact(v, digits) for k, v in x.items()}
    if isinstance(x, list | tuple):
        return [compact(v, digits) for v in x]
    return x


def snapshot_parts(snap: MarketSnapshot) -> list:
    return [p for p in (snap.quote, snap.expected_range, snap.levels) if p is not None]


def levels_view(snap: MarketSnapshot, max_sigma: float | None = None) -> list[dict]:
    if snap.levels is None:
        return []
    spot = snap.quote.value.ltp
    sigma = snap.signal.sigma_points if snap.signal else None
    rows = []
    for lv in snap.levels.value:
        dist = lv.price - spot
        if max_sigma is not None and sigma and abs(dist) > max_sigma * sigma:
            continue
        rows.append(
            {
                "name": lv.name,
                "price": lv.price,
                "basis": lv.basis,
                "design_kind": lv.kind.value,
                "role_now": "resistance" if dist > 0 else "support" if dist < 0 else "at price",
                "distance_pts": dist,
                "distance_sigma": dist / sigma if sigma else None,
            }
        )
    return sorted(rows, key=lambda r: abs(r["distance_pts"]))


def signal_view(snap: MarketSnapshot, brief: bool = False) -> dict | None:
    sig = snap.signal
    if sig is None:
        return None
    d = sig.model_dump(mode="json", exclude_none=True)
    if brief:
        d.pop("components", None)
        d["reasons"] = [r for r in d["reasons"] if r["effect"] != 0 or r["component"] == "risk"][:8]
    return d


def market_digest_data(snap: MarketSnapshot) -> dict:
    """Everything in one dict: quote, future, expected range, IV term structure, PCR/max pain,
    levels within 2 sigma, news score, the signal (brief), top movers, macro, stale sections."""
    q = snap.quote.value
    data: dict = {
        "computed_at": snap.computed_at.isoformat(),
        "version": snap.version,
        "quote": {
            "ltp": q.ltp,
            "change_pct": q.change_pct,
            "high": q.high,
            "low": q.low,
            "prev_close": q.prev_close,
        },
        "stale_sections": snap.stale_sections,
        "errors": snap.errors,
    }
    if snap.future:
        f = snap.future.value
        data["future"] = {
            "ltp": f.ltp,
            "basis_points": f.basis_points,
            "oi": f.oi,
            "buildup_15m": [o.buildup.value for o in f.buildup_15m[-4:]],
        }
    if snap.expected_range:
        data["expected_range"] = {
            k: {
                "days": v.calendar_days,
                "sigma_points": v.sigma_points,
                "one_sigma": [v.one_sigma.lower, v.one_sigma.upper],
                "two_sigma": [v.two_sigma.lower, v.two_sigma.upper],
                "iv": v.iv,
            }
            for k, v in snap.expected_range.value.items()
        }
    data["expiries"] = [
        {
            "expiry": str(e.value.expiry),
            "days": e.value.days_to_expiry,
            "atm_iv": e.value.iv.atm_iv,
            "skew_25d": e.value.iv.skew_25d,
            "pcr_oi": e.value.pcr.pcr_oi,
            "max_pain": e.value.pcr.max_pain,
        }
        for e in snap.expiries
    ]
    data["levels"] = levels_view(snap, max_sigma=2.0)
    if snap.news:
        n = snap.news.value
        data["news"] = {
            "score": n.score,
            "label": n.label,
            "n_used": n.n_used,
            "stale": snap.news.stale,
            "top": [
                {"title": c.title, "published_at": c.published_at.isoformat(), "contribution": c.contribution}
                for c in n.top[:3]
            ],
        }
    data["signal"] = signal_view(snap, brief=True)
    if snap.constituents:
        movers = sorted(snap.constituents.value, key=lambda c: -abs(c.weight * c.change_pct))[:5]
        data["top_movers"] = [
            {
                "symbol": c.key.underlying,
                "change_pct": c.change_pct,
                "contribution_pct_pts": c.weight * c.change_pct / 100,
            }
            for c in movers
        ]
    if snap.macro:
        data["macro"] = {k: v.value for k, v in snap.macro if v is not None}
    return data


def market_digest(snap: MarketSnapshot, now: datetime) -> dict:
    """{symbol, source, as_of, stale, now, data} - provenance of the oldest/stalest critical part."""
    parts = [p.restamp(now) for p in snapshot_parts(snap)]
    return {
        "symbol": snap.symbol,
        "source": derived_source([p.source for p in parts]).value,
        "as_of": min(p.as_of for p in parts).isoformat(),
        "stale": any(p.stale for p in parts),
        "now": now.isoformat(),
        "data": compact(market_digest_data(snap)),
    }
