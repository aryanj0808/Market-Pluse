"""NSE F&O bhavcopy (end-of-day, with OI) - the first real-history source.

Two formats exist:
  * UDiFF (from 8 Jul 2024): BhavCopy_NSE_FO_0_0_0_YYYYMMDD_F_0000.csv.zip
  * legacy (before):         foDDMONYYYYbhav.csv.zip
Both are parsed into broker-neutral `FoEodRecord`s, detected by header.

NOTE: written against NSE's published column layout. It could not be checked against a
live download from the development machine (nseindia.com is blocked by its network);
`parse_bhavcopy` fails loudly on any unexpected header, so a layout change will surface.
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections.abc import Iterable, Iterator
from datetime import date, datetime
from pathlib import Path
from typing import Literal

import httpx
from pydantic import Field, model_validator

from mp_core.models import Exchange, InstrumentKey, OptionType, StrictModel

UDIFF_START = date(2024, 7, 8)
UDIFF_REQUIRED = {
    "TradDt",
    "FinInstrmTp",
    "TckrSymb",
    "XpryDt",
    "StrkPric",
    "OptnTp",
    "OpnPric",
    "HghPric",
    "LwPric",
    "ClsPric",
    "SttlmPric",
    "UndrlygPric",
    "OpnIntrst",
    "ChngInOpnIntrst",
    "TtlTradgVol",
    "NewBrdLotQty",
}
LEGACY_REQUIRED = {
    "INSTRUMENT",
    "SYMBOL",
    "EXPIRY_DT",
    "STRIKE_PR",
    "OPTION_TYP",
    "OPEN",
    "HIGH",
    "LOW",
    "CLOSE",
    "SETTLE_PR",
    "CONTRACTS",
    "OPEN_INT",
    "CHG_IN_OI",
    "TIMESTAMP",
}
INDEX_TYPES_UDIFF = {"IDO": "option", "IDF": "future"}
STOCK_TYPES_UDIFF = {"STO": "option", "STF": "future"}
LEGACY_TYPES = {
    "OPTIDX": ("option", True),
    "FUTIDX": ("future", True),
    "OPTSTK": ("option", False),
    "FUTSTK": ("future", False),
}


class FoEodRecord(StrictModel):
    trade_date: date
    key: InstrumentKey
    open: float = Field(ge=0)  # 0 when the contract did not trade
    high: float = Field(ge=0)
    low: float = Field(ge=0)
    close: float = Field(ge=0)
    settle: float = Field(ge=0)
    underlying_price: float | None = Field(default=None, gt=0)  # UDiFF only
    oi: int = Field(ge=0)
    oi_change: int
    volume: int = Field(ge=0)
    volume_unit: Literal["contracts", "units"]
    lot_size: int | None = Field(default=None, gt=0)  # UDiFF only

    @model_validator(mode="after")
    def _check(self) -> FoEodRecord:
        if self.high and self.low and self.high < self.low:
            raise ValueError(f"{self.key.id} {self.trade_date}: high {self.high} < low {self.low}")
        return self


def _f(v: str) -> float:
    v = v.strip()
    return float(v) if v and v != "-" else 0.0


def _i(v: str) -> int:
    return int(round(_f(v)))


def _parse_udiff(rows: Iterable[dict[str, str]], symbols: set[str] | None) -> Iterator[FoEodRecord]:
    for r in rows:
        tp = r["FinInstrmTp"].strip()
        kind = INDEX_TYPES_UDIFF.get(tp) or STOCK_TYPES_UDIFF.get(tp)
        sym = r["TckrSymb"].strip().upper()
        if kind is None or (symbols and sym not in symbols):
            continue
        is_option = kind == "option"
        key = InstrumentKey(
            exchange=Exchange.NSE,
            underlying=sym,
            expiry=date.fromisoformat(r["XpryDt"].strip()),
            strike=_f(r["StrkPric"]) if is_option else None,
            option_type=OptionType(r["OptnTp"].strip()) if is_option else None,
            is_index=tp in INDEX_TYPES_UDIFF,
        )
        und = _f(r["UndrlygPric"])
        lot = _i(r["NewBrdLotQty"])
        yield FoEodRecord(
            trade_date=date.fromisoformat(r["TradDt"].strip()),
            key=key,
            open=_f(r["OpnPric"]),
            high=_f(r["HghPric"]),
            low=_f(r["LwPric"]),
            close=_f(r["ClsPric"]),
            settle=_f(r["SttlmPric"]),
            underlying_price=und or None,
            oi=_i(r["OpnIntrst"]),
            oi_change=_i(r["ChngInOpnIntrst"]),
            volume=_i(r["TtlTradgVol"]),
            volume_unit="units",
            lot_size=lot or None,
        )


def _parse_legacy(rows: Iterable[dict[str, str]], symbols: set[str] | None) -> Iterator[FoEodRecord]:
    for r in rows:
        spec = LEGACY_TYPES.get(r["INSTRUMENT"].strip())
        sym = r["SYMBOL"].strip().upper()
        if spec is None or (symbols and sym not in symbols):
            continue
        kind, is_index = spec
        is_option = kind == "option"
        key = InstrumentKey(
            exchange=Exchange.NSE,
            underlying=sym,
            expiry=datetime.strptime(r["EXPIRY_DT"].strip(), "%d-%b-%Y").date(),  # noqa: DTZ007 - date only
            strike=_f(r["STRIKE_PR"]) if is_option else None,
            option_type=OptionType(r["OPTION_TYP"].strip()) if is_option else None,
            is_index=is_index,
        )
        yield FoEodRecord(
            trade_date=datetime.strptime(r["TIMESTAMP"].strip(), "%d-%b-%Y").date(),  # noqa: DTZ007
            key=key,
            open=_f(r["OPEN"]),
            high=_f(r["HIGH"]),
            low=_f(r["LOW"]),
            close=_f(r["CLOSE"]),
            settle=_f(r["SETTLE_PR"]),
            oi=_i(r["OPEN_INT"]),
            oi_change=_i(r["CHG_IN_OI"]),
            volume=_i(r["CONTRACTS"]),
            volume_unit="contracts",
        )


def parse_bhavcopy(text: str, symbols: Iterable[str] | None = None) -> list[FoEodRecord]:
    """Parse a bhavcopy CSV (either format). `symbols` filters underlyings, e.g. {"BANKNIFTY"}."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    header = {h.strip() for h in (reader.fieldnames or []) if h}
    wanted = {s.split(":")[-1].upper() for s in symbols} if symbols else None
    rows = ({(k or "").strip(): (v or "") for k, v in r.items()} for r in reader)
    if header >= UDIFF_REQUIRED:
        return list(_parse_udiff(rows, wanted))
    if header >= LEGACY_REQUIRED:
        return list(_parse_legacy(rows, wanted))
    raise ValueError(f"unrecognised bhavcopy header: {sorted(header)}")


def read_bhavcopy_zip(path: Path, symbols: Iterable[str] | None = None) -> list[FoEodRecord]:
    with zipfile.ZipFile(path) as zf:
        csvs = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if len(csvs) != 1:
            raise ValueError(f"{path}: expected one CSV inside, found {csvs}")
        return parse_bhavcopy(zf.read(csvs[0]).decode("utf-8"), symbols)


def bhavcopy_url(d: date) -> str:
    if d >= UDIFF_START:
        return f"https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"
    mon = d.strftime("%b").upper()
    return (
        f"https://nsearchives.nseindia.com/content/historical/DERIVATIVES/{d:%Y}/{mon}/fo{d:%d}{mon}{d:%Y}bhav.csv.zip"
    )


def download_bhavcopy(d: date, dest_dir: Path, client: httpx.Client | None = None) -> Path:
    """Download one day's archive to `dest_dir`. Raises on HTTP errors or non-zip content
    (NSE and corporate proxies return HTML error pages with status 200 surprisingly often)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    url = bhavcopy_url(d)
    own = client is None
    client = client or httpx.Client(
        headers={"User-Agent": "Mozilla/5.0 (market-pulse)"}, timeout=30, follow_redirects=True
    )
    try:
        resp = client.get(url)
        resp.raise_for_status()
        if not resp.content.startswith(b"PK"):
            raise ValueError(f"{url}: response is not a zip (got {resp.content[:60]!r})")
        out = dest_dir / url.rsplit("/", 1)[1]
        out.write_bytes(resp.content)
        return out
    finally:
        if own:
            client.close()
