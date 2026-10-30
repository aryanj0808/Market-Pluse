"""Open-interest build-up classification.

    price up   + OI up   -> long build-up     (fresh longs)
    price down + OI up   -> short build-up    (fresh shorts)
    price up   + OI down -> short covering    (shorts exiting)
    price down + OI down -> long unwinding    (longs exiting)

Moves smaller than the thresholds are NEUTRAL rather than forced into a bucket.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum

from pydantic import Field

from mp_core.analytics.options import option_mark
from mp_core.models import AwareDatetime, Candle, OptionChain, OptionType, StrictModel


class Buildup(StrEnum):
    LONG_BUILDUP = "long_buildup"
    SHORT_BUILDUP = "short_buildup"
    SHORT_COVERING = "short_covering"
    LONG_UNWINDING = "long_unwinding"
    NEUTRAL = "neutral"

    @property
    def bias(self) -> int:
        """+1 bullish, -1 bearish, 0 none (for the underlying when applied to futures/calls)."""
        return {"long_buildup": 1, "short_covering": 1, "short_buildup": -1, "long_unwinding": -1}.get(self.value, 0)


def classify_buildup(
    price_change_pct: float, oi_change_pct: float, *, price_eps_pct: float = 0.02, oi_eps_pct: float = 0.1
) -> Buildup:
    if abs(price_change_pct) < price_eps_pct or abs(oi_change_pct) < oi_eps_pct:
        return Buildup.NEUTRAL
    up, oi_up = price_change_pct > 0, oi_change_pct > 0
    if up and oi_up:
        return Buildup.LONG_BUILDUP
    if not up and oi_up:
        return Buildup.SHORT_BUILDUP
    if up:
        return Buildup.SHORT_COVERING
    return Buildup.LONG_UNWINDING


class BuildupObs(StrictModel):
    start: AwareDatetime
    end: AwareDatetime
    price_change_pct: float
    oi_change_pct: float
    buildup: Buildup


def futures_buildup(
    candles: Sequence[Candle], *, price_eps_pct: float = 0.02, oi_eps_pct: float = 0.1
) -> list[BuildupObs]:
    """Classify each bar vs the previous one (pass e.g. 15m futures candles)."""
    if any(c.oi is None for c in candles):
        raise ValueError("futures_buildup needs candles with OI (index candles have none)")
    if len({c.key for c in candles}) > 1:
        raise ValueError("candles span more than one contract; split at the roll")
    out = []
    for a, b in zip(candles, candles[1:], strict=False):
        dp = 100.0 * (b.close - a.close) / a.close
        doi = 100.0 * (b.oi - a.oi) / a.oi if a.oi else 0.0
        out.append(
            BuildupObs(
                start=a.ts,
                end=b.ts,
                price_change_pct=dp,
                oi_change_pct=doi,
                buildup=classify_buildup(dp, doi, price_eps_pct=price_eps_pct, oi_eps_pct=oi_eps_pct),
            )
        )
    return out


class StrikeBuildup(StrictModel):
    strike: float
    option_type: OptionType
    price_change_pct: float
    oi_change: int
    oi_change_pct: float
    buildup: Buildup


class ChainBuildup(StrictModel):
    start: AwareDatetime
    end: AwareDatetime
    rows: list[StrikeBuildup]
    counts: dict[str, int] = Field(description="buildup -> number of contracts, per side")


def chain_buildup(
    prev: OptionChain, now: OptionChain, *, price_eps_pct: float = 1.0, oi_eps_pct: float = 1.0
) -> ChainBuildup:
    """Per-contract build-up between two snapshots of the same expiry. Option premiums move a
    lot more than futures, hence larger default thresholds."""
    if prev.expiry != now.expiry or prev.underlying != now.underlying:
        raise ValueError("chain_buildup needs two snapshots of the same underlying and expiry")
    if prev.ts >= now.ts:
        raise ValueError("prev snapshot must be older than now")
    before = {
        (r.strike, side): q
        for r in prev.rows
        for side, q in ((OptionType.CE, r.call), (OptionType.PE, r.put))
        if q is not None
    }
    rows: list[StrikeBuildup] = []
    for r in now.rows:
        for side, q in ((OptionType.CE, r.call), (OptionType.PE, r.put)):
            old = before.get((r.strike, side))
            if q is None or old is None or old.oi == 0 or option_mark(old) <= 0:
                continue
            dp = 100.0 * (option_mark(q) - option_mark(old)) / option_mark(old)
            doi = 100.0 * (q.oi - old.oi) / old.oi
            rows.append(
                StrikeBuildup(
                    strike=r.strike,
                    option_type=side,
                    price_change_pct=dp,
                    oi_change=q.oi - old.oi,
                    oi_change_pct=doi,
                    buildup=classify_buildup(dp, doi, price_eps_pct=price_eps_pct, oi_eps_pct=oi_eps_pct),
                )
            )
    counts: dict[str, int] = {}
    for x in rows:
        k = f"{x.option_type.value}.{x.buildup.value}"
        counts[k] = counts.get(k, 0) + 1
    return ChainBuildup(start=prev.ts, end=now.ts, rows=rows, counts=counts)
