"""Key price levels: prior day H/L/C, VWAP, pivots (classic + CPR), option OI walls.

Every Level carries a `basis` naming exactly which input produced it, so signals can cite it.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date
from enum import StrEnum

from pydantic import Field

from mp_core import market_calendar as cal
from mp_core.models import Candle, Interval, OptionChain, StrictModel


class LevelKind(StrEnum):
    SUPPORT = "support"
    RESISTANCE = "resistance"
    PIVOT = "pivot"
    REFERENCE = "reference"


class Level(StrictModel):
    name: str
    price: float = Field(gt=0)
    kind: LevelKind
    basis: str  # e.g. "prior_day.high", "pivot.classic.R1", "oi.max_call"


class PriorDay(StrictModel):
    day: date
    high: float
    low: float
    close: float


def prior_day(daily: Sequence[Candle], today: date) -> PriorDay:
    """Last completed daily bar strictly before `today`."""
    if any(c.interval is not Interval.D1 for c in daily):
        raise ValueError("prior_day needs daily candles")
    prior = [c for c in daily if c.ts.astimezone(cal.IST).date() < today]
    if not prior:
        raise ValueError(f"no daily bar before {today}")
    c = max(prior, key=lambda c: c.ts)
    return PriorDay(day=c.ts.astimezone(cal.IST).date(), high=c.high, low=c.low, close=c.close)


class Pivots(StrictModel):
    p: float
    r1: float
    r2: float
    r3: float
    s1: float
    s2: float
    s3: float
    cpr_tc: float  # central pivot range, top
    cpr_bc: float  # bottom

    @property
    def cpr_width_pct(self) -> float:
        return 100.0 * (self.cpr_tc - self.cpr_bc) / self.p


def pivots(high: float, low: float, close: float) -> Pivots:
    """Classic floor pivots and the Central Pivot Range from the prior session."""
    if not low <= close <= high:
        raise ValueError(f"need low <= close <= high, got {low}, {close}, {high}")
    p = (high + low + close) / 3.0
    bc = (high + low) / 2.0
    tc = 2.0 * p - bc
    rng = high - low
    return Pivots(
        p=p,
        r1=2 * p - low,
        s1=2 * p - high,
        r2=p + rng,
        s2=p - rng,
        r3=high + 2 * (p - low),
        s3=low - 2 * (high - p),
        cpr_tc=max(tc, bc),
        cpr_bc=min(tc, bc),
    )


class Vwap(StrictModel):
    vwap: float
    sd: float  # volume-weighted standard deviation of typical price
    upper_1sd: float
    lower_1sd: float
    volume: int
    bars: int


def vwap(candles: Sequence[Candle]) -> Vwap:
    """Session VWAP on typical price (H+L+C)/3. Index candles carry no volume; pass the
    same-session futures candles. Raises if there is no volume to weight by."""
    if not candles:
        raise ValueError("vwap needs at least one candle")
    days = {c.ts.astimezone(cal.IST).date() for c in candles}
    if len(days) != 1:
        raise ValueError(f"vwap candles must be from a single session, got {sorted(days)}")
    vol = sum(c.volume for c in candles)
    if vol <= 0:
        raise ValueError(f"no volume on {candles[0].key.id} - use futures candles for an index VWAP")
    tps = [(c.high + c.low + c.close) / 3.0 for c in candles]
    vw = sum(tp * c.volume for tp, c in zip(tps, candles, strict=True)) / vol
    var = sum(c.volume * (tp - vw) ** 2 for tp, c in zip(tps, candles, strict=True)) / vol
    sd = math.sqrt(var)
    return Vwap(vwap=vw, sd=sd, upper_1sd=vw + sd, lower_1sd=vw - sd, volume=vol, bars=len(candles))


def oi_levels(chain: OptionChain, top_n: int = 2) -> list[Level]:
    """Highest call OI strikes act as resistance, highest put OI strikes as support. Also
    flags the strikes with the largest fresh OI addition today."""
    rows = [r for r in chain.rows if r.call is not None and r.put is not None]
    if not rows:
        return []
    out: list[Level] = []
    by_call = sorted(rows, key=lambda r: (-r.call.oi, r.strike))[:top_n]
    by_put = sorted(rows, key=lambda r: (-r.put.oi, -r.strike))[:top_n]
    for i, r in enumerate(by_call, 1):
        out.append(Level(name=f"Call OI #{i}", price=r.strike, kind=LevelKind.RESISTANCE, basis=f"oi.call_rank{i}"))
    for i, r in enumerate(by_put, 1):
        out.append(Level(name=f"Put OI #{i}", price=r.strike, kind=LevelKind.SUPPORT, basis=f"oi.put_rank{i}"))
    add_c = max(rows, key=lambda r: (r.call.oi_change, -r.strike))
    add_p = max(rows, key=lambda r: (r.put.oi_change, r.strike))
    if add_c.call.oi_change > 0:
        out.append(
            Level(name="Max call OI addition", price=add_c.strike, kind=LevelKind.RESISTANCE, basis="oi.call_max_add")
        )
    if add_p.put.oi_change > 0:
        out.append(
            Level(name="Max put OI addition", price=add_p.strike, kind=LevelKind.SUPPORT, basis="oi.put_max_add")
        )
    return out


def compute_levels(
    pd: PriorDay, session_vwap: Vwap | None = None, chain: OptionChain | None = None, top_n: int = 2
) -> list[Level]:
    """All levels, sorted by price. Inputs that are missing are simply not used."""
    pv = pivots(pd.high, pd.low, pd.close)
    R, S, P, REF = LevelKind.RESISTANCE, LevelKind.SUPPORT, LevelKind.PIVOT, LevelKind.REFERENCE
    out = [
        Level(name="Prior day high", price=pd.high, kind=REF, basis="prior_day.high"),
        Level(name="Prior day low", price=pd.low, kind=REF, basis="prior_day.low"),
        Level(name="Prior day close", price=pd.close, kind=REF, basis="prior_day.close"),
        Level(name="Pivot", price=pv.p, kind=P, basis="pivot.classic.P"),
        Level(name="R1", price=pv.r1, kind=R, basis="pivot.classic.R1"),
        Level(name="R2", price=pv.r2, kind=R, basis="pivot.classic.R2"),
        Level(name="R3", price=pv.r3, kind=R, basis="pivot.classic.R3"),
        Level(name="S1", price=pv.s1, kind=S, basis="pivot.classic.S1"),
        Level(name="S2", price=pv.s2, kind=S, basis="pivot.classic.S2"),
        Level(name="S3", price=pv.s3, kind=S, basis="pivot.classic.S3"),
        Level(name="CPR top", price=pv.cpr_tc, kind=P, basis="pivot.cpr.TC"),
        Level(name="CPR bottom", price=pv.cpr_bc, kind=P, basis="pivot.cpr.BC"),
    ]
    if session_vwap is not None:
        out += [
            Level(name="VWAP", price=session_vwap.vwap, kind=P, basis="vwap.session"),
            Level(name="VWAP +1sd", price=session_vwap.upper_1sd, kind=R, basis="vwap.upper_1sd"),
            Level(name="VWAP -1sd", price=session_vwap.lower_1sd, kind=S, basis="vwap.lower_1sd"),
        ]
    if chain is not None:
        out += oi_levels(chain, top_n)
    return sorted(out, key=lambda lv: (lv.price, lv.basis))


def nearest(levels: Sequence[Level], price: float) -> tuple[Level | None, Level | None]:
    """(closest level strictly below, closest level strictly above) `price`."""
    below = [lv for lv in levels if lv.price < price]
    above = [lv for lv in levels if lv.price > price]
    return (
        max(below, key=lambda lv: lv.price) if below else None,
        min(above, key=lambda lv: lv.price) if above else None,
    )
