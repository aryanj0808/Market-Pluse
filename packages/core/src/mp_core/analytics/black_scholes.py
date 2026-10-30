"""Black-Scholes pricing, Greeks and implied volatility (European, continuous rate, no dividends).

NSE index options are European and cash-settled. Using spot with the risk-free rate is equivalent
to Black-76 on the same-expiry future when F = S*e^(rT); callers that have a traded future can pass
`spot=F*e^(-rT)` to price off the future instead.

Near-expiry handling:
  * t < MIN_T (one minute) is treated as expired: price = intrinsic, Greeks of the payoff, no IV.
  * An IV is only reported when it is identifiable. A price at the minimum tick, or with less than
    half a tick of time value, carries no volatility information and yields a status, not a number.
"""

from __future__ import annotations

import math
from enum import StrEnum

from pydantic import Field

from mp_core.models import OptionType, StrictModel

MIN_T = 60.0 / (365.0 * 86400.0)  # one minute, in years
SQRT_2PI = math.sqrt(2.0 * math.pi)
VOL_LO, VOL_HI = 1e-4, 5.0


def norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / SQRT_2PI


def _check(spot: float, strike: float, t_years: float, vol: float) -> None:
    if spot <= 0 or strike <= 0:
        raise ValueError(f"spot and strike must be positive (spot={spot}, strike={strike})")
    if vol < 0 or t_years < 0:
        raise ValueError(f"vol and time must be non-negative (vol={vol}, t={t_years})")


def _d1d2(spot: float, strike: float, t: float, rate: float, vol: float) -> tuple[float, float]:
    sd = vol * math.sqrt(t)
    d1 = (math.log(spot / strike) + (rate + 0.5 * vol * vol) * t) / sd
    return d1, d1 - sd


def intrinsic(spot: float, strike: float, option_type: OptionType) -> float:
    return max(spot - strike, 0.0) if option_type is OptionType.CE else max(strike - spot, 0.0)


def price(spot: float, strike: float, t_years: float, rate: float, vol: float, option_type: OptionType) -> float:
    _check(spot, strike, t_years, vol)
    df = math.exp(-rate * t_years)
    if t_years == 0 or vol == 0:
        fwd_intrinsic = spot - strike * df
        return max(fwd_intrinsic, 0.0) if option_type is OptionType.CE else max(-fwd_intrinsic, 0.0)
    d1, d2 = _d1d2(spot, strike, t_years, rate, vol)
    if option_type is OptionType.CE:
        return spot * norm_cdf(d1) - strike * df * norm_cdf(d2)
    return strike * df * norm_cdf(-d2) - spot * norm_cdf(-d1)


class Greeks(StrictModel):
    price: float
    delta: float  # per 1 point of spot
    gamma: float  # per 1 point of spot
    theta_per_day: float  # per calendar day
    vega_per_pct: float  # per 1 vol point (0.01)
    rho_per_pct: float  # per 1% rate
    expired: bool = False


def greeks(spot: float, strike: float, t_years: float, rate: float, vol: float, option_type: OptionType) -> Greeks:
    _check(spot, strike, t_years, vol)
    is_call = option_type is OptionType.CE
    if t_years < MIN_T or vol == 0:
        itm = spot > strike if is_call else spot < strike
        sign = 1.0 if is_call else -1.0
        return Greeks(
            price=intrinsic(spot, strike, option_type),
            delta=sign if itm else 0.0,
            gamma=0.0,
            theta_per_day=0.0,
            vega_per_pct=0.0,
            rho_per_pct=0.0,
            expired=t_years < MIN_T,
        )
    d1, d2 = _d1d2(spot, strike, t_years, rate, vol)
    df = math.exp(-rate * t_years)
    sqrt_t = math.sqrt(t_years)
    pdf = norm_pdf(d1)
    gamma = pdf / (spot * vol * sqrt_t)
    vega = spot * pdf * sqrt_t
    decay = -spot * pdf * vol / (2 * sqrt_t)
    if is_call:
        delta = norm_cdf(d1)
        theta = decay - rate * strike * df * norm_cdf(d2)
        rho = strike * t_years * df * norm_cdf(d2)
    else:
        delta = norm_cdf(d1) - 1.0
        theta = decay + rate * strike * df * norm_cdf(-d2)
        rho = -strike * t_years * df * norm_cdf(-d2)
    return Greeks(
        price=price(spot, strike, t_years, rate, vol, option_type),
        delta=delta,
        gamma=gamma,
        theta_per_day=theta / 365.0,
        vega_per_pct=vega / 100.0,
        rho_per_pct=rho / 100.0,
    )


class IVStatus(StrEnum):
    OK = "ok"
    EXPIRED = "expired"  # less than MIN_T to expiry
    BELOW_INTRINSIC = "below_intrinsic"  # arbitrage / stale print
    ABOVE_UPPER_BOUND = "above_upper_bound"  # call > spot or put > discounted strike
    MIN_TICK = "min_tick"  # price at the exchange's minimum tick: no information
    NO_TIME_VALUE = "no_time_value"  # (price - intrinsic) < half a tick
    OUT_OF_RANGE = "out_of_range"  # solution outside [VOL_LO, VOL_HI]


class IVResult(StrictModel):
    iv: float | None = Field(default=None, gt=0)
    status: IVStatus

    @property
    def ok(self) -> bool:
        return self.status is IVStatus.OK


def implied_vol(
    option_price: float,
    spot: float,
    strike: float,
    t_years: float,
    rate: float,
    option_type: OptionType,
    *,
    tick: float = 0.05,
    tol: float = 1e-7,
    max_iter: int = 100,
) -> IVResult:
    """Safeguarded Newton (falls back to bisection inside a maintained bracket)."""
    _check(spot, strike, t_years, 0.0)
    if option_price < 0:
        raise ValueError(f"negative option price {option_price}")
    if t_years < MIN_T:
        return IVResult(status=IVStatus.EXPIRED)
    df = math.exp(-rate * t_years)
    is_call = option_type is OptionType.CE
    lower = max(spot - strike * df, 0.0) if is_call else max(strike * df - spot, 0.0)
    upper = spot if is_call else strike * df
    if option_price < lower - 1e-9:
        return IVResult(status=IVStatus.BELOW_INTRINSIC)
    if option_price >= upper:
        return IVResult(status=IVStatus.ABOVE_UPPER_BOUND)
    if option_price <= tick + 1e-12:
        return IVResult(status=IVStatus.MIN_TICK)
    if option_price - lower < tick / 2:
        return IVResult(status=IVStatus.NO_TIME_VALUE)

    def f(v: float) -> float:
        return price(spot, strike, t_years, rate, v, option_type) - option_price

    lo, hi = VOL_LO, VOL_HI
    if f(lo) > 0 or f(hi) < 0:
        return IVResult(status=IVStatus.OUT_OF_RANGE)
    # Brenner-Subrahmanyam starting point, clamped into the bracket
    v = min(max(SQRT_2PI / math.sqrt(t_years) * option_price / spot, 0.05), 2.0)
    for _ in range(max_iter):
        diff = f(v)
        if abs(diff) < tol:
            return IVResult(iv=v, status=IVStatus.OK)
        if diff > 0:
            hi = v
        else:
            lo = v
        vega = spot * norm_pdf(_d1d2(spot, strike, t_years, rate, v)[0]) * math.sqrt(t_years)
        step = v - diff / vega if vega > 1e-12 else None
        v = step if step is not None and lo < step < hi else 0.5 * (lo + hi)
        if hi - lo < 1e-10:
            break
    return IVResult(iv=v, status=IVStatus.OK)
