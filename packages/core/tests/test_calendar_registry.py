from datetime import UTC, date, datetime

import pytest

from mp_core import market_calendar as cal
from mp_core.registry import get_index, load_indices


def test_registry_has_three_indices():
    assert {"NIFTY", "BANKNIFTY", "FINNIFTY"} <= set(load_indices())
    assert get_index("NSE:BANKNIFTY").strike_step == 100
    assert get_index("nifty").underlying == "NIFTY"
    with pytest.raises(KeyError, match="unknown index"):
        get_index("NSE:MIDCPNIFTY")


def test_banknifty_constituent_weights_sum_to_100():
    assert sum(c.weight for c in get_index("BANKNIFTY").constituents) == pytest.approx(100.0)


def test_session_bounds_in_utc():
    o, c = cal.session_bounds(date(2026, 10, 1))
    assert (o, c) == (datetime(2026, 10, 1, 3, 45, tzinfo=UTC), datetime(2026, 10, 1, 10, 0, tzinfo=UTC))


@pytest.mark.parametrize(
    "y, m, expected",
    [
        (2026, 9, date(2026, 9, 29)),
        (2026, 10, date(2026, 10, 27)),
        (2026, 11, date(2026, 11, 24)),
        (2026, 12, date(2026, 12, 29)),
    ],
)
def test_monthly_expiry_last_tuesday(y, m, expected):
    assert cal.monthly_expiry(get_index("BANKNIFTY"), y, m) == expected


def test_weekly_only_where_configured():
    nifty = cal.expiries(get_index("NIFTY"), date(2026, 10, 1), date(2026, 10, 31))
    bank = cal.expiries(get_index("BANKNIFTY"), date(2026, 10, 1), date(2026, 10, 31))
    assert nifty == [date(2026, 10, d) for d in (6, 13, 20, 27)]
    assert bank == [date(2026, 10, 27)]


def test_expiry_on_holiday_rolls_back(monkeypatch):
    monkeypatch.setattr(cal, "_holidays", lambda: (frozenset({date(2026, 10, 27)}), frozenset({2026})))
    assert cal.monthly_expiry(get_index("BANKNIFTY"), 2026, 10) == date(2026, 10, 26)
    assert not cal.is_trading_day(date(2026, 10, 27))


def test_upcoming_expiries_counts_expiry_day_itself():
    assert cal.upcoming_expiries(get_index("BANKNIFTY"), date(2026, 9, 29), 2) == [
        date(2026, 9, 29),
        date(2026, 10, 27),
    ]


def test_years_to_expiry():
    exp = date(2026, 10, 27)
    assert cal.years_to_expiry(cal.expiry_close(exp), exp) == 0
    assert cal.years_to_expiry(datetime(2026, 11, 1, tzinfo=UTC), exp) == 0
    one_day_before = cal.expiry_close(date(2026, 10, 26))
    assert cal.years_to_expiry(one_day_before, exp) == pytest.approx(1 / 365)


def test_holiday_list_status_is_honest():
    assert not cal.holidays_known(2026)  # nothing guessed; see data/holidays.toml
    assert not cal.is_trading_day(date(2026, 10, 3))  # Saturday


def test_front_future_expiry_is_monthly_and_rolls():
    nifty = get_index("NIFTY")  # has weeklies, but futures are monthly
    assert cal.front_future_expiry(nifty, date(2026, 10, 1)) == date(2026, 10, 27)
    assert cal.front_future_expiry(nifty, date(2026, 10, 27)) == date(2026, 10, 27)
    assert cal.front_future_expiry(nifty, date(2026, 10, 28)) == date(2026, 11, 24)
    assert cal.front_future_expiry(nifty, date(2026, 12, 30)) == date(2027, 1, 26)
