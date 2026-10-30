"""Repo-wide pytest config."""

import asyncio
import sys

import pytest

if sys.platform == "win32":

    def pytest_asyncio_loop_factories(config, item):
        # async psycopg cannot use Windows' default Proactor loop
        return {"selector": asyncio.SelectorEventLoop}


@pytest.fixture(autouse=True)
def _test_hosts(monkeypatch):
    """The gateway only answers allowed Host names; test clients use "testserver" and "t"."""
    monkeypatch.setenv("MP_ALLOWED_HOSTS", "testserver,t")
