"""Expected move from implied volatility: spot * IV * sqrt(calendar_days / 365)."""

from __future__ import annotations

import math
from datetime import datetime

from pydantic import Field, model_validator

from mp_core import market_calendar as cal
from mp_core.models import StrictModel

FORMULA = "spot * iv * sqrt(calendar_days / 365)"


class Band(StrictModel):
    lower: float
    upper: float

    @model_validator(mode="after")
    def _ordered(self) -> Band:
        if self.lower > self.upper:
            raise ValueError(f"band lower {self.lower} > upper {self.upper}")
        return self

    def contains(self, x: float) -> bool:
        return self.lower <= x <= self.upper


class ExpectedRange(StrictModel):
    spot: float = Field(gt=0)
    iv: float = Field(gt=0, description="annualised, decimal (0.18 = 18%)")
    calendar_days: float = Field(ge=0)
    sigma_points: float = Field(ge=0, description="1-sigma move in index points")
    sigma_pct: float = Field(ge=0)
    one_sigma: Band
    two_sigma: Band
    formula: str = FORMULA


def expected_move(spot: float, iv: float, calendar_days: float) -> ExpectedRange:
    if spot <= 0:
        raise ValueError(f"spot must be positive, got {spot}")
    if not 0 < iv < 5:
        raise ValueError(f"iv must be a decimal in (0, 5), got {iv} - pass 0.18, not 18")
    if calendar_days < 0:
        raise ValueError(f"calendar_days must be >= 0, got {calendar_days}")
    sigma = spot * iv * math.sqrt(calendar_days / 365.0)
    return ExpectedRange(
        spot=spot,
        iv=iv,
        calendar_days=calendar_days,
        sigma_points=sigma,
        sigma_pct=100.0 * sigma / spot,
        one_sigma=Band(lower=spot - sigma, upper=spot + sigma),
        two_sigma=Band(lower=spot - 2 * sigma, upper=spot + 2 * sigma),
    )


def calendar_days_until(now: datetime, target: datetime) -> float:
    """Fractional calendar days between two instants (e.g. now -> next session's close)."""
    return max((target - now).total_seconds(), 0.0) / 86400.0


def days_to_next_close(now: datetime, sessions: int = 1) -> float:
    """Calendar days from `now` to the close `sessions` trading sessions ahead. If the market is
    open, the current session counts as the first."""
    d = now.astimezone(cal.IST).date()
    if not cal.is_trading_day(d) or now >= cal.session_bounds(d)[1]:
        d = cal.next_trading_day(d)
    for _ in range(sessions - 1):
        d = cal.next_trading_day(d)
    return calendar_days_until(now, cal.session_bounds(d)[1])
