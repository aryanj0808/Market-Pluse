"""Repo-wide pytest config."""

import asyncio
import sys

if sys.platform == "win32":

    def pytest_asyncio_loop_factories(config, item):
        # async psycopg cannot use Windows' default Proactor loop
        return {"selector": asyncio.SelectorEventLoop}
