"""Index registry loaded from `data/indices.toml`."""

from __future__ import annotations

import tomllib
from functools import cache
from importlib import resources

from pydantic import Field

from mp_core.models import Exchange, InstrumentKey, StrictModel

WEEKDAYS = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4}

# Index-wide news tags the classifier may assign (besides constituent tickers).
NEWS_TAGS = {
    "SECTOR_BANKS": "banking sector as a whole (deposits, credit growth, asset quality, bank regulation)",
    "SECTOR_FINANCIALS": "NBFCs, insurers, capital markets, fintech",
    "RBI_POLICY": "RBI monetary policy, liquidity, rates, RBI regulatory actions",
    "MACRO_INDIA": "Indian macro data and fiscal policy (GDP, inflation, budget, rupee)",
    "FLOWS": "FII/DII flows, large block deals, index rebalancing",
    "GLOBAL": "global markets and events (Fed, crude, US yields, geopolitics)",
}


class ConstituentSpec(StrictModel):
    symbol: str
    name: str
    weight: float = Field(gt=0, le=100)
    beta: float = Field(gt=0)


class IndexSpec(StrictModel):
    underlying: str
    exchange: Exchange
    name: str
    strike_step: float = Field(gt=0)
    lot_size: int = Field(gt=0)
    weekly_expiry: str  # "" or a WEEKDAYS key
    monthly_expiry: str  # "LAST_<WEEKDAY>"
    verified: bool
    constituents: list[ConstituentSpec] = []
    news_tags: dict[str, float] = {}  # index-wide news tag -> relevance (0..1)

    @property
    def key(self) -> InstrumentKey:
        return InstrumentKey(exchange=self.exchange, underlying=self.underlying)


def _validate(spec: IndexSpec) -> IndexSpec:
    if spec.weekly_expiry and spec.weekly_expiry not in WEEKDAYS:
        raise ValueError(f"{spec.underlying}: bad weekly_expiry {spec.weekly_expiry!r}")
    if not spec.monthly_expiry.startswith("LAST_") or spec.monthly_expiry[5:] not in WEEKDAYS:
        raise ValueError(f"{spec.underlying}: bad monthly_expiry {spec.monthly_expiry!r}")
    bad = {t: w for t, w in spec.news_tags.items() if t not in NEWS_TAGS or not 0 <= w <= 1}
    if bad:
        raise ValueError(f"{spec.underlying}: bad news_tags {bad}; tags must be in {sorted(NEWS_TAGS)}, weights 0..1")
    return spec


@cache
def load_indices() -> dict[str, IndexSpec]:
    raw = tomllib.loads(resources.files("mp_core.data").joinpath("indices.toml").read_text("utf-8"))
    return {name: _validate(IndexSpec(underlying=name, **body)) for name, body in raw.items()}


def news_vocabulary() -> dict[str, str]:
    """Allowed classifier entities: every known constituent ticker (-> its name) and every tag."""
    vocab = {c.symbol: c.name for spec in load_indices().values() for c in spec.constituents}
    return {**dict(sorted(vocab.items())), **NEWS_TAGS}


def get_index(symbol: str) -> IndexSpec:
    """Accepts `NSE:BANKNIFTY` or `BANKNIFTY`."""
    name = symbol.split(":")[-1].upper()
    try:
        return load_indices()[name]
    except KeyError:
        raise KeyError(f"unknown index {symbol!r}; known: {sorted(load_indices())}") from None
