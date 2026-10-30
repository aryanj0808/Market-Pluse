"""Broker-neutral instrument identity.

Instruments are keyed by (exchange, underlying, expiry, strike, option_type). Broker
tokens (Kite instrument_token, Upstox instrument_key, Fyers symbol) are mapped inside
each adapter and never leak into core.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import model_validator

from mp_core.models.common import StrictModel


class Exchange(StrEnum):
    NSE = "NSE"
    BSE = "BSE"


class OptionType(StrEnum):
    CE = "CE"
    PE = "PE"


class InstrumentKind(StrEnum):
    INDEX = "index"
    EQUITY = "equity"
    FUTURE = "future"
    OPTION = "option"


class InstrumentKey(StrictModel):
    exchange: Exchange
    underlying: str
    expiry: date | None = None
    strike: float | None = None
    option_type: OptionType | None = None
    is_index: bool = True  # False for single stocks (constituents)

    @model_validator(mode="after")
    def _check_shape(self) -> InstrumentKey:
        if not self.underlying or self.underlying != self.underlying.upper():
            raise ValueError(f"underlying must be non-empty upper-case, got {self.underlying!r}")
        has_strike, has_type = self.strike is not None, self.option_type is not None
        if has_strike != has_type:
            raise ValueError("strike and option_type must be given together")
        if has_strike and self.expiry is None:
            raise ValueError("options need an expiry")
        if self.strike is not None and self.strike <= 0:
            raise ValueError(f"strike must be positive, got {self.strike}")
        return self

    @property
    def kind(self) -> InstrumentKind:
        if self.option_type is not None:
            return InstrumentKind.OPTION
        if self.expiry is not None:
            return InstrumentKind.FUTURE
        return InstrumentKind.INDEX if self.is_index else InstrumentKind.EQUITY

    @property
    def symbol(self) -> str:
        """`NSE:BANKNIFTY` - the spot/underlying symbol used across the API."""
        return f"{self.exchange}:{self.underlying}"

    @property
    def id(self) -> str:
        """Stable string id, e.g. `NSE:BANKNIFTY:2026-11-24:52000:CE`."""
        parts = [self.exchange.value, self.underlying]
        if self.expiry is not None:
            parts.append(self.expiry.isoformat())
        if self.strike is not None and self.option_type is not None:
            parts += [f"{self.strike:g}", self.option_type.value]
        return ":".join(parts)

    def underlying_key(self) -> InstrumentKey:
        return InstrumentKey(exchange=self.exchange, underlying=self.underlying, is_index=self.is_index)

    @classmethod
    def parse(cls, text: str, *, is_index: bool = True) -> InstrumentKey:
        """Inverse of `.id`. Accepts `NSE:BANKNIFTY`, `NSE:BANKNIFTY:2026-11-24`, and
        `NSE:BANKNIFTY:2026-11-24:52000:CE`."""
        parts = text.strip().split(":")
        if len(parts) not in (2, 3, 5):
            raise ValueError(f"cannot parse instrument {text!r}")
        kwargs: dict = {"exchange": parts[0].upper(), "underlying": parts[1].upper(), "is_index": is_index}
        if len(parts) >= 3:
            kwargs["expiry"] = date.fromisoformat(parts[2])
        if len(parts) == 5:
            kwargs["strike"] = float(parts[3])
            kwargs["option_type"] = parts[4].upper()
        return cls(**kwargs)

    def __str__(self) -> str:
        return self.id
