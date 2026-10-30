"""The single interface every market-data source implements.

Read-only by design: there are no order, position or funds methods, and there never
will be. Adapters map broker tokens to `InstrumentKey` internally.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from datetime import date, datetime

from mp_core.models import (
    Candle,
    Constituent,
    InstrumentKey,
    Interval,
    MacroSnapshot,
    OptionChain,
    Quote,
    Source,
    Sourced,
    Tick,
)


class DataUnavailable(LookupError):
    """The provider has no data for this request (as opposed to bad data, which raises
    a pydantic ValidationError)."""


class MarketDataProvider(ABC):
    source: Source

    @abstractmethod
    async def get_quote(self, key: InstrumentKey) -> Sourced[Quote]: ...

    @abstractmethod
    async def get_candles(
        self, key: InstrumentKey, interval: Interval, start: datetime, end: datetime
    ) -> Sourced[list[Candle]]:
        """Bars whose open time is in [start, end]."""

    @abstractmethod
    async def get_expiries(self, underlying: InstrumentKey) -> list[date]: ...

    @abstractmethod
    async def get_option_chain(self, underlying: InstrumentKey, expiry: date) -> Sourced[OptionChain]: ...

    @abstractmethod
    async def get_constituents(self, underlying: InstrumentKey) -> Sourced[list[Constituent]]: ...

    @abstractmethod
    async def get_macro(self) -> MacroSnapshot: ...

    @abstractmethod
    def stream_ticks(self, keys: Sequence[InstrumentKey]) -> AsyncIterator[Tick]: ...

    async def close(self) -> None:  # noqa: B027 - optional hook
        """Release connections. Default: nothing to release."""
