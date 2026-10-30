"""Market data models. Validators reject impossible data instead of passing it on."""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import Field, model_validator

from mp_core.models.common import AwareDatetime, Sourced, StrictModel
from mp_core.models.instruments import InstrumentKey, InstrumentKind, OptionType


class Interval(StrEnum):
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    H1 = "1h"
    D1 = "1d"

    @property
    def minutes(self) -> int:
        return {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "1d": 1440}[self.value]


class Tick(StrictModel):
    key: InstrumentKey
    ts: AwareDatetime
    ltp: float = Field(gt=0)
    volume: int = Field(ge=0, default=0)  # cumulative day volume
    oi: int | None = Field(ge=0, default=None)


class Candle(StrictModel):
    key: InstrumentKey
    interval: Interval
    ts: AwareDatetime  # bar open time
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: int = Field(ge=0, default=0)
    oi: int | None = Field(ge=0, default=None)

    @model_validator(mode="after")
    def _ohlc_consistent(self) -> Candle:
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError(
                f"inconsistent OHLC for {self.key.id} @ {self.ts}: "
                f"o={self.open} h={self.high} l={self.low} c={self.close}"
            )
        return self


class Quote(StrictModel):
    key: InstrumentKey
    ts: AwareDatetime
    ltp: float = Field(gt=0)
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    prev_close: float = Field(gt=0)
    volume: int = Field(ge=0, default=0)
    oi: int | None = Field(ge=0, default=None)

    @model_validator(mode="after")
    def _range_consistent(self) -> Quote:
        if not (self.low <= self.ltp <= self.high):
            raise ValueError(f"ltp {self.ltp} outside day range [{self.low}, {self.high}] for {self.key.id}")
        return self

    @property
    def change(self) -> float:
        return self.ltp - self.prev_close

    @property
    def change_pct(self) -> float:
        return 100.0 * self.change / self.prev_close


class OptionQuote(StrictModel):
    key: InstrumentKey
    ltp: float = Field(ge=0)
    bid: float = Field(ge=0)
    ask: float = Field(ge=0)
    volume: int = Field(ge=0)
    oi: int = Field(ge=0)
    oi_change: int  # vs previous trading day's close

    @model_validator(mode="after")
    def _check(self) -> OptionQuote:
        if self.key.kind is not InstrumentKind.OPTION:
            raise ValueError(f"OptionQuote needs an option key, got {self.key.id}")
        if self.ask and self.bid > self.ask:
            raise ValueError(f"crossed market bid={self.bid} > ask={self.ask} for {self.key.id}")
        return self


class OptionChainRow(StrictModel):
    strike: float = Field(gt=0)
    call: OptionQuote | None = None
    put: OptionQuote | None = None

    @model_validator(mode="after")
    def _check(self) -> OptionChainRow:
        for side, q, ot in (("call", self.call, OptionType.CE), ("put", self.put, OptionType.PE)):
            if q is not None and (q.key.strike != self.strike or q.key.option_type is not ot):
                raise ValueError(f"{side} quote {q.key.id} does not belong to strike {self.strike}")
        return self


class OptionChain(StrictModel):
    underlying: InstrumentKey
    expiry: date
    ts: AwareDatetime
    spot: float = Field(gt=0)
    future: float | None = Field(gt=0, default=None)  # same-expiry future, if available
    rows: list[OptionChainRow] = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> OptionChain:
        strikes = [r.strike for r in self.rows]
        if strikes != sorted(strikes) or len(set(strikes)) != len(strikes):
            raise ValueError("option chain strikes must be strictly increasing")
        for r in self.rows:
            for q in (r.call, r.put):
                if q is not None and (q.key.expiry != self.expiry or q.key.underlying != self.underlying.underlying):
                    raise ValueError(f"{q.key.id} does not belong to chain {self.underlying.id}/{self.expiry}")
        return self


class Constituent(StrictModel):
    key: InstrumentKey
    name: str
    weight: float = Field(ge=0, le=100)  # % of index
    ltp: float = Field(gt=0)
    prev_close: float = Field(gt=0)

    @property
    def change_pct(self) -> float:
        return 100.0 * (self.ltp - self.prev_close) / self.prev_close


class MacroSnapshot(StrictModel):
    """Each field carries its own provenance: VIX is intraday, FII/DII is end-of-day."""

    india_vix: Sourced[float] | None = None
    usdinr: Sourced[float] | None = None
    in10y_yield: Sourced[float] | None = None  # percent
    fii_net_cr: Sourced[float] | None = None  # cash market, INR crore
    dii_net_cr: Sourced[float] | None = None
