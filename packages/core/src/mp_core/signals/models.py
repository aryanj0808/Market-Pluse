"""Signal models. A signal is a deterministic function of one MarketSnapshot."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from mp_core.models import AwareDatetime, Source, StrictModel

Bias = Literal["bullish", "bearish", "neutral"]
Component = Literal["technical", "options", "news", "risk", "data"]


class Reason(StrictModel):
    """One step of the argument, pointing at the exact snapshot input that produced it."""

    component: Component
    input: str  # path into the snapshot, e.g. "levels[vwap.session]", "expiries[2026-09-29].pcr.pcr_oi"
    value: float | str | None
    effect: float  # signed contribution to the component score (0 for informational reasons)
    text: str


class ComponentScore(StrictModel):
    name: Component
    score: float = Field(ge=-1, le=1)
    weight: float = Field(ge=0)  # nominal weight; 0 when the component was unavailable
    available: bool
    reasons: list[Reason]


class Zone(StrictModel):
    low: float
    high: float
    basis: list[str]  # level bases (e.g. "oi.put_rank1") or "expected_range.session"


class Target(StrictModel):
    price: float
    basis: str


class Sizing(StrictModel):
    instrument: str  # futures contract the sizing refers to
    lot_size: int
    entry: float
    stop_loss: float
    risk_per_lot: float  # INR
    max_loss: float  # INR, the configured budget
    lots: int  # floor(max_loss / risk_per_lot)
    loss_at_stop: float  # lots * risk_per_lot


class Signal(StrictModel):
    signal_id: str
    symbol: str
    ts: AwareDatetime  # snapshot computed_at
    snapshot_version: int
    source: Source  # synthetic if any input was
    status: Literal["active", "no_signal"]
    bias: Bias
    composite: float = Field(ge=-1, le=1)
    confidence: float = Field(ge=0, le=1)
    horizon: Literal["session", "next_session"]
    spot: float
    sigma_points: float  # 1-sigma move for the horizon (from the expected range)
    components: list[ComponentScore]
    buy_zone: Zone | None = None
    sell_zone: Zone | None = None
    entry: float | None = None
    stop_loss: float | None = None
    invalidation: float | None = None  # a close beyond this level invalidates the idea
    targets: list[Target] = []
    reward_risk: float | None = None
    sizing: Sizing | None = None
    reasons: list[Reason]
    no_signal_reasons: list[str] = []
    rules_version: str
