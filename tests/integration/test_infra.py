"""Run with: docker compose -f infra/docker-compose.yml --env-file .env up -d --wait
uv run pytest -m integration"""

import contextlib
import os
from pathlib import Path

import psycopg
import pytest
import redis

pytestmark = pytest.mark.integration


def _env() -> dict[str, str]:
    env = dict(os.environ)
    dotenv = Path(__file__).resolve().parents[2] / ".env"
    if dotenv.exists():
        for line in dotenv.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip())
    return env


def test_redis_roundtrip_and_pubsub():
    r = redis.Redis.from_url(_env()["REDIS_URL"])
    assert r.ping()
    p = r.pubsub()
    p.subscribe("mp:test")
    p.get_message(timeout=1)
    r.publish("mp:test", "hello")
    msg = p.get_message(timeout=2)
    assert msg and msg["data"] == b"hello"


def test_timescale_schema():
    with psycopg.connect(_env()["DATABASE_URL"]) as conn:
        hyper = {r[0] for r in conn.execute("select hypertable_name from timescaledb_information.hypertables")}
        assert {"ticks", "candles", "option_quotes", "signals"} <= hyper


def test_synthetic_outcomes_rejected_by_database():
    with psycopg.connect(_env()["DATABASE_URL"]) as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("insert into signal_outcomes values (gen_random_uuid(), now(), '1d', true, 1, 1, 'synthetic')")


async def test_history_reconnects_after_the_connection_is_killed():
    from mp_core.store.history import History

    h = await History(_env()["DATABASE_URL"]).connect()
    try:
        assert (await h.counts()) is not None
        cur = await (await h._ensure()).execute("select pg_backend_pid()")
        pid = (await cur.fetchone())[0]
        with psycopg.connect(_env()["DATABASE_URL"], autocommit=True) as admin:
            admin.execute("select pg_terminate_backend(%s)", (pid,))
        # the first call notices the dead connection (callers treat recording as best-effort)
        with contextlib.suppress(psycopg.Error):
            await h.counts()
        assert (await h.counts()) is not None and h.healthy  # ...the next one has reconnected
    finally:
        await h.close()
