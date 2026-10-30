"""Zerodha Kite adapter against a mocked API (documented response shapes). It has not been run against
the real API - see `mp kite-check`."""

import asyncio
import hashlib
import time
from datetime import date, datetime
from urllib.parse import parse_qs

import httpx
import pytest

from mp_core import market_calendar as cal
from mp_core.analytics.black_scholes import price as bs_price
from mp_core.clock import SimClock
from mp_core.models import InstrumentKey, Interval, OptionType, Source
from mp_core.providers.base import DataUnavailable
from mp_core.providers.kite import (
    KiteAuthError,
    KiteError,
    KiteInstruments,
    KiteProvider,
    OIBaseline,
    _RateLimit,
    download_instruments,
    exchange_request_token,
    login_url,
    parse_request_token,
)
from mp_core.registry import get_index

NOW = datetime(2026, 10, 20, 11, 0, tzinfo=cal.IST)
EXP = date(2026, 10, 27)
SPOT = 56000.0
BN = InstrumentKey.parse("NSE:BANKNIFTY")
FUT = InstrumentKey(exchange="NSE", underlying="BANKNIFTY", expiry=EXP)
CE = InstrumentKey(exchange="NSE", underlying="BANKNIFTY", expiry=EXP, strike=56000, option_type=OptionType.CE)
STRIKES = [SPOT + 100 * i for i in range(-30, 31)]
HEADER = "instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,lot_size,instrument_type,segment,exchange"


def _nse_csv() -> str:
    rows = [
        "260105,1016,NIFTY BANK,NIFTY BANK,0,,0,0,0,EQ,INDICES,NSE",
        "264969,1035,INDIA VIX,INDIA VIX,0,,0,0,0,EQ,INDICES,NSE",
        "256265,1001,NIFTY 50,NIFTY 50,0,,0,0,0,EQ,INDICES,NSE",
    ]
    for i, c in enumerate(get_index("NSE:BANKNIFTY").constituents):
        rows.append(f"{5000 + i},{i},{c.symbol},{c.name},0,,0,0.05,1,EQ,NSE,NSE")
    return "\n".join([HEADER, *rows]) + "\n"


def _nfo_csv() -> str:
    rows = [
        "9001,1,BANKNIFTY26OCTFUT,BANKNIFTY,0,2026-10-27,0,0.05,30,FUT,NFO-FUT,NFO",
        "9002,2,RELIANCE26OCTFUT,RELIANCE,0,2026-10-27,0,0.05,500,FUT,NFO-FUT,NFO",
    ]
    tok = 10000
    for s in STRIKES:
        for t in ("CE", "PE"):
            tok += 1
            rows.append(f"{tok},{tok},BANKNIFTY26OCT{int(s)}{t},BANKNIFTY,0,2026-10-27,{s},0.05,30,{t},NFO-OPT,NFO")
    return "\n".join([HEADER, *rows]) + "\n"


def _option_price(sym: str) -> tuple[float, int]:
    strike, t = float(sym[-7:-2]), OptionType(sym[-2:])
    t_years = (cal.expiry_close(EXP) - NOW).total_seconds() / (365 * 86400)
    p = bs_price(SPOT, strike, t_years, 0.065, 0.14 + abs(strike - SPOT) / 1e5, t)
    return max(round(p, 2), 0.05), 100_000 + int(abs(strike - SPOT))


class FakeKite:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.spot = SPOT
        self.status: int | None = None

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        self.calls.append(path)
        if self.status is not None:
            return httpx.Response(self.status, json={"status": "error", "message": "nope", "error_type": "X"})
        assert req.headers["X-Kite-Version"] == "3"
        if path == "/quote":
            assert req.headers["Authorization"] == "token KEY:TOK"
            syms = [v for k, v in req.url.params.multi_items() if k == "i"]
            assert len(syms) <= 500
            return httpx.Response(200, json={"status": "success", "data": {s: self._quote(s) for s in syms}})
        if path.startswith("/instruments/historical/"):
            daily = path.endswith("/day")
            candles = (
                [
                    ["2026-10-16T00:00:00+0530", 55500, 55900, 55400, 55850, 0],
                    ["2026-10-19T00:00:00+0530", 55800, 56100, 55700, 55910, 0],
                ]
                if daily
                else [
                    ["2026-10-20T10:58:00+0530", 56000, 56008, 55990, 56005, 10, 0],
                    ["2026-10-20T10:59:00+0530", 56005, 56012, 56001, 56010, 12, 0],
                    ["2026-10-20T11:00:00+0530", 56010, 56020, 56000, 56015, 3, 0],
                ]  # forming
            )
            return httpx.Response(200, json={"status": "success", "data": {"candles": candles}})
        if path == "/user/profile":
            return httpx.Response(200, json={"status": "success", "data": {"user_name": "Test User"}})
        return httpx.Response(404, json={"status": "error", "message": "no route"})

    def _quote(self, sym: str) -> dict:
        ohlc = {"open": 55950, "high": 56100, "low": 55900, "close": 55910}
        base = {"instrument_token": 1, "timestamp": "2026-10-20 11:00:00", "ohlc": ohlc}
        if sym == "NSE:NIFTY BANK":
            return {**base, "last_price": self.spot}
        if sym == "NSE:INDIA VIX":
            return {**base, "last_price": 14.2, "ohlc": {"open": 14, "high": 14.5, "low": 13.9, "close": 14.1}}
        if sym == "NFO:BANKNIFTY26OCTFUT":
            return {**base, "last_price": self.spot + 120, "volume": 1000, "oi": 2_000_000}
        if sym.startswith("NFO:BANKNIFTY26OCT"):
            p, oi = _option_price(sym)
            depth = {
                "buy": [{"price": round(p - 0.5, 2), "quantity": 30}],
                "sell": [{"price": p + 0.5, "quantity": 30}],
            }
            return {
                **base,
                "last_price": p,
                "volume": 500,
                "oi": oi,
                "depth": depth,
                "ohlc": {"open": p, "high": p, "low": p, "close": p},
            }
        return {
            **base,
            "last_price": 1600.0,
            "volume": 10,
            "ohlc": {"open": 1590, "high": 1610, "low": 1580, "close": 1595},
        }


@pytest.fixture
def instruments():
    return KiteInstruments(_nse_csv(), _nfo_csv())


@pytest.fixture
def fake():
    return FakeKite()


@pytest.fixture
def provider(fake, instruments, tmp_path):
    return KiteProvider(
        "KEY",
        "TOK",
        instruments,
        clock=SimClock(NOW),
        poll_seconds=0.01,
        quote_rate=1000,
        history_rate=1000,
        client=httpx.AsyncClient(transport=httpx.MockTransport(fake)),
        oi_baseline=OIBaseline(tmp_path / "oi.json"),
    )


class TestInstruments:
    def test_maps_index_future_option_and_stocks(self, instruments):
        assert instruments.kite_symbol(BN) == "NSE:NIFTY BANK"
        assert instruments.kite_symbol(FUT) == "NFO:BANKNIFTY26OCTFUT"
        assert instruments.kite_symbol(CE) == "NFO:BANKNIFTY26OCT56000CE"
        assert instruments.key_by_symbol["NSE:HDFCBANK"] == InstrumentKey.parse("NSE:HDFCBANK", is_index=False)
        assert instruments.lot_size[FUT.id] == 30 and instruments.expiries("BANKNIFTY") == [EXP]
        assert "NFO:RELIANCE26OCTFUT" not in instruments.key_by_symbol  # stock derivatives ignored
        with pytest.raises(DataUnavailable, match="not in the Kite instrument list"):
            instruments.kite_symbol(InstrumentKey.parse("NSE:BANKNIFTY:2026-11-24"))

    def test_truncated_dump_fails_loudly(self):
        with pytest.raises(KiteError, match="no BANKNIFTY"):
            KiteInstruments(HEADER + "\n", _nfo_csv())


class TestProvider:
    async def test_quote_and_cache(self, provider, fake):
        q = await provider.get_quote(BN)
        assert q.source is Source.KITE and q.value.ltp == SPOT and q.value.prev_close == 55910
        assert q.as_of == datetime(2026, 10, 20, 11, 0, tzinfo=cal.IST) and not q.stale
        n = len(fake.calls)
        await provider.get_quote(BN)  # served from the poll cache: the quote budget is ~1 request/s
        assert len(fake.calls) == n

    async def test_option_chain_window_and_depth(self, provider):
        chain = (await provider.get_option_chain(BN, EXP)).value
        assert len(chain.rows) == 51 and chain.spot == SPOT
        assert min(r.strike for r in chain.rows) == SPOT - 2500 and max(r.strike for r in chain.rows) == SPOT + 2500
        atm = next(r for r in chain.rows if r.strike == SPOT)
        assert atm.call.bid == round(atm.call.ltp - 0.5, 2) and atm.call.ask == atm.call.ltp + 0.5
        assert atm.call.oi_change == 0  # first sight: session-relative

    async def test_candles_drop_forming_bar(self, provider):
        c = (await provider.get_candles(BN, Interval.M1, NOW.replace(hour=9, minute=15), NOW)).value
        assert [x.ts.astimezone(cal.IST).strftime("%H:%M") for x in c] == ["10:58", "10:59"]
        d = (await provider.get_candles(BN, Interval.D1, datetime(2026, 10, 1, tzinfo=cal.IST), NOW)).value
        assert [x.close for x in d] == [55850, 55910]

    async def test_expiries_macro_constituents(self, provider):
        assert await provider.get_expiries(BN) == [EXP]
        assert (await provider.get_macro()).india_vix.value == 14.2
        cons = (await provider.get_constituents(BN)).value
        assert len(cons) == len(get_index("NSE:BANKNIFTY").constituents) and cons[0].ltp == 1600.0

    async def test_tick_polling_yields_changes_only(self, provider, fake):
        ticks = []
        async for t in provider.stream_ticks([BN, FUT]):
            ticks.append(t)
            if len(ticks) == 2:
                fake.spot += 5
            if len(ticks) == 4:
                break
        assert [t.ltp for t in ticks] == [SPOT, SPOT + 120, SPOT + 5, SPOT + 125]

    async def test_403_fails_loudly(self, provider, fake):
        fake.status = 403
        with pytest.raises(KiteAuthError, match="access token"):
            await provider.get_quote(BN)

    async def test_5xx_retried_then_error(self, provider, fake, monkeypatch):
        fake.status = 503
        monkeypatch.setattr("mp_core.providers.kite.asyncio.sleep", _no_sleep)
        with pytest.raises(KiteError, match="HTTP 503"):
            await provider.get_quote(BN)
        assert len(fake.calls) == 3

    async def test_check_and_no_order_methods(self, provider):
        got = await provider.check()
        assert got["user"] == "Test User" and got["banknifty"] == SPOT
        assert not any(hasattr(provider, m) for m in ("place_order", "modify_order", "cancel_order"))
        with pytest.raises(KiteAuthError):
            KiteProvider("KEY", "", provider.instruments)


async def _no_sleep(_s: float) -> None:
    return None


class TestOIBaseline:
    def test_previous_session_last_oi_is_baseline(self, tmp_path):
        path = tmp_path / "oi.json"
        b = OIBaseline(path)
        assert b.change(CE, 1000, date(2026, 10, 19)) == 0
        assert b.change(CE, 1500, date(2026, 10, 19)) == 500  # session-relative on day one
        b.save()
        b2 = OIBaseline(path)
        assert b2.change(CE, 1700, date(2026, 10, 20)) == 200  # vs yesterday's last (1500)
        assert b2.change(CE, 1400, date(2026, 10, 20)) == -100


async def test_rate_limit_spaces_calls():
    rl = _RateLimit(50)
    t0 = time.perf_counter()
    for _ in range(4):
        await rl.wait()
    assert time.perf_counter() - t0 >= 3 / 50 - 0.005


class TestLogin:
    def test_login_url(self):
        assert login_url("abc") == "https://kite.zerodha.com/connect/login?v=3&api_key=abc"

    def test_parse_request_token(self):
        url = "http://127.0.0.1:8000/?action=login&type=login&status=success&request_token=Oq3zuRqrHowzDq30tN0H"
        assert parse_request_token(url) == "Oq3zuRqrHowzDq30tN0H"
        assert parse_request_token("  Oq3zuRqrHowzDq30tN0H ") == "Oq3zuRqrHowzDq30tN0H"
        with pytest.raises(KiteAuthError, match="status"):
            parse_request_token("http://127.0.0.1:8000/?status=cancelled&request_token=abcdefgh")
        with pytest.raises(KiteAuthError):
            parse_request_token("not a token!")

    async def test_exchange_checksum(self):
        def h(req: httpx.Request) -> httpx.Response:
            form = parse_qs(req.content.decode())
            assert form["checksum"][0] == hashlib.sha256(b"KEYreqSECRET").hexdigest()
            return httpx.Response(200, json={"status": "success", "data": {"access_token": "AT", "user_name": "U"}})

        got = await exchange_request_token("req", "KEY", "SECRET", httpx.AsyncClient(transport=httpx.MockTransport(h)))
        assert got == {"access_token": "AT", "user": "U"}

        def bad(req: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"status": "error", "message": "Token is invalid or has expired."})

        with pytest.raises(KiteAuthError, match="expired"):
            await exchange_request_token("x", "K", "S", httpx.AsyncClient(transport=httpx.MockTransport(bad)))

    async def test_download_rejects_proxy_page_and_caches(self, tmp_path):
        def h(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=_nse_csv() if req.url.path.endswith("NSE") else _nfo_csv())

        ins = await download_instruments(tmp_path, "K", "T", client=httpx.AsyncClient(transport=httpx.MockTransport(h)))
        assert ins.kite_symbol(CE) == "NFO:BANKNIFTY26OCT56000CE"

        def blocked(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>Access denied</html>")

        await download_instruments(tmp_path, "K", "T", client=httpx.AsyncClient(transport=httpx.MockTransport(blocked)))
        with pytest.raises(KiteError, match="not CSV"):
            await download_instruments(
                tmp_path / "x", "K", "T", client=httpx.AsyncClient(transport=httpx.MockTransport(blocked))
            )


async def test_full_pipeline_on_mocked_kite(provider):
    """Ingest -> engine -> signal with Kite as the provider (mocked API); provenance says kite."""
    from mp_core.pipeline import Engine, Ingestor
    from mp_core.store import MarketStore, MemoryKV

    store = MarketStore(MemoryKV())
    ing = Ingestor(provider, store, "NSE:BANKNIFTY")
    await asyncio.wait_for(ing.run(max_ticks=2), 10)
    snap = await Engine(store, "NSE:BANKNIFTY").compute_once()
    assert snap.source is Source.KITE and snap.quote.source is Source.KITE
    assert snap.expiries and snap.expiries[0].value.expiry == EXP
    assert snap.constituents is not None and snap.signal is not None
    assert snap.macro.india_vix.value == 14.2
