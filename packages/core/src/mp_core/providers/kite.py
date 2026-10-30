"""Zerodha Kite Connect v3 market-data adapter (read-only: quotes, candles, option chain - never orders).

Endpoints (https://kite.trade/docs/connect/v3/, base https://api.kite.trade, header X-Kite-Version: 3,
Authorization: token <api_key>:<access_token>):
    GET  /instruments/{NSE|NFO}                   instrument dump (CSV)
    GET  /quote?i=EX:SYM&i=...                     full quotes, <= 500 per call, ~1 request/s
    GET  /quote/ltp?i=...                          last prices
    GET  /instruments/historical/{token}/{interval}?from=&to=&oi=1    candles, ~3 requests/s
    POST /session/token                            request_token -> access_token (daily login)

Notes:
  * Kite has no option-chain endpoint: the chain is built from the NFO instrument list (strikes
    around spot for the expiry) and one batched quote call.
  * Kite quotes carry no previous-day OI. The adapter keeps each session's last OI in a small JSON
    file and reports OI change against it; on the first day (no file yet) it uses the first OI it
    saw that session and logs that the change is session-relative.
  * The quote API allows ~1 request/s: one combined poll per second feeds both the tick stream and
    get_quote (served from that cache), so lookups do not spend the budget.

STATUS: written against the documented API and tested with mocked responses only. Verify with
`mp kite-check` from a network that can reach api.kite.trade.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import logging
import re
import time
from collections.abc import AsyncIterator, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from pydantic import BaseModel, ConfigDict

from mp_core import market_calendar as cal
from mp_core.clock import Clock, SystemClock
from mp_core.models import (
    Candle,
    Constituent,
    InstrumentKey,
    Interval,
    MacroSnapshot,
    OptionChain,
    OptionChainRow,
    OptionQuote,
    OptionType,
    Quote,
    Source,
    Sourced,
    Tick,
    stamp,
)
from mp_core.net import ssl_verify
from mp_core.providers.base import DataUnavailable, MarketDataProvider
from mp_core.registry import get_index
from mp_core.settings import TTLs, get_settings

log = logging.getLogger(__name__)

BASE = "https://api.kite.trade"
LOGIN = "https://kite.zerodha.com/connect/login"
INDEX_SYMBOLS = {"BANKNIFTY": "NSE:NIFTY BANK", "NIFTY": "NSE:NIFTY 50", "FINNIFTY": "NSE:NIFTY FIN SERVICE"}
VIX_SYMBOL = "NSE:INDIA VIX"
INTERVALS = {
    Interval.M1: "minute",
    Interval.M5: "5minute",
    Interval.M15: "15minute",
    Interval.H1: "60minute",
    Interval.D1: "day",
}
STRIKES_EACH_SIDE = 25


class KiteError(RuntimeError):
    pass


class KiteAuthError(KiteError):
    """403 TokenException: missing, wrong or expired access token (they expire around 6 AM IST)."""


class _Loose(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _Ohlc(_Loose):
    open: float
    high: float
    low: float
    close: float  # previous session's close


class _Level(_Loose):
    price: float


class _Depth(_Loose):
    buy: list[_Level] = []
    sell: list[_Level] = []


class _Quote(_Loose):
    instrument_token: int
    last_price: float
    ohlc: _Ohlc
    volume: int | None = 0
    oi: float | None = None
    timestamp: str | None = None
    last_trade_time: str | None = None
    depth: _Depth | None = None


def _ist(ts: str | None) -> datetime | None:
    """Kite timestamps: '2026-10-20 11:00:00' (IST) or '2026-10-20T09:15:00+0530'."""
    if not ts:
        return None
    ts = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", ts.strip())
    t = datetime.fromisoformat(ts)
    return t if t.tzinfo else t.replace(tzinfo=cal.IST)


class KiteInstruments:
    """Map InstrumentKey <-> Kite 'EXCHANGE:TRADINGSYMBOL' (and instrument_token) from the dumps."""

    def __init__(self, nse_csv: str, nfo_csv: str) -> None:
        self.symbol: dict[str, str] = {}  # InstrumentKey.id -> "NFO:BANKNIFTY26OCT56000CE"
        self.token: dict[str, int] = {}  # InstrumentKey.id -> instrument_token
        self.key_by_symbol: dict[str, InstrumentKey] = {}
        self.options: dict[tuple[str, date], list[InstrumentKey]] = {}
        self.lot_size: dict[str, int] = {}
        for row in csv.DictReader(io.StringIO(nse_csv)):
            sym = f"NSE:{row['tradingsymbol']}"
            if row.get("segment") == "INDICES":
                und = next((u for u, s in INDEX_SYMBOLS.items() if s == sym), None)
                key = (
                    InstrumentKey(exchange="NSE", underlying=und)
                    if und
                    else (InstrumentKey(exchange="NSE", underlying="INDIAVIX") if sym == VIX_SYMBOL else None)
                )
            elif row.get("instrument_type") == "EQ":
                key = InstrumentKey(exchange="NSE", underlying=row["tradingsymbol"].upper(), is_index=False)
            else:
                key = None
            if key is not None:
                self._add(key, sym, int(row["instrument_token"]), None)
        for row in csv.DictReader(io.StringIO(nfo_csv)):
            und = row.get("name", "").upper()
            if und not in INDEX_SYMBOLS or not row.get("expiry"):
                continue
            exp = date.fromisoformat(row["expiry"])
            itype = row.get("instrument_type")
            if itype == "FUT":
                key = InstrumentKey(exchange="NSE", underlying=und, expiry=exp)
            elif itype in ("CE", "PE") and float(row.get("strike") or 0) > 0:
                key = InstrumentKey(
                    exchange="NSE", underlying=und, expiry=exp, strike=float(row["strike"]), option_type=itype
                )
                self.options.setdefault((und, exp), []).append(key)
            else:
                continue
            self._add(key, f"NFO:{row['tradingsymbol']}", int(row["instrument_token"]), int(row.get("lot_size") or 0))
        if InstrumentKey(exchange="NSE", underlying="BANKNIFTY").id not in self.symbol or not self.options:
            raise KiteError("instrument dump has no BANKNIFTY index or options - wrong or truncated file?")

    def _add(self, key: InstrumentKey, sym: str, token: int, lot: int | None) -> None:
        self.symbol[key.id], self.token[key.id], self.key_by_symbol[sym] = sym, token, key
        if lot:
            self.lot_size[key.id] = lot

    def kite_symbol(self, key: InstrumentKey) -> str:
        try:
            return self.symbol[key.id]
        except KeyError:
            raise DataUnavailable(f"{key.id} is not in the Kite instrument list (expired or not loaded?)") from None

    def expiries(self, underlying: str) -> list[date]:
        return sorted({e for (u, e) in self.options if u == underlying})


class OIBaseline:
    """Previous-session OI per contract, kept on disk (Kite quotes have no previous-day OI)."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.data: dict = json.loads(path.read_text("utf-8")) if path and path.exists() else {}
        self._warned = False

    def change(self, key: InstrumentKey, oi: int, today: date) -> int:
        rec = self.data.get(key.id)
        if rec is None or rec["day"] == today.isoformat() and "base" not in rec:
            rec = {"day": today.isoformat(), "base": oi, "approx": True, "last": oi}
            if not self._warned:
                self._warned = True
                log.warning(
                    "no previous-session OI on file yet: OI change is measured from this session's first "
                    "value until tomorrow"
                )
        elif rec["day"] != today.isoformat():  # first sight today: yesterday's last OI is the baseline
            rec = {"day": today.isoformat(), "base": rec["last"], "approx": False, "last": oi}
        rec["last"] = oi
        self.data[key.id] = rec
        return oi - int(rec["base"])

    def save(self) -> None:
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data), encoding="utf-8")


class _RateLimit:
    def __init__(self, per_second: float) -> None:
        self.gap = 1.0 / per_second
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.perf_counter()
            if now < self._next:
                await asyncio.sleep(self._next - now)
            self._next = max(now, self._next) + self.gap


class KiteProvider(MarketDataProvider):
    source = Source.KITE

    def __init__(
        self,
        api_key: str,
        access_token: str,
        instruments: KiteInstruments,
        *,
        clock: Clock | None = None,
        client: httpx.AsyncClient | None = None,
        poll_seconds: float = 1.0,
        ttls: TTLs | None = None,
        oi_baseline: OIBaseline | None = None,
        quote_rate: float = 1.0,
        history_rate: float = 3.0,
    ) -> None:
        if not (api_key and access_token):
            raise KiteAuthError("KITE_API_KEY / KITE_ACCESS_TOKEN not set - run `mp kite-login`")
        self.clock = clock or SystemClock()
        self.instruments = instruments
        self.poll_seconds = poll_seconds
        self.ttl = ttls or get_settings().ttl
        self.oi = oi_baseline or OIBaseline(None)
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0), verify=ssl_verify())
        self._headers = {"X-Kite-Version": "3", "Authorization": f"token {api_key}:{access_token}"}
        self._quote_limit = _RateLimit(quote_rate)
        self._history_limit = _RateLimit(history_rate)
        self._cache: dict[str, tuple[float, _Quote]] = {}  # kite symbol -> (wall time fetched, quote)

    # ------------------------------------------------------------------ http
    async def _get(self, path: str, params: list[tuple[str, str]] | dict | None = None, *, limit: _RateLimit):
        for attempt in range(3):
            await limit.wait()
            try:
                r = await self.client.get(BASE + path, params=params, headers=self._headers)
            except httpx.HTTPError as ex:
                if attempt == 2:
                    raise KiteError(f"{path}: {type(ex).__name__}") from None
                continue
            if r.status_code == 403:
                raise KiteAuthError("Kite rejected the access token (expired? they last until ~6 AM IST)")
            if r.status_code == 429 or r.status_code >= 500:
                if attempt == 2:
                    raise KiteError(f"{path}: HTTP {r.status_code}")
                await asyncio.sleep(1.0 * (attempt + 1))
                continue
            try:
                body = r.json()
            except ValueError:
                raise KiteError(f"{path}: HTTP {r.status_code}, not JSON (proxy page?)") from None
            if r.status_code != 200 or body.get("status") != "success":
                raise KiteError(f"{path}: HTTP {r.status_code} {body.get('error_type', '')} {body.get('message', '')}")
            return body["data"]
        raise KiteError(f"{path}: retries exhausted")

    async def _quotes(self, symbols: Sequence[str]) -> dict[str, _Quote]:
        out: dict[str, _Quote] = {}
        for i in range(0, len(symbols), 500):
            data = await self._get("/quote", [("i", s) for s in symbols[i : i + 500]], limit=self._quote_limit)
            now = time.time()
            for sym, entry in data.items():
                q = _Quote.model_validate(entry)
                self._cache[sym] = (now, q)
                out[sym] = q
        return out

    def _now(self) -> datetime:
        return self.clock.now()

    def _stamp(self, value, as_of: datetime, ttl: float):
        return stamp(value, source=self.source, as_of=as_of, now=self._now(), ttl_seconds=ttl)

    def _quote_model(self, key: InstrumentKey, q: _Quote) -> Quote:
        ts = _ist(q.last_trade_time) or _ist(q.timestamp) or self._now()
        return Quote(
            key=key,
            ts=ts,
            ltp=q.last_price,
            open=q.ohlc.open or q.last_price,
            high=max(q.ohlc.high, q.last_price),
            low=min(q.ohlc.low or q.last_price, q.last_price),
            prev_close=q.ohlc.close or q.last_price,
            volume=q.volume or 0,
            oi=int(q.oi) if q.oi else None,
        )

    # ------------------------------------------------------------------ interface
    async def get_quote(self, key: InstrumentKey) -> Sourced[Quote]:
        sym = self.instruments.kite_symbol(key)
        hit = self._cache.get(sym)
        if hit is None or time.time() - hit[0] > max(1.5, 1.5 * self.poll_seconds):
            q = (await self._quotes([sym])).get(sym)
            if q is None:
                raise DataUnavailable(f"no Kite quote for {key.id}")
        else:
            q = hit[1]  # served from the 1-per-second poll: the quote API budget is ~1 request/s
        quote = self._quote_model(key, q)
        return self._stamp(quote, quote.ts, self.ttl.quote)

    async def get_candles(
        self, key: InstrumentKey, interval: Interval, start: datetime, end: datetime
    ) -> Sourced[list[Candle]]:
        token = self.instruments.token.get(key.id)
        if token is None:
            raise DataUnavailable(f"{key.id} is not in the Kite instrument list")
        now = self._now()
        params = {
            "from": start.astimezone(cal.IST).strftime("%Y-%m-%d %H:%M:%S"),
            "to": min(end, now).astimezone(cal.IST).strftime("%Y-%m-%d %H:%M:%S"),
            "oi": "1",
        }
        data = await self._get(
            f"/instruments/historical/{token}/{INTERVALS[interval]}", params, limit=self._history_limit
        )
        candles = []
        for r in data["candles"]:  # [timestamp, open, high, low, close, volume, (oi)]
            if not isinstance(r, list) or len(r) < 6:
                raise KiteError(f"unexpected candle row {r!r}")
            ts = _ist(r[0])
            bar_end = (
                cal.session_bounds(ts.astimezone(cal.IST).date())[1]
                if interval is Interval.D1
                else ts + timedelta(minutes=interval.minutes)
            )
            if bar_end > now:  # completed bars only
                continue
            candles.append(
                Candle(
                    key=key,
                    interval=interval,
                    ts=ts,
                    open=r[1],
                    high=r[2],
                    low=r[3],
                    close=r[4],
                    volume=int(r[5]),
                    oi=int(r[6]) if len(r) > 6 and r[6] else None,
                )
            )
        candles.sort(key=lambda c: c.ts)
        if not candles:
            raise DataUnavailable(f"no {interval} candles for {key.id} in [{start}, {end}]")
        last = candles[-1]
        as_of = (
            cal.session_bounds(last.ts.astimezone(cal.IST).date())[1]
            if interval is Interval.D1
            else last.ts + timedelta(minutes=interval.minutes)
        )
        return self._stamp(
            candles, as_of, self.ttl.candles_daily if interval is Interval.D1 else self.ttl.candles_intraday
        )

    async def get_expiries(self, underlying: InstrumentKey) -> list[date]:
        now = self._now()
        exps = [e for e in self.instruments.expiries(underlying.underlying) if now <= cal.expiry_close(e)]
        if not exps:
            raise DataUnavailable(f"no option expiries for {underlying.symbol} in the Kite instrument list")
        return exps

    async def get_option_chain(self, underlying: InstrumentKey, expiry: date) -> Sourced[OptionChain]:
        contracts = self.instruments.options.get((underlying.underlying, expiry))
        if not contracts:
            raise DataUnavailable(f"no {underlying.symbol} options for {expiry} in the Kite instrument list")
        spot = (await self.get_quote(underlying)).value.ltp
        strikes = sorted({k.strike for k in contracts}, key=lambda s: abs(s - spot))
        keep = set(sorted(strikes[: 2 * STRIKES_EACH_SIDE + 1]))
        chosen = [k for k in contracts if k.strike in keep]
        fetched = self._now()
        quotes = await self._quotes([self.instruments.kite_symbol(k) for k in chosen])
        today = fetched.astimezone(cal.IST).date()
        by_strike: dict[float, dict[OptionType, OptionQuote]] = {}
        for k in chosen:
            q = quotes.get(self.instruments.kite_symbol(k))
            if q is None:
                continue
            oi = int(q.oi or 0)
            bid = q.depth.buy[0].price if q.depth and q.depth.buy else 0.0
            ask = q.depth.sell[0].price if q.depth and q.depth.sell else 0.0
            by_strike.setdefault(k.strike, {})[k.option_type] = OptionQuote(
                key=k,
                ltp=q.last_price,
                bid=bid,
                ask=max(ask, bid),
                volume=q.volume or 0,
                oi=oi,
                oi_change=self.oi.change(k, oi, today),
            )
        self.oi.save()
        if not by_strike:
            raise DataUnavailable(f"no option quotes returned for {underlying.symbol} {expiry}")
        rows = [
            OptionChainRow(strike=s, call=v.get(OptionType.CE), put=v.get(OptionType.PE))
            for s, v in sorted(by_strike.items())
        ]
        chain = OptionChain(underlying=underlying, expiry=expiry, ts=fetched, spot=spot, rows=rows)
        return self._stamp(chain, fetched, self.ttl.option_chain)

    async def get_constituents(self, underlying: InstrumentKey) -> Sourced[list[Constituent]]:
        spec = get_index(underlying.symbol)
        keys = [InstrumentKey(exchange="NSE", underlying=c.symbol, is_index=False) for c in spec.constituents]
        known = [k for k in keys if k.id in self.instruments.symbol]
        if len(known) < len(keys):
            log.warning(
                "constituents not in the Kite instrument list (skipped): %s",
                ", ".join(k.underlying for k in keys if k not in known),
            )
        if not known:
            raise DataUnavailable(f"no constituents of {underlying.symbol} in the Kite instrument list")
        quotes = await self._quotes([self.instruments.kite_symbol(k) for k in known])
        out, as_of = [], None
        for c, k in ((c, k) for c, k in zip(spec.constituents, keys, strict=True) if k in known):
            q = quotes.get(self.instruments.kite_symbol(k))
            if q is None:
                continue
            m = self._quote_model(k, q)
            out.append(Constituent(key=k, name=c.name, weight=c.weight, ltp=m.ltp, prev_close=m.prev_close))
            as_of = m.ts if as_of is None else min(as_of, m.ts)
        if not out:
            raise DataUnavailable("no constituent quotes from Kite")
        return self._stamp(out, as_of, self.ttl.constituents)

    async def get_macro(self) -> MacroSnapshot:
        """India VIX only; USD/INR, bond yields and FII/DII flows are not in Kite market data."""
        vix = InstrumentKey(exchange="NSE", underlying="INDIAVIX")
        if vix.id not in self.instruments.symbol:
            return MacroSnapshot()
        q = await self.get_quote(vix)
        return MacroSnapshot(india_vix=self._stamp(q.value.ltp, q.value.ts, self.ttl.india_vix))

    async def stream_ticks(self, keys: Sequence[InstrumentKey]) -> AsyncIterator[Tick]:
        """One combined quote poll per `poll_seconds` (index, futures, VIX); yields a Tick when a price
        changes, and fills the cache get_quote is served from."""
        mapped = [k for k in keys if k.id in self.instruments.symbol]
        if len(mapped) < len(keys):
            log.warning(
                "not streaming (not in the instrument list): %s", ", ".join(k.id for k in keys if k not in mapped)
            )
        if not mapped:
            raise DataUnavailable("none of the requested instruments are in the Kite instrument list")
        extra = [VIX_SYMBOL] if VIX_SYMBOL in self.instruments.key_by_symbol else []
        syms = {self.instruments.kite_symbol(k): k for k in mapped}
        last: dict[str, float] = {}
        failures = 0
        while True:
            started = time.perf_counter()
            try:
                data = await self._quotes([*syms, *extra])
                failures = 0
            except KiteAuthError:
                raise
            except KiteError as ex:
                failures += 1
                log.warning("tick poll failed (%d in a row): %s", failures, ex)
                await asyncio.sleep(min(30, 2**failures))
                continue
            for sym, key in syms.items():
                q = data.get(sym)
                if q is not None and q.last_price > 0 and last.get(sym) != q.last_price:
                    last[sym] = q.last_price
                    yield Tick(
                        key=key, ts=self._now(), ltp=q.last_price, volume=q.volume or 0, oi=int(q.oi) if q.oi else None
                    )
            await asyncio.sleep(max(0.0, self.poll_seconds - (time.perf_counter() - started)))

    async def check(self) -> dict:
        profile = await self._get("/user/profile", limit=self._quote_limit)
        idx = InstrumentKey(exchange="NSE", underlying="BANKNIFTY")
        q = await self.get_quote(idx)
        return {
            "user": profile.get("user_name") or profile.get("user_id"),
            "banknifty": q.value.ltp,
            "as_of": q.as_of.isoformat(),
            "expiries": [str(e) for e in (await self.get_expiries(idx))[:3]],
        }

    async def close(self) -> None:
        self.oi.save()
        await self.client.aclose()


# ------------------------------------------------------------------ daily login
def login_url(api_key: str) -> str:
    return f"{LOGIN}?" + urlencode({"v": "3", "api_key": api_key})


def parse_request_token(text: str) -> str:
    """Accept the bare token or the whole redirect URL (even from a browser error page)."""
    text = text.strip()
    if "request_token=" in text:
        qs = parse_qs(urlparse(text if "://" in text else "http://x/?" + text.split("?", 1)[-1]).query)
        if qs.get("status", ["success"])[0] != "success":
            raise KiteAuthError(f"login did not succeed (status={qs.get('status')})")
        return qs["request_token"][0]
    if not re.fullmatch(r"[A-Za-z0-9]{8,64}", text):
        raise KiteAuthError("that does not look like a request_token or a Kite redirect URL")
    return text


async def exchange_request_token(
    request_token: str, api_key: str, api_secret: str, client: httpx.AsyncClient | None = None
) -> dict:
    """POST /session/token with checksum = sha256(api_key + request_token + api_secret)."""
    checksum = hashlib.sha256((api_key + request_token + api_secret).encode()).hexdigest()
    own = client is None
    client = client or httpx.AsyncClient(timeout=20, verify=ssl_verify())
    try:
        r = await client.post(
            f"{BASE}/session/token",
            data={"api_key": api_key, "request_token": request_token, "checksum": checksum},
            headers={"X-Kite-Version": "3"},
        )
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        data = body.get("data") or {}
        if r.status_code != 200 or not data.get("access_token"):
            raise KiteAuthError(f"token exchange failed: HTTP {r.status_code} {body.get('message', '')}".strip())
        return {"access_token": data["access_token"], "user": data.get("user_name") or data.get("user_id")}
    finally:
        if own:
            await client.aclose()


# ------------------------------------------------------------------ instruments
async def download_instruments(
    cache_dir: Path, api_key: str, access_token: str, max_age_h: float = 12, client: httpx.AsyncClient | None = None
) -> KiteInstruments:
    """NSE + NFO instrument dumps, cached for `max_age_h` (contracts change daily)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    own = client is None
    client = client or httpx.AsyncClient(timeout=60, verify=ssl_verify())
    texts = {}
    try:
        for ex in ("NSE", "NFO"):
            path = cache_dir / f"{ex}.csv"
            if path.exists() and time.time() - path.stat().st_mtime < max_age_h * 3600:
                texts[ex] = path.read_text("utf-8")
                continue
            try:
                r = await client.get(
                    f"{BASE}/instruments/{ex}",
                    headers={"X-Kite-Version": "3", "Authorization": f"token {api_key}:{access_token}"},
                )
                r.raise_for_status()
            except httpx.HTTPError as ex_:
                hint = (
                    " (TLS intercepted - set MP_CA_BUNDLE, or use an open network)"
                    if "CERTIFICATE" in str(ex_).upper()
                    else ""
                )
                raise KiteError(
                    f"could not download the Kite {ex} instrument list: {type(ex_).__name__}{hint}"
                ) from None
            if not r.text.startswith("instrument_token"):
                raise KiteError(f"Kite {ex} instrument list is not CSV (got {r.text[:60]!r}) - blocked by a proxy?")
            path.write_text(r.text, encoding="utf-8")
            texts[ex] = r.text
    finally:
        if own:
            await client.aclose()
    return KiteInstruments(texts["NSE"], texts["NFO"])


async def provider_from_settings(clock: Clock | None = None) -> KiteProvider:
    s = get_settings()
    if not (s.kite_api_key and s.kite_access_token):
        raise KiteAuthError("set KITE_API_KEY and KITE_ACCESS_TOKEN in .env (run `mp kite-login` for the token)")
    cache = Path(s.kite_cache_dir)
    instruments = await download_instruments(cache, s.kite_api_key, s.kite_access_token)
    if not get_index(s.default_symbol).verified:
        log.warning("lot size / constituent weights in indices.toml are unverified - update them from NSE")
    return KiteProvider(
        s.kite_api_key,
        s.kite_access_token,
        instruments,
        clock=clock,
        poll_seconds=s.kite_poll_seconds,
        oi_baseline=OIBaseline(cache / "oi_baseline.json"),
    )


__all__ = [
    "KiteProvider",
    "KiteInstruments",
    "KiteError",
    "KiteAuthError",
    "login_url",
    "parse_request_token",
    "exchange_request_token",
    "download_instruments",
    "provider_from_settings",
]
