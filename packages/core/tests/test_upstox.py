"""Upstox adapter against a mocked API (documented response shapes). It has not been run against
the real API - see `mp upstox-check`."""

import gzip
import json
from datetime import UTC, date, datetime
from urllib.parse import unquote

import httpx
import pytest
from pydantic import ValidationError

from mp_core import market_calendar as cal
from mp_core.clock import SimClock
from mp_core.models import InstrumentKey, Interval, OptionType, Source
from mp_core.providers.base import DataUnavailable
from mp_core.providers.upstox import (
    InstrumentMaster,
    UpstoxAuthError,
    UpstoxError,
    UpstoxProvider,
    authorize_url,
    download_instruments,
    exchange_code,
)

NOW = datetime(2026, 10, 20, 11, 0, tzinfo=cal.IST)
EXP = date(2026, 10, 27)
EXP_MS = int(datetime(2026, 10, 27, tzinfo=cal.IST).timestamp() * 1000)
BN = InstrumentKey.parse("NSE:BANKNIFTY")
FUT = InstrumentKey.parse("NSE:BANKNIFTY:2026-10-27")
CE = InstrumentKey(exchange="NSE", underlying="BANKNIFTY", expiry=EXP, strike=56000, option_type=OptionType.CE)

INSTRUMENTS = [
    {
        "segment": "NSE_FO",
        "instrument_key": "NSE_FO|1001",
        "trading_symbol": "BANKNIFTY 26 OCT FUT",
        "instrument_type": "FUT",
        "underlying_symbol": "BANKNIFTY",
        "expiry": EXP_MS,
        "lot_size": 30,
    },
    {
        "segment": "NSE_FO",
        "instrument_key": "NSE_FO|2001",
        "trading_symbol": "BANKNIFTY 56000 CE",
        "instrument_type": "CE",
        "underlying_symbol": "BANKNIFTY",
        "expiry": EXP_MS,
        "strike_price": 56000.0,
        "lot_size": 30,
    },
    {
        "segment": "NSE_FO",
        "instrument_key": "NSE_FO|2002",
        "trading_symbol": "BANKNIFTY 56000 PE",
        "instrument_type": "PE",
        "underlying_symbol": "BANKNIFTY",
        "expiry": EXP_MS,
        "strike_price": 56000.0,
        "lot_size": 30,
    },
    {
        "segment": "NSE_FO",
        "instrument_key": "NSE_FO|9",
        "trading_symbol": "SBIN FUT",
        "instrument_type": "FUT",
        "underlying_symbol": "SBIN",
        "expiry": EXP_MS,
    },  # stock derivative: ignored
    {
        "segment": "NSE_EQ",
        "instrument_key": "NSE_EQ|INE040A01034",
        "trading_symbol": "HDFCBANK",
        "instrument_type": "EQ",
    },
    {
        "segment": "NSE_EQ",
        "instrument_key": "NSE_EQ|INE090A01021",
        "trading_symbol": "ICICIBANK",
        "instrument_type": "EQ",
    },
    {"garbage": True},
]


def quote_entry(token, ltp, change=100.0, ts="2026-10-20T11:00:00.000+05:30", oi=None):
    return {
        "instrument_token": token,
        "last_price": ltp,
        "net_change": change,
        "volume": 1000,
        "oi": oi,
        "timestamp": ts,
        "ohlc": {"open": ltp - 50, "high": ltp + 80, "low": ltp - 120, "close": ltp - 10},
        "depth": {},
    }


class FakeUpstox:
    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.ltp = 56000.0
        self.ltp_polls = 0
        self.fail_with: list[int] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        if self.fail_with:
            return httpx.Response(self.fail_with.pop(0), json={"status": "error", "errors": [{"message": "x"}]})
        assert req.headers["authorization"] == "Bearer TOKEN"
        path = unquote(req.url.path)
        keys = req.url.params.get("instrument_key", "")
        if path == "/v2/market-quote/quotes":
            data = {}
            for k in keys.split(","):
                ltp = {"NSE_INDEX|Nifty Bank": 56010.0, "NSE_INDEX|India VIX": 14.2}.get(k, 1000.0)
                data[k.replace("|", ":")] = quote_entry(k, ltp)
            return httpx.Response(200, json={"status": "success", "data": data})
        if path == "/v2/market-quote/ltp":
            self.ltp_polls += 1
            if self.ltp_polls > 1 and self.ltp_polls % 2:  # the price moves on every other poll
                self.ltp += 5
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "NSE_INDEX:Nifty Bank": {"instrument_token": "NSE_INDEX|Nifty Bank", "last_price": self.ltp}
                    },
                },
            )
        if path == "/v2/option/chain":
            assert req.url.params["expiry_date"] == "2026-10-27"
            side = {
                "instrument_key": "NSE_FO|2001",
                "market_data": {
                    "ltp": 410.5,
                    "volume": 900,
                    "oi": 15000.0,
                    "prev_oi": 12000.0,
                    "bid_price": 410.0,
                    "ask_price": 411.0,
                    "close_price": 380.0,
                },
                "option_greeks": {"iv": 15.1, "delta": 0.52},
            }
            put = {
                **side,
                "instrument_key": "NSE_FO|2002",
                "market_data": {**side["market_data"], "ltp": 395.0, "oi": 9000.0, "prev_oi": 10000.0},
            }
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": [
                        {
                            "expiry": "2026-10-27",
                            "pcr": 0.6,
                            "strike_price": 56000.0,
                            "underlying_key": keys,
                            "underlying_spot_price": 56010.0,
                            "call_options": side,
                            "put_options": put,
                        }
                    ],
                },
            )
        if path.startswith("/v3/historical-candle/intraday/"):
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "candles": [
                            [
                                "2026-10-20T10:59:00+05:30",
                                56005,
                                56012,
                                56001,
                                56010,
                                0,
                                0,
                            ],  # still forming at 11:00? no: closed
                            ["2026-10-20T10:58:00+05:30", 56000, 56008, 55990, 56005, 0, 0],
                        ]
                    },
                },
            )
        if path.startswith("/v3/historical-candle/"):
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "candles": [
                            ["2026-10-19T00:00:00+05:30", 55800, 56100, 55700, 55910, 0, 0],
                            ["2026-10-16T00:00:00+05:30", 55500, 55900, 55400, 55850, 0, 0],
                        ]
                    },
                },
            )
        if path == "/v2/user/profile":
            return httpx.Response(200, json={"status": "success", "data": {"user_name": "Test"}})
        return httpx.Response(404, json={"status": "error"})


@pytest.fixture
def fake():
    return FakeUpstox()


@pytest.fixture
def provider(fake):
    return UpstoxProvider(
        "TOKEN",
        InstrumentMaster(INSTRUMENTS),
        clock=SimClock(NOW),
        poll_seconds=0.01,
        client=httpx.AsyncClient(transport=httpx.MockTransport(fake)),
    )


class TestInstrumentMaster:
    def test_maps_both_ways(self):
        m = InstrumentMaster(INSTRUMENTS)
        assert m.broker(FUT) == "NSE_FO|1001" and m.broker(CE) == "NSE_FO|2001"
        assert m.broker(BN) == "NSE_INDEX|Nifty Bank"
        assert m.from_broker["NSE_EQ|INE040A01034"] == InstrumentKey.parse("NSE:HDFCBANK", is_index=False)
        assert m.lot_size[FUT.id] == 30 and m.expiries["BANKNIFTY"] == {EXP}
        assert "NSE_FO|9" not in m.from_broker  # stock derivatives ignored
        with pytest.raises(DataUnavailable, match="not in the Upstox instrument list"):
            m.broker(InstrumentKey.parse("NSE:BANKNIFTY:2026-11-24"))


class TestProvider:
    async def test_quote(self, provider, fake):
        q = await provider.get_quote(BN)
        assert q.source is Source.UPSTOX and not q.stale
        assert q.value.ltp == 56010.0 and q.value.prev_close == 55910.0  # last - net_change
        assert q.value.ts == datetime(2026, 10, 20, 5, 30, tzinfo=UTC)
        assert fake.calls[0].url.params["instrument_key"] == "NSE_INDEX|Nifty Bank"
        assert "%7C" in str(fake.calls[0].url)  # '|' is URL-encoded

    async def test_option_chain(self, provider):
        ch = await provider.get_option_chain(BN, EXP)
        assert ch.source is Source.UPSTOX and ch.value.spot == 56010.0
        row = ch.value.rows[0]
        assert row.call.oi == 15000 and row.call.oi_change == 3000 and row.put.oi_change == -1000
        assert row.call.key == CE and row.call.bid <= row.call.ltp <= row.call.ask

    async def test_candles_merge_order_and_completeness(self, provider, fake):
        start = datetime(2026, 10, 15, tzinfo=cal.IST)
        daily = await provider.get_candles(BN, Interval.D1, start, NOW)
        assert [c.ts.astimezone(cal.IST).date() for c in daily.value] == [date(2026, 10, 16), date(2026, 10, 19)]
        minute = await provider.get_candles(BN, Interval.M1, datetime(2026, 10, 20, 9, 15, tzinfo=cal.IST), NOW)
        assert [c.ts.astimezone(cal.IST).strftime("%H:%M") for c in minute.value] == ["10:58", "10:59"]  # oldest first
        paths = [unquote(c.url.path) for c in fake.calls]
        assert "/v3/historical-candle/NSE_INDEX|Nifty Bank/days/1/2026-10-19/2026-10-15" in paths
        assert "/v3/historical-candle/intraday/NSE_INDEX|Nifty Bank/minutes/1" in paths

    async def test_forming_bar_is_dropped(self, fake):
        p = UpstoxProvider(
            "TOKEN",
            InstrumentMaster(INSTRUMENTS),
            clock=SimClock(datetime(2026, 10, 20, 10, 59, 30, tzinfo=cal.IST)),
            client=httpx.AsyncClient(transport=httpx.MockTransport(fake)),
        )
        bars = await p.get_candles(BN, Interval.M1, datetime(2026, 10, 20, 9, 15, tzinfo=cal.IST), p.clock.now())
        assert [c.ts.astimezone(cal.IST).strftime("%H:%M") for c in bars.value] == ["10:58"]

    async def test_expiries_and_macro(self, provider):
        assert await provider.get_expiries(BN) == [EXP]
        m = await provider.get_macro()
        assert m.india_vix.value == 14.2 and m.india_vix.source is Source.UPSTOX
        assert m.usdinr is None and m.fii_net_cr is None  # not available from Upstox: missing, not guessed

    async def test_constituents_use_configured_weights(self, provider):
        cons = (await provider.get_constituents(BN)).value
        assert {c.key.underlying for c in cons} == {"HDFCBANK", "ICICIBANK"}  # only those in the instrument list
        assert all(c.weight > 0 for c in cons)

    async def test_tick_polling_yields_changes_only(self, provider, fake):
        import asyncio

        seen = []

        async def take():
            async for t in provider.stream_ticks([BN]):
                seen.append(t.ltp)
                if len(seen) == 3:
                    return

        await asyncio.wait_for(take(), 5)  # never hang the suite on a polling loop
        assert seen == [56000.0, 56005.0, 56010.0]
        assert fake.ltp_polls == 5  # the two unchanged polls produced no tick

    async def test_401_fails_loudly(self, provider, fake):
        fake.fail_with = [401]
        with pytest.raises(UpstoxAuthError, match="expired"):
            await provider.get_quote(BN)

    async def test_429_and_5xx_are_retried(self, provider, fake, monkeypatch):
        import asyncio

        async def no_sleep(_):
            return None

        monkeypatch.setattr(asyncio, "sleep", no_sleep)
        fake.fail_with = [429, 503]
        assert (await provider.get_quote(BN)).value.ltp == 56010.0
        fake.fail_with = [500, 500, 500]
        with pytest.raises(UpstoxError, match="HTTP 500"):
            await provider.get_quote(BN)

    async def test_unexpected_shape_fails_loudly(self, provider, fake):
        orig = fake.__call__

        def broken(req):
            if req.url.path == "/v2/option/chain":
                return httpx.Response(200, json={"status": "success", "data": [{"strike": 56000}]})
            return orig(req)

        provider.client = httpx.AsyncClient(transport=httpx.MockTransport(broken))
        with pytest.raises(ValidationError, match="strike_price"):
            await provider.get_option_chain(BN, EXP)

    async def test_no_order_methods_and_token_required(self, provider):
        assert not [m for m in dir(provider) if any(w in m.lower() for w in ("order", "position", "fund"))]
        with pytest.raises(UpstoxAuthError):
            UpstoxProvider("", InstrumentMaster([]))

    async def test_check(self, provider):
        c = await provider.check()
        assert c["user"] == "Test" and c["banknifty"] == 56010.0 and c["expiries"] == ["2026-10-27"]


class TestLoginAndInstruments:
    def test_authorize_url(self):
        u = authorize_url("KEY", "http://127.0.0.1:8000/")
        assert u.startswith("https://api.upstox.com/v2/login/authorization/dialog?")
        assert "client_id=KEY" in u and "response_type=code" in u

    async def test_exchange_code(self):
        def h(req):
            assert b"grant_type=authorization_code" in req.content and b"code=abc" in req.content
            return httpx.Response(200, json={"access_token": "TOK"})

        assert await exchange_code("abc", "k", "s", "r", httpx.AsyncClient(transport=httpx.MockTransport(h))) == "TOK"

        def bad(req):
            return httpx.Response(401, json={"status": "error", "errors": [{"message": "invalid code"}]})

        with pytest.raises(UpstoxAuthError, match="401"):
            await exchange_code("x", "k", "s", "r", httpx.AsyncClient(transport=httpx.MockTransport(bad)))

    async def test_download_rejects_proxy_page_and_caches(self, tmp_path):
        good = gzip.compress(json.dumps(INSTRUMENTS).encode())

        def h(req):
            return httpx.Response(200, content=good)

        dest = tmp_path / "NSE.json.gz"
        await download_instruments(dest, httpx.AsyncClient(transport=httpx.MockTransport(h)))
        assert InstrumentMaster.load(dest).broker(FUT) == "NSE_FO|1001"

        def blocked(req):
            return httpx.Response(200, content=b"<html>Blocked site</html>")

        with pytest.raises(UpstoxError, match="not gzip"):
            await download_instruments(
                tmp_path / "x.json.gz", httpx.AsyncClient(transport=httpx.MockTransport(blocked))
            )
        # fresh cache: no download at all
        await download_instruments(dest, httpx.AsyncClient(transport=httpx.MockTransport(blocked)))


async def test_full_pipeline_on_mocked_upstox(provider):
    """Ingest -> engine -> signal with Upstox as the provider (mocked API): the adapter plugs into
    everything built for the replay provider, and the provenance says upstox, not synthetic."""
    import asyncio

    from mp_core.pipeline import Engine, Ingestor
    from mp_core.store import MarketStore, MemoryKV

    store = MarketStore(MemoryKV())
    ing = Ingestor(provider, store, "NSE:BANKNIFTY")  # next month's future is not listed: skipped, not fatal
    await asyncio.wait_for(ing.run(max_ticks=2), 10)
    snap = await Engine(store, "NSE:BANKNIFTY").compute_once()
    assert snap.source is Source.UPSTOX and snap.quote.source is Source.UPSTOX
    assert snap.expiries and snap.expiries[0].value.expiry == EXP
    assert snap.constituents is not None and snap.signal is not None
    assert snap.macro.india_vix.value == 14.2
