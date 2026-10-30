"""NSE trading calendar: sessions, holidays, expiries. Times are IST at the edges, UTC inside."""

from __future__ import annotations

import calendar as _cal
import logging
import tomllib
from datetime import UTC, date, datetime, time, timedelta
from functools import cache
from importlib import resources
from zoneinfo import ZoneInfo

from mp_core.registry import WEEKDAYS, IndexSpec

log = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)
SESSION_MINUTES = 375


@cache
def _holidays() -> tuple[frozenset[date], frozenset[int]]:
    raw = tomllib.loads(resources.files("mp_core.data").joinpath("holidays.toml").read_text("utf-8"))
    return frozenset(date.fromisoformat(d) for d in raw["dates"]), frozenset(raw["complete_years"])


_warned_years: set[int] = set()


def holidays_known(year: int) -> bool:
    return year in _holidays()[1]


def is_trading_day(d: date) -> bool:
    if d.weekday() >= 5:
        return False
    holidays, complete = _holidays()
    if d.year not in complete and d.year not in _warned_years:
        _warned_years.add(d.year)
        log.warning("NSE holiday list for %s not loaded; treating all weekdays as trading days", d.year)
    return d not in holidays


def trading_days(start: date, end: date) -> list[date]:
    """Inclusive range of trading days."""
    out, d = [], start
    while d <= end:
        if is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def next_trading_day(d: date) -> date:
    d += timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def prev_trading_day(d: date) -> date:
    d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def session_bounds(d: date) -> tuple[datetime, datetime]:
    """(open, close) of the trading session on `d`, in UTC."""
    return (
        datetime.combine(d, MARKET_OPEN, IST).astimezone(UTC),
        datetime.combine(d, MARKET_CLOSE, IST).astimezone(UTC),
    )


def expiry_close(expiry: date) -> datetime:
    """Options expire at the close of the expiry day."""
    return session_bounds(expiry)[1]


def _roll_back(d: date) -> date:
    """NSE moves an expiry falling on a holiday to the previous trading day."""
    return d if is_trading_day(d) else prev_trading_day(d)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    last = date(year, month, _cal.monthrange(year, month)[1])
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def monthly_expiry(spec: IndexSpec, year: int, month: int) -> date:
    return _roll_back(_last_weekday(year, month, WEEKDAYS[spec.monthly_expiry[5:]]))


def front_future_expiry(spec: IndexSpec, on: date) -> date:
    """Expiry of the front-month future trading on `on` (index futures are monthly only)."""
    exp = monthly_expiry(spec, on.year, on.month)
    if exp < on:
        exp = monthly_expiry(spec, on.year + (on.month == 12), on.month % 12 + 1)
    return exp


def expiries(spec: IndexSpec, start: date, end: date) -> list[date]:
    """All expiries in [start, end], weekly and monthly, sorted, de-duplicated."""
    found: set[date] = set()
    y, m = start.year, start.month
    while date(y, m, 1) <= end:
        found.add(monthly_expiry(spec, y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    if spec.weekly_expiry:
        wd = WEEKDAYS[spec.weekly_expiry]
        d = start + timedelta(days=(wd - start.weekday()) % 7)
        while d <= end + timedelta(days=7):
            found.add(_roll_back(d))
            d += timedelta(days=7)
    return sorted(e for e in found if start <= e <= end)


def upcoming_expiries(spec: IndexSpec, on: date, count: int = 2) -> list[date]:
    """Next `count` expiries on or after `on` (an expiry day counts until it closes)."""
    return expiries(spec, on, on + timedelta(days=70))[:count]


def years_to_expiry(now: datetime, expiry: date) -> float:
    """Calendar time to expiry close, in years (ACT/365). Floored at 0."""
    secs = (expiry_close(expiry) - now).total_seconds()
    return max(secs, 0.0) / (365.0 * 86400.0)
