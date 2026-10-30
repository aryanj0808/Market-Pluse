"""Latency targets in a real browser. Run with: uv run pytest -m perf  (needs Redis and Edge)."""

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.perf
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))


def test_dashboard_meets_latency_targets(tmp_path):
    import perf_browser

    report = tmp_path / "perf.md"
    code = perf_browser.main(["--seconds", "45", "--loads", "3", "--report", str(report)])
    assert code == 0, report.read_text(encoding="utf-8")
