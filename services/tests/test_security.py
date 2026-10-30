"""Gateway protections: Host allow-list (DNS rebinding) and Origin checks (cross-site requests)."""

import httpx
import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from mp_core.store import MarketStore, MemoryKV
from mp_services.gateway import _origin_ok, create_app


@pytest.fixture
def app():
    return create_app(MarketStore(MemoryKV()))


def client(app, base_url="http://127.0.0.1:8000"):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base_url)


def test_origin_rule():
    hosts = ["127.0.0.1", "localhost"]
    assert _origin_ok(None, hosts)  # curl, scripts, MCP clients
    assert _origin_ok("http://127.0.0.1:8000", hosts) and _origin_ok("http://localhost:5173", hosts)
    assert not _origin_ok("https://evil.example", hosts)
    assert not _origin_ok("http://127.0.0.1.evil.example", hosts)
    assert not _origin_ok("null", hosts)  # sandboxed iframes / file:// pages


async def test_unknown_host_header_is_refused(app):
    async with client(app, "http://rebind.evil.example:8000") as c:
        assert (await c.get("/api/health")).status_code == 400
    async with client(app) as c:
        assert (await c.get("/api/health")).status_code == 200


async def test_cross_site_ask_is_refused_before_it_reaches_claude(app):
    async with client(app) as c:
        evil = await c.post(
            "/api/ask/NSE:BANKNIFTY", json={"question": "hi"}, headers={"Origin": "https://evil.example"}
        )
        same = await c.post(
            "/api/ask/NSE:BANKNIFTY", json={"question": "hi"}, headers={"Origin": "http://127.0.0.1:8000"}
        )
    assert evil.status_code == 403
    assert same.status_code == 503  # passes the origin check; Ask-Claude is simply not configured here


def test_cross_site_websocket_is_closed(app):
    with TestClient(app, base_url="http://127.0.0.1:8000") as tc:
        with (
            pytest.raises(WebSocketDisconnect) as ex,
            tc.websocket_connect("/api/stream/NSE:BANKNIFTY", headers={"Origin": "https://evil.example"}),
        ):
            pass
        assert ex.value.code == 4403


def test_allowed_hosts_can_be_extended(monkeypatch):
    monkeypatch.setenv("MP_ALLOWED_HOSTS", "my-pc.tailnet.ts.net")
    from mp_core.net import allowed_hosts

    assert allowed_hosts() == ["127.0.0.1", "localhost", "my-pc.tailnet.ts.net"]
