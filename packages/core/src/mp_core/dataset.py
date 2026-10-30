"""On-disk replay dataset format, shared by the synthetic generator, the live recorder
(Phase 3) and ReplayProvider.

    <dir>/manifest.json         Manifest (source, symbol, days, constituents, ...)
    <dir>/candles_1m.csv        key,ts,open,high,low,close,volume,oi    (all instruments)
    <dir>/ticks.jsonl.gz        one Tick per line
    <dir>/chains.jsonl.gz       one OptionChain per line
    <dir>/macro.jsonl           one MacroRow per line
    <dir>/events.json           ground-truth labels (synthetic datasets only)
"""

from __future__ import annotations

import csv
import gzip
import json
from collections.abc import Iterable, Iterator
from datetime import date
from pathlib import Path

from pydantic import Field

from mp_core.models import AwareDatetime, Candle, InstrumentKey, Interval, Source, StrictModel

FORMAT_VERSION = 1
CANDLE_FIELDS = ["key", "ts", "open", "high", "low", "close", "volume", "oi"]


class ConstituentInfo(StrictModel):
    symbol: str
    name: str
    weight: float  # % of index when the price was `base_price`
    base_price: float | None = None  # set => weight drifts with price (free-float cap weighting)


class Manifest(StrictModel):
    format_version: int = FORMAT_VERSION
    source: Source
    symbol: str  # e.g. NSE:BANKNIFTY
    days: list[date] = Field(min_length=1)
    constituents: list[ConstituentInfo] = []
    seed: int | None = None
    generator: str | None = None
    # How often each data kind updates in this dataset (seconds). Staleness TTLs are never set
    # tighter than 1.2x this, so a 5-minute chain cadence is not reported stale after 60 s.
    cadence_seconds: dict[str, float] = {}
    notes: str = ""


class MacroRow(StrictModel):
    ts: AwareDatetime
    india_vix: float | None = None
    usdinr: float | None = None
    in10y_yield: float | None = None
    fii_net_cr: float | None = None
    dii_net_cr: float | None = None
    flows_as_of: AwareDatetime | None = None  # publish time of the FII/DII figures


class DayLabel(StrictModel):
    """What the generator actually did on a day - lets tests check realism."""

    day: date
    regime: str
    realized_vol_annual: float
    gap_pct: float
    is_gap_day: bool
    jump_pct: float
    is_expiry_day: bool
    iv_crush: bool
    atm_iv_open: float
    atm_iv_close: float


def write_candles(path: Path, candles: Iterable[Candle]) -> int:
    n = 0
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(CANDLE_FIELDS)
        for c in candles:
            w.writerow(
                [c.key.id, c.ts.isoformat(), c.open, c.high, c.low, c.close, c.volume, "" if c.oi is None else c.oi]
            )
            n += 1
    return n


def read_candles(path: Path, index_underlying: str) -> Iterator[Candle]:
    """1-minute candles. Keys whose underlying is not the index are equities."""
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != CANDLE_FIELDS:
            raise ValueError(f"{path}: unexpected header {reader.fieldnames}")
        for row in reader:
            key = InstrumentKey.parse(row["key"], is_index=row["key"].split(":")[1] == index_underlying)
            yield Candle(
                key=key,
                interval=Interval.M1,
                ts=row["ts"],
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=int(row["volume"]),
                oi=int(row["oi"]) if row["oi"] else None,
            )


def write_jsonl(path: Path, rows: Iterable[StrictModel]) -> int:
    opener = gzip.open if path.suffix == ".gz" else open
    n = 0
    with opener(path, "wt", encoding="utf-8") as f:  # type: ignore[operator]
        for r in rows:
            f.write(r.model_dump_json())
            f.write("\n")
            n += 1
    return n


def iter_jsonl(path: Path) -> Iterator[dict]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as f:  # type: ignore[operator]
        for lineno, line in enumerate(f, 1):
            if line.strip():
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as e:
                    raise ValueError(f"{path}:{lineno}: invalid JSON ({e})") from e


def write_json(path: Path, model: StrictModel | list[StrictModel]) -> None:
    if isinstance(model, list):
        text = json.dumps([m.model_dump(mode="json") for m in model], indent=1)
    else:
        text = model.model_dump_json(indent=1)
    path.write_text(text, encoding="utf-8")


def read_manifest(directory: Path) -> Manifest:
    path = directory / "manifest.json"
    if not path.exists():
        raise FileNotFoundError(f"no manifest.json in {directory} - generate one with `mp synth`")
    m = Manifest.model_validate_json(path.read_text(encoding="utf-8"))
    if m.format_version != FORMAT_VERSION:
        raise ValueError(f"{path}: format_version {m.format_version}, expected {FORMAT_VERSION}")
    return m


__all__ = [
    "DayLabel",
    "Manifest",
    "MacroRow",
    "ConstituentInfo",
    "read_candles",
    "write_candles",
    "write_jsonl",
    "iter_jsonl",
    "write_json",
    "read_manifest",
]
