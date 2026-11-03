"""Append-only, tamper-evident audit trail: every signal acted on, risk decision, order, fill, halt
and reconciliation result. One JSON object per line, one file per IST day; each line carries the
SHA-256 of the previous line, so an edited or deleted line breaks the chain (`verify`).

The file is the record of what the system *decided and did*; application logs are not.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from mp_core import market_calendar as cal

GENESIS = "0" * 64


class Audit(Protocol):
    def record(self, kind: str, ts: datetime, **payload: Any) -> None: ...


class MemoryAudit:
    """For tests and replays."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def record(self, kind: str, ts: datetime, **payload: Any) -> None:
        self.rows.append({"kind": kind, "ts": ts.isoformat(), **payload})

    def kinds(self) -> list[str]:
        return [r["kind"] for r in self.rows]


class JsonlAudit:
    def __init__(self, directory: str | Path) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._day: str | None = None
        self._prev = GENESIS

    def _path(self, day: str) -> Path:
        return self.dir / f"{day}.jsonl"

    def _open_day(self, day: str) -> None:
        self._day, self._prev = day, GENESIS
        path = self._path(day)
        if path.exists():  # continue the chain after a restart
            lines = path.read_text("utf-8").splitlines()
            if lines:
                self._prev = hashlib.sha256(lines[-1].encode()).hexdigest()

    def record(self, kind: str, ts: datetime, **payload: Any) -> None:
        day = ts.astimezone(cal.IST).date().isoformat()
        if day != self._day:
            self._open_day(day)
        line = json.dumps({"kind": kind, "ts": ts.isoformat(), "prev": self._prev, **payload}, default=str)
        with open(self._path(day), "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        self._prev = hashlib.sha256(line.encode()).hexdigest()


def verify(path: str | Path) -> tuple[bool, int, str]:
    """(intact, lines checked, message). Any edited, inserted or deleted line breaks the chain."""
    prev = GENESIS
    lines = Path(path).read_text("utf-8").splitlines()
    for i, line in enumerate(lines, 1):
        try:
            got = json.loads(line).get("prev")
        except json.JSONDecodeError:
            return False, i, f"line {i} is not JSON"
        if got != prev:
            return False, i, f"chain broken at line {i}"
        prev = hashlib.sha256(line.encode()).hexdigest()
    return True, len(lines), "intact"
