import math

import pytest

from mp_core.analytics.black_scholes import MIN_T, IVStatus, greeks, implied_vol, price
from mp_core.models import OptionType

CE, PE = OptionType.CE, OptionType.PE
MINUTE = 1 / (365 * 24 * 60)


def test_textbook_value():
    # Hull: S=100, K=100, T=1, r=5%, sigma=20% -> call 10.4506, put 5.5735
    assert price(100, 100, 1, 0.05, 0.2, CE) == pytest.approx(10.4506, abs=1e-4)
    assert price(100, 100, 1, 0.05, 0.2, PE) == pytest.approx(5.5735, abs=1e-4)


@pytest.mark.parametrize("k", [40000, 52000, 60000])
@pytest.mark.parametrize("t", [1 / 365, 7 / 365, 0.25])
def test_put_call_parity(k, t):
    s, r, v = 52000, 0.065, 0.18
    assert price(s, k, t, r, v, CE) - price(s, k, t, r, v, PE) == pytest.approx(s - k * math.exp(-r * t), abs=1e-6)


def test_zero_time_is_intrinsic():
    assert price(52100, 52000, 0, 0.065, 0.2, CE) == pytest.approx(100)
    assert price(52100, 52000, 0, 0.065, 0.2, PE) == 0


def test_rejects_bad_inputs():
    with pytest.raises(ValueError):
        price(0, 100, 1, 0.05, 0.2, CE)
    with pytest.raises(ValueError):
        price(100, 100, -1, 0.05, 0.2, CE)


class TestGreeks:
    def test_textbook_greeks(self):
        # Hull example 19.x: S=49, K=50, r=5%, sigma=20%, T=0.3846
        g = greeks(49, 50, 0.3846, 0.05, 0.2, CE)
        assert g.delta == pytest.approx(0.522, abs=1e-3)
        assert g.gamma == pytest.approx(0.066, abs=1e-3)
        assert g.vega_per_pct * 100 == pytest.approx(12.1, abs=0.1)
        assert g.theta_per_day * 365 == pytest.approx(-4.31, abs=0.01)
        assert g.rho_per_pct * 100 == pytest.approx(8.91, abs=0.01)

    @pytest.mark.parametrize("ot", [CE, PE])
    @pytest.mark.parametrize("k, t, v", [(52000, 30 / 365, 0.18), (50000, 2 / 365, 0.3), (55000, 0.5, 0.12)])
    def test_against_finite_differences(self, ot, k, t, v):
        s, r = 52000.0, 0.065
        g = greeks(s, k, t, r, v, ot)
        h = 1.0
        assert g.delta == pytest.approx(
            (price(s + h, k, t, r, v, ot) - price(s - h, k, t, r, v, ot)) / (2 * h), abs=1e-5
        )
        fd_gamma = (price(s + h, k, t, r, v, ot) - 2 * price(s, k, t, r, v, ot) + price(s - h, k, t, r, v, ot)) / h**2
        assert g.gamma == pytest.approx(fd_gamma, rel=1e-3, abs=1e-8)
        dv = 1e-4
        fd_vega = (price(s, k, t, r, v + dv, ot) - price(s, k, t, r, v - dv, ot)) / (2 * dv) / 100
        assert g.vega_per_pct == pytest.approx(fd_vega, rel=1e-5)
        dt = 1e-6  # instantaneous theta; a 1-day difference is badly biased near expiry
        fd_theta = (price(s, k, t - dt, r, v, ot) - price(s, k, t + dt, r, v, ot)) / (2 * dt) / 365
        assert g.theta_per_day == pytest.approx(fd_theta, rel=1e-4, abs=1e-6)

    def test_call_put_relationships(self):
        c, p = greeks(52000, 52500, 0.05, 0.065, 0.2, CE), greeks(52000, 52500, 0.05, 0.065, 0.2, PE)
        assert c.delta - p.delta == pytest.approx(1.0)
        assert c.gamma == pytest.approx(p.gamma) and c.vega_per_pct == pytest.approx(p.vega_per_pct)

    @pytest.mark.parametrize("s, ot, delta", [(52100, CE, 1.0), (51900, CE, 0.0), (51900, PE, -1.0), (52100, PE, 0.0)])
    def test_expired_greeks_are_payoff_greeks(self, s, ot, delta):
        g = greeks(s, 52000, MIN_T / 2, 0.065, 0.2, ot)
        assert g.expired and g.delta == delta and g.gamma == g.vega_per_pct == g.theta_per_day == 0
        assert g.price == (max(s - 52000, 0) if ot is CE else max(52000 - s, 0))

    def test_gamma_explodes_but_stays_finite_near_expiry(self):
        g = greeks(52000, 52000, 5 * MINUTE, 0.065, 0.15, CE)
        assert (
            not g.expired and math.isfinite(g.gamma) and g.gamma > greeks(52000, 52000, 7 / 365, 0.065, 0.15, CE).gamma
        )
        assert g.delta == pytest.approx(0.5, abs=0.01)


class TestImpliedVol:
    @pytest.mark.parametrize("ot", [CE, PE])
    @pytest.mark.parametrize("k", [48000, 51000, 52000, 53000, 56000])
    @pytest.mark.parametrize("t", [30 * MINUTE, 1 / 365, 7 / 365, 30 / 365, 1.0])
    @pytest.mark.parametrize("v", [0.08, 0.18, 0.45, 1.2])
    def test_roundtrip(self, ot, k, t, v):
        s, r = 52000.0, 0.065
        px = price(s, k, t, r, v, ot)
        res = implied_vol(px, s, k, t, r, ot, tick=1e-9)
        lower = max(s - k * math.exp(-r * t), 0) if ot is CE else max(k * math.exp(-r * t) - s, 0)
        if px - lower < 1e-6:  # (almost) no time value: either refused, or solved without precision claims
            assert res.status in (IVStatus.NO_TIME_VALUE, IVStatus.MIN_TICK, IVStatus.OK)
        else:
            assert res.ok and res.iv == pytest.approx(v, rel=1e-4)

    def test_with_real_tick_size(self):
        px = round(price(52000, 52000, 3 / 365, 0.065, 0.16, CE) / 0.05) * 0.05
        assert implied_vol(px, 52000, 52000, 3 / 365, 0.065, CE).iv == pytest.approx(0.16, abs=2e-4)

    def test_expired(self):
        assert implied_vol(10, 52000, 52000, MIN_T / 2, 0.065, CE).status is IVStatus.EXPIRED

    def test_below_intrinsic(self):
        assert implied_vol(50, 52100, 52000, 1 / 365, 0.065, CE).status is IVStatus.BELOW_INTRINSIC

    def test_above_upper_bound(self):
        assert implied_vol(53000, 52000, 52000, 1 / 365, 0.065, CE).status is IVStatus.ABOVE_UPPER_BOUND

    def test_min_tick_deep_otm(self):
        assert implied_vol(0.05, 52000, 56000, 1 / 365, 0.065, CE).status is IVStatus.MIN_TICK

    def test_no_time_value_deep_itm(self):
        intrinsic = 52000 - 48000 * math.exp(-0.065 / 365)
        assert implied_vol(intrinsic + 0.02, 52000, 48000, 1 / 365, 0.065, CE).status is IVStatus.NO_TIME_VALUE

    def test_negative_price_rejected(self):
        with pytest.raises(ValueError):
            implied_vol(-1, 52000, 52000, 0.1, 0.065, CE)
