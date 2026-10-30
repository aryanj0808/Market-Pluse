"""Upstox market-data adapter (read-only: quotes, candles, option chain - never orders).

Endpoints (Upstox API docs; v2 base https://api.upstox.com/v2, v3 for candles):
    GET /v2/market-quote/quotes?instrument_key=K1,K2       full quotes (<= 500 keys)
    GET /v2/market-quote/ltp?instrument_key=K1,K2          last traded prices (tick polling)
    GET /v2/option/chain?instrument_key=K&expiry_date=D    put/call chain for one expiry
    GET /v3/historical-candle/{K}/{unit}/{n}/{to}/{from}   completed days
    GET /v3/historical-candle/intraday/{K}/{unit}/{n}      today
    instruments: https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz

Broker tokens ("NSE_FO|36321", "NSE_INDEX|Nifty Bank") never leave this module: everything outside
sees InstrumentKey. Responses are validated by pydantic models; an unexpected shape raises with the
missing field named. STATUS: written against the documented API and tested with mocked responses
only - the development network blocks api.upstox.com. Verify with `mp upstox-check` before relying
on it.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import time
from collections.abc import AsyncIterator, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from mp_core import market_calendar as cal
from mp_core.clock import Clock, SystemClock
from mp_core.models import (
    Candle,
    Constituent,
    InstrumentKey,
    InstrumentKind,
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
from mp_core.providers.base import DataUnavailable, MarketDataProvider
from mp_core.registry import get_index
from mp_core.settings import TTLs, get_settings

log = logging.getLogger(__name__)

BASE = "https://api.upstox.com"
INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
INDEX_KEYS = {
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "NIFTY": "NSE_INDEX|Nifty 50",
    "FINNIFTY": "NSE_INDEX|Nifty Fin Service",
}
VIX_KEY = "NSE_INDEX|India VIX"
CANDLE_UNITS = {
    Interval.M1: ("minutes", 1),
    Interval.M5: ("minutes", 5),
    Interval.M15: ("minutes", 15),
    Interval.H1: ("hours", 1),
    Interval.D1: ("days", 1),
}


class UpstoxError(RuntimeError):
    pass


class UpstoxAuthError(UpstoxError):
    """401: the access token is missing, wrong or expired (they expire daily)."""


class _Loose(BaseModel):
    model_config = ConfigDict(extra="ignore")  # brokers add fields; the ones we need are required


class _Instrument(_Loose):
    segment: str
    instrument_key: str
    trading_symbol: str
    instrument_type: str
    underlying_symbol: str | None = None
    expiry: int | None = None  # epoch ms
    strike_price: float | None = None
    lot_size: int | None = None


class _Ohlc(_Loose):
    open: float
    high: float
    low: float
    close: float


class _FullQuote(_Loose):
    instrument_token: str
    last_price: float
    net_change: float
    ohlc: _Ohlc
    volume: int | None = 0
    oi: float | None = None
    timestamp: str | None = None
    last_trade_time: str | None = None  # epoch ms as a string


class _Ltp(_Loose):
    instrument_token: str
    last_price: float


class _OptMarket(_Loose):
    ltp: float
    volume: int
    oi: float
    prev_oi: float
    bid_price: float
    ask_price: float


class _OptSide(_Loose):
    instrument_key: str
    market_data: _OptMarket


class _ChainRow(_Loose):
    expiry: str
    strike_price: float
    underlying_spot_price: float
    call_options: _OptSide | None = None
    put_options: _OptSide | None = None


class InstrumentMaster:
    """Bidirectional map InstrumentKey <-> Upstox instrument_key, built from the instrument file."""

    def __init__(self, rows: Sequence[dict]) -> None:
        self.to_broker: dict[str, str] = {}
        self.from_broker: dict[str, InstrumentKey] = {}
        self.lot_size: dict[str, int] = {}
        self.expiries: dict[str, set[date]] = {}
        bad = 0
        for raw in rows:
            try:
                ins = _Instrument.model_validate(raw)
            except ValidationError:
                bad += 1
                continue
            key = self._key(ins)
            if key is not None:
                self.to_broker[key.id] = ins.instrument_key
                self.from_broker[ins.instrument_key] = key
                if ins.lot_size:
                    self.lot_size[key.id] = ins.lot_size
                if key.kind is InstrumentKind.OPTION:
                    self.expiries.setdefault(key.underlying, set()).add(key.expiry)
        for name, ukey in INDEX_KEYS.items():
            k = InstrumentKey(exchange="NSE", underlying=name)
            self.to_broker[k.id], self.from_broker[ukey] = ukey, k
        if bad:
            log.info("instrument master: skipped %d rows we do not use", bad)

    @staticmethod
    def _key(ins: _Instrument) -> InstrumentKey | None:
        if ins.segment == "NSE_EQ" and ins.instrument_type == "EQ":
            return InstrumentKey(exchange="NSE", underlying=ins.trading_symbol.upper(), is_index=False)
        if ins.segment != "NSE_FO" or not ins.underlying_symbol or ins.expiry is None:
            return None
        und = ins.underlying_symbol.upper()
        if und not in INDEX_KEYS:
            return None  # stock derivatives: not needed by this project
        exp = datetime.fromtimestamp(ins.expiry / 1000, tz=cal.IST).date()
        if ins.instrument_type == "FUT":
            return InstrumentKey(exchange="NSE", underlying=und, expiry=exp)
        if ins.instrument_type in ("CE", "PE") and ins.strike_price:
            return InstrumentKey(
                exchange="NSE", underlying=und, expiry=exp, strike=ins.strike_price, option_type=ins.instrument_type
            )
        return None

    @classmethod
    def load(cls, path: Path) -> InstrumentMaster:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return cls(json.load(f))

    def broker(self, key: InstrumentKey) -> str:
        try:
            return self.to_broker[key.id]
        except KeyError:
            raise DataUnavailable(f"{key.id} is not in the Upstox instrument list (expired or not loaded?)") from None


async def download_instruments(dest: Path, client: httpx.AsyncClient | None = None, max_age_h: float = 12) -> Path:
    """Cache the public instrument file; refreshed when older than `max_age_h` (contracts change daily)."""
    if dest.exists() and time.time() - dest.stat().st_mtime < max_age_h * 3600:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    own = client is None
    client = client or httpx.AsyncClient(timeout=60, follow_redirects=True)
    try:
        try:
            r = await client.get(INSTRUMENTS_URL)
            r.raise_for_status()
        except httpx.HTTPError as ex:
            hint = " (TLS intercepted - this network blocks Upstox)" if "CERTIFICATE" in str(ex).upper() else ""
            raise UpstoxError(f"could not download the Upstox instrument list: {type(ex).__name__}{hint}") from None
        if not r.content.startswith(b"\x1f\x8b"):
            raise UpstoxError(f"instrument file is not gzip (got {r.content[:60]!r}) - blocked by a proxy?")
        dest.write_bytes(r.content)
        return dest
    finally:
        if own:
            await client.aclose()


class UpstoxProvider(MarketDataProvider):
    source = Source.UPSTOX

    def __init__(
        self,
        token: str,
        instruments: InstrumentMaster,
        *,
        clock: Clock | None = None,
        client: httpx.AsyncClient | None = None,
        poll_seconds: float = 1.0,
        ttls: TTLs | None = None,
    ) -> None:
        if not token:
            raise UpstoxAuthError("UPSTOX_ACCESS_TOKEN is not set - run `mp upstox-login`")
        self.clock = clock or SystemClock()
        self.instruments = instruments
        self.poll_seconds = poll_seconds
        self.ttl = ttls or get_settings().ttl
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0))
        self._headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    # ------------------------------------------------------------------ http
    async def _get(self, path: str, params: dict | None = None) -> dict | list:
        for attempt in range(3):
            try:
                r = await self.client.get(BASE + path, params=params, headers=self._headers)
            except httpx.HTTPError as ex:
                if attempt == 2:
                    raise UpstoxError(f"{path}: {type(ex).__name__}") from None
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            if r.status_code == 401:
                raise UpstoxAuthError("Upstox rejected the access token (expired? they last one day)")
            if r.status_code == 429 or r.status_code >= 500:
                if attempt == 2:
                    raise UpstoxError(f"{path}: HTTP {r.status_code}")
                await asyncio.sleep(1.0 * (attempt + 1))
                continue
            try:
                body = r.json()
            except ValueError:
                raise UpstoxError(f"{path}: HTTP {r.status_code}, not JSON (proxy page?)") from None
            if r.status_code != 200 or body.get("status") != "success":
                raise UpstoxError(f"{path}: HTTP {r.status_code} {json.dumps(body.get('errors', body))[:300]}")
            return body["data"]
        raise UpstoxError(f"{path}: retries exhausted")

    def _now(self) -> datetime:
        return self.clock.now()

    def _stamp(self, value, as_of: datetime, ttl: float):
        return stamp(value, source=self.source, as_of=as_of, now=self._now(), ttl_seconds=ttl)

    @staticmethod
    def _ts(q: _FullQuote, fallback: datetime) -> datetime:
        if q.last_trade_time and q.last_trade_time.isdigit():
            return datetime.fromtimestamp(int(q.last_trade_time) / 1000, tz=cal.IST)
        if q.timestamp:
            t = datetime.fromisoformat(q.timestamp)
            return t if t.tzinfo else t.replace(tzinfo=cal.IST)
        return fallback

    async def _full_quotes(self, keys: Sequence[InstrumentKey]) -> dict[str, _FullQuote]:
        broker = {self.instruments.broker(k): k for k in keys}
        data = await self._get("/v2/market-quote/quotes", {"instrument_key": ",".join(broker)})
        out = {}
        for entry in data.values():  # map keys are "NSE_EQ:SYMBOL"; instrument_token is the stable id
            q = _FullQuote.model_validate(entry)
            if q.instrument_token in broker:
                out[broker[q.instrument_token].id] = q
        return out

    def _quote(self, key: InstrumentKey, q: _FullQuote) -> Quote:
        ts = self._ts(q, self._now())
        prev_close = q.last_price - q.net_change  # robust to whatever ohlc.close means intraday
        return Quote(
            key=key,
            ts=ts,
            ltp=q.last_price,
            open=q.ohlc.open or q.last_price,
            high=max(q.ohlc.high, q.last_price),
            low=min(q.ohlc.low or q.last_price, q.last_price),
            prev_close=prev_close,
            volume=q.volume or 0,
            oi=int(q.oi) if q.oi else None,
        )

    # ------------------------------------------------------------------ interface
    async def get_quote(self, key: InstrumentKey) -> Sourced[Quote]:
        q = (await self._full_quotes([key])).get(key.id)
        if q is None:
            raise DataUnavailable(f"no Upstox quote for {key.id}")
        quote = self._quote(key, q)
        return self._stamp(quote, quote.ts, self.ttl.quote)

    async def get_candles(
        self, key: InstrumentKey, interval: Interval, start: datetime, end: datetime
    ) -> Sourced[list[Candle]]:
        unit, n = CANDLE_UNITS[interval]
        k = quote(self.instruments.broker(key), safe="")
        now = self._now()
        today = now.astimezone(cal.IST).date()
        rows: list = []
        d0, d1 = start.astimezone(cal.IST).date(), min(end.astimezone(cal.IST).date(), today)
        if d0 < today:
            hist_to = min(d1, today - timedelta(days=1))
            rows += (await self._get(f"/v3/historical-candle/{k}/{unit}/{n}/{hist_to}/{d0}"))["candles"]
        if d1 >= today and interval is not Interval.D1:
            rows += (await self._get(f"/v3/historical-candle/intraday/{k}/{unit}/{n}"))["candles"]
        candles = []
        for r in rows:  # [timestamp, open, high, low, close, volume, oi]
            if not isinstance(r, list) or len(r) < 6:
                raise UpstoxError(f"unexpected candle row {r!r}")
            ts = datetime.fromisoformat(r[0])
            bar_end = (
                ts + timedelta(minutes=interval.minutes)
                if interval is not Interval.D1
                else cal.session_bounds(ts.astimezone(cal.IST).date())[1]
            )
            if not (start <= ts <= end) or bar_end > now:  # completed bars only
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
        candles.sort(key=lambda c: c.ts)  # Upstox returns newest first
        if not candles:
            raise DataUnavailable(f"no {interval} candles for {key.id} in [{start}, {end}]")
        last = candles[-1]
        as_of = (
            last.ts + timedelta(minutes=interval.minutes)
            if interval is not Interval.D1
            else cal.session_bounds(last.ts.astimezone(cal.IST).date())[1]
        )
        ttl = self.ttl.candles_daily if interval is Interval.D1 else self.ttl.candles_intraday
        return self._stamp(candles, as_of, ttl)

    async def get_expiries(self, underlying: InstrumentKey) -> list[date]:
        today = self._now().astimezone(cal.IST).date()
        exps = sorted(e for e in self.instruments.expiries.get(underlying.underlying, set()) if e >= today)
        if not exps:
            raise DataUnavailable(f"no option expiries for {underlying.symbol} in the instrument list")
        return [e for e in exps if self._now() <= cal.expiry_close(e)]

    async def get_option_chain(self, underlying: InstrumentKey, expiry: date) -> Sourced[OptionChain]:
        fetched = self._now()
        data = await self._get(
            "/v2/option/chain",
            {"instrument_key": self.instruments.broker(underlying), "expiry_date": expiry.isoformat()},
        )
        rows = sorted((_ChainRow.model_validate(r) for r in data), key=lambda r: r.strike_price)
        if not rows:
            raise DataUnavailable(f"empty Upstox chain for {underlying.symbol} {expiry}")
        out = []
        for r in rows:
            quotes = {}
            for ot, side in ((OptionType.CE, r.call_options), (OptionType.PE, r.put_options)):
                if side is None:
                    continue
                m = side.market_data
                key = InstrumentKey(
                    exchange="NSE",
                    underlying=underlying.underlying,
                    expiry=expiry,
                    strike=r.strike_price,
                    option_type=ot,
                )
                quotes[ot] = OptionQuote(
                    key=key,
                    ltp=m.ltp,
                    bid=m.bid_price,
                    ask=max(m.ask_price, m.bid_price),
                    volume=m.volume,
                    oi=int(m.oi),
                    oi_change=int(m.oi - m.prev_oi),
                )
            out.append(
                OptionChainRow(strike=r.strike_price, call=quotes.get(OptionType.CE), put=quotes.get(OptionType.PE))
            )
        # the chain carries no timestamp: it is as fresh as the request
        chain = OptionChain(
            underlying=underlying, expiry=expiry, ts=fetched, spot=rows[0].underlying_spot_price, rows=out
        )
        return self._stamp(chain, fetched, self.ttl.option_chain)

    async def get_constituents(self, underlying: InstrumentKey) -> Sourced[list[Constituent]]:
        spec = get_index(underlying.symbol)
        if not spec.constituents:
            raise DataUnavailable(f"no constituent list configured for {underlying.symbol}")
        keys = [InstrumentKey(exchange="NSE", underlying=c.symbol, is_index=False) for c in spec.constituents]
        missing = [k.underlying for k in keys if k.id not in self.instruments.to_broker]
        if missing:  # a renamed/delisted constituent must not blank the whole panel
            log.warning("constituents not in the Upstox instrument list (skipped): %s", ", ".join(missing))
        quotes = await self._full_quotes([k for k in keys if k.id in self.instruments.to_broker])
        out, as_of = [], None
        for c, k in zip(spec.constituents, keys, strict=True):
            q = quotes.get(k.id)
            if q is None:
                continue
            qq = self._quote(k, q)
            out.append(Constituent(key=k, name=c.name, weight=c.weight, ltp=qq.ltp, prev_close=qq.prev_close))
            as_of = qq.ts if as_of is None else min(as_of, qq.ts)
        if not out:
            raise DataUnavailable("no constituent quotes from Upstox")
        return self._stamp(out, as_of, self.ttl.constituents)

    async def get_macro(self) -> MacroSnapshot:
        """India VIX only: USD/INR, bond yields and FII/DII flows are not in Upstox market data and are
        reported as missing rather than guessed."""
        vix_key = InstrumentKey(exchange="NSE", underlying="INDIAVIX")
        self.instruments.to_broker.setdefault(vix_key.id, VIX_KEY)
        q = (await self._full_quotes([vix_key])).get(vix_key.id)
        if q is None:
            return MacroSnapshot()
        ts = self._ts(q, self._now())
        return MacroSnapshot(india_vix=self._stamp(q.last_price, ts, self.ttl.india_vix))

    async def stream_ticks(self, keys: Sequence[InstrumentKey]) -> AsyncIterator[Tick]:
        """Poll last traded prices once per `poll_seconds` (one batched call) and yield a Tick when a
        price changes. (Upstox's push feed is protobuf over WebSocket - a later upgrade.)"""
        mapped = [k for k in keys if k.id in self.instruments.to_broker]
        if len(mapped) < len(keys):  # e.g. next month's future before it is listed: stream the rest
            log.warning(
                "not streaming (not in the instrument list): %s", ", ".join(k.id for k in keys if k not in mapped)
            )
        if not mapped:
            raise DataUnavailable("none of the requested instruments are in the Upstox instrument list")
        broker = {self.instruments.broker(k): k for k in mapped}
        last: dict[str, float] = {}
        failures = 0
        while True:
            started = time.monotonic()
            try:
                data = await self._get("/v2/market-quote/ltp", {"instrument_key": ",".join(broker)})
                failures = 0
            except UpstoxAuthError:
                raise
            except UpstoxError as ex:
                failures += 1
                log.warning("tick poll failed (%d in a row): %s", failures, ex)
                await asyncio.sleep(min(30, 2**failures))
                continue
            now = self._now()
            for entry in data.values():
                t = _Ltp.model_validate(entry)
                key = broker.get(t.instrument_token)
                if key is not None and last.get(key.id) != t.last_price and t.last_price > 0:
                    last[key.id] = t.last_price
                    yield Tick(key=key, ts=now, ltp=t.last_price)
            await asyncio.sleep(max(0.0, self.poll_seconds - (time.monotonic() - started)))

    async def check(self) -> dict:
        """Cheap end-to-end check for `mp upstox-check`: token, one index quote, expiries."""
        profile = await self._get("/v2/user/profile")
        idx = InstrumentKey(exchange="NSE", underlying="BANKNIFTY")
        q = await self.get_quote(idx)
        return {
            "user": profile.get("user_name") or profile.get("user_id"),
            "banknifty": q.value.ltp,
            "as_of": q.as_of.isoformat(),
            "expiries": [str(e) for e in (await self.get_expiries(idx))[:3]],
        }

    async def close(self) -> None:
        await self.client.aclose()


# ------------------------------------------------------------------ daily login (OAuth 2)
def authorize_url(api_key: str, redirect_uri: str) -> str:
    """Open this in a browser, log in, and copy the `code` from the redirect."""
    from urllib.parse import urlencode

    return f"{BASE}/v2/login/authorization/dialog?" + urlencode(
        {"response_type": "code", "client_id": api_key, "redirect_uri": redirect_uri}
    )


async def exchange_code(
    code: str, api_key: str, api_secret: str, redirect_uri: str, client: httpx.AsyncClient | None = None
) -> str:
    """Trade the one-time code for today's access token."""
    own = client is None
    client = client or httpx.AsyncClient(timeout=20)
    try:
        r = await client.post(
            f"{BASE}/v2/login/authorization/token",
            data={
                "code": code,
                "client_id": api_key,
                "client_secret": api_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            headers={"Accept": "application/json"},
        )
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        token = body.get("access_token")
        if r.status_code != 200 or not token:
            raise UpstoxAuthError(
                f"token exchange failed: HTTP {r.status_code} {json.dumps(body.get('errors', ''))[:200]}"
            )
        return token
    finally:
        if own:
            await client.aclose()


async def provider_from_settings(clock: Clock | None = None) -> UpstoxProvider:
    s = get_settings()
    path = await download_instruments(Path(s.upstox_instruments_cache))
    master = InstrumentMaster.load(path)
    spec = get_index(s.default_symbol)
    if not spec.verified:
        log.warning(
            "%s: lot size / constituent weights in indices.toml are unverified - update them from NSE", spec.underlying
        )
    return UpstoxProvider(s.upstox_access_token or "", master, clock=clock, poll_seconds=s.upstox_poll_seconds)
