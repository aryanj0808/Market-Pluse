from datetime import date, datetime
from pathlib import Path

import pytest

from mp_core import market_calendar as cal
from mp_core.synthetic.generator import generate

SEED = 2
START = date(2026, 9, 1)
DAYS = 25  # seed 2: calm/normal/stressed regimes, two gap days + crush, a jump, the 29-Sep expiry


@pytest.fixture(scope="session")
def synth_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("synth") / "BANKNIFTY"
    generate("NSE:BANKNIFTY", out, start=START, days=DAYS, seed=SEED)
    return out


def ist(d: date, hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, ss, tzinfo=cal.IST)
