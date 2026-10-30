"""Option-chain analytics: IV per strike, ATM IV, skew, term structure, PCR, max pain."""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date, datetime

from pydantic import Field

from mp_core import market_calendar as cal
from mp_core.analytics.black_scholes import IVStatus, implied_vol
from mp_core.models import OptionChain, OptionQuote, OptionType, StrictModel


def option_mark(q: OptionQuote) -> float:
    """Mid when there is a two-sided market, else last traded price."""
    return 0.5 * (q.bid + q.ask) if q.bid > 0 and q.ask > 0 else q.ltp


class StrikeIV(StrictModel):
    strike: float
    call_iv: float | None
    call_status: IVStatus | None
    put_iv: float | None
    put_status: IVStatus | None

    def otm_iv(self, forward: float) -> float | None:
        """IV of the out-of-the-money side (more liquid, no early-exercise noise); falls back
        to the other side if the OTM one has no IV."""
        first, second = (self.call_iv, self.put_iv) if self.strike >= forward else (self.put_iv, self.call_iv)
        return first if first is not None else second


class IVSummary(StrictModel):
    expiry: date
    ts: datetime
    t_years: float
    spot: float
    forward: float
    atm_iv: float | None
    skew_25d: float | None = Field(
        description="IV(25-delta put) - IV(25-delta call); positive = puts richer. None if the chain is too narrow"
    )
    strikes: list[StrikeIV]
    n_ok: int
    n_failed: int


def chain_ivs(chain: OptionChain, now: datetime, rate: float) -> tuple[float, float, list[StrikeIV]]:
    """(t_years, forward, per-strike IVs). Forward is the same-expiry future when the chain has
    one, else spot carried at `rate`."""
    t = cal.years_to_expiry(now, chain.expiry)
    forward = chain.future if chain.future is not None else chain.spot * math.exp(rate * t)
    # price off the forward so a futures basis does not show up as skew
    s_eff = forward * math.exp(-rate * t)
    out = []
    for row in chain.rows:
        vals: dict[OptionType, tuple[float | None, IVStatus | None]] = {}
        for ot, q in ((OptionType.CE, row.call), (OptionType.PE, row.put)):
            if q is None:
                vals[ot] = (None, None)
                continue
            r = implied_vol(option_mark(q), s_eff, row.strike, t, rate, ot)
            vals[ot] = (r.iv, r.status)
        out.append(
            StrikeIV(
                strike=row.strike,
                call_iv=vals[OptionType.CE][0],
                call_status=vals[OptionType.CE][1],
                put_iv=vals[OptionType.PE][0],
                put_status=vals[OptionType.PE][1],
            )
        )
    return t, forward, out


def _interp_otm(strikes: Sequence[StrikeIV], k: float, forward: float) -> float | None:
    pts = [(s.strike, s.otm_iv(forward)) for s in strikes]
    pts = [(x, y) for x, y in pts if y is not None]
    if not pts or k < pts[0][0] or k > pts[-1][0]:
        return None
    for (x0, y0), (x1, y1) in zip(pts, pts[1:], strict=False):
        if x0 <= k <= x1:
            return y0 + (y1 - y0) * (k - x0) / (x1 - x0)
    return pts[0][1] if pts[0][0] == k else None


def delta_strikes(forward: float, vol: float, t: float, delta: float = 0.25) -> tuple[float, float]:
    """(put strike, call strike) at +/-`delta` forward delta, located with a single vol (the ATM
    IV) - the usual first-pass approximation; exact under a flat smile."""
    if not 0 < delta < 0.5:
        raise ValueError(f"delta must be in (0, 0.5), got {delta}")
    from statistics import NormalDist

    z = NormalDist().inv_cdf(1 - delta)
    sd = vol * math.sqrt(t)
    return forward * math.exp(-z * sd + 0.5 * sd * sd), forward * math.exp(z * sd + 0.5 * sd * sd)


def iv_summary(chain: OptionChain, now: datetime, rate: float) -> IVSummary:
    t, fwd, strikes = chain_ivs(chain, now, rate)
    atm = _interp_otm(strikes, fwd, fwd)
    skew = None
    if atm is not None and t > 0:
        k_put, k_call = delta_strikes(fwd, atm, t, 0.25)
        p_iv, c_iv = _interp_otm(strikes, k_put, fwd), _interp_otm(strikes, k_call, fwd)
        skew = p_iv - c_iv if p_iv is not None and c_iv is not None else None
    n_ok = sum((s.call_status is IVStatus.OK) + (s.put_status is IVStatus.OK) for s in strikes)
    n_all = sum((s.call_status is not None) + (s.put_status is not None) for s in strikes)
    return IVSummary(
        expiry=chain.expiry,
        ts=chain.ts,
        t_years=t,
        spot=chain.spot,
        forward=fwd,
        atm_iv=atm,
        skew_25d=skew,
        strikes=strikes,
        n_ok=n_ok,
        n_failed=n_all - n_ok,
    )


class TermPoint(StrictModel):
    expiry: date
    days: float
    atm_iv: float | None


def term_structure(chains: Sequence[OptionChain], now: datetime, rate: float) -> list[TermPoint]:
    out = []
    for ch in sorted(chains, key=lambda c: c.expiry):
        s = iv_summary(ch, now, rate)
        out.append(TermPoint(expiry=ch.expiry, days=s.t_years * 365, atm_iv=s.atm_iv))
    return out


class PcrMaxPain(StrictModel):
    expiry: date
    pcr_oi: float | None  # total put OI / total call OI
    pcr_volume: float | None
    pcr_oi_change: float | None  # put OI added / call OI added (only when both are additions)
    max_pain: float
    total_call_oi: int
    total_put_oi: int


def pcr_max_pain(chain: OptionChain) -> PcrMaxPain:
    calls = [r.call for r in chain.rows if r.call is not None]
    puts = [r.put for r in chain.rows if r.put is not None]
    c_oi, p_oi = sum(q.oi for q in calls), sum(q.oi for q in puts)
    c_vol, p_vol = sum(q.volume for q in calls), sum(q.volume for q in puts)
    c_chg, p_chg = sum(q.oi_change for q in calls), sum(q.oi_change for q in puts)

    def pain(settle: float) -> float:
        """Total payout option writers owe if the index settles at `settle`."""
        total = 0.0
        for r in chain.rows:
            if r.call is not None:
                total += r.call.oi * max(settle - r.strike, 0.0)
            if r.put is not None:
                total += r.put.oi * max(r.strike - settle, 0.0)
        return total

    max_pain = min((r.strike for r in chain.rows), key=lambda k: (pain(k), k))
    return PcrMaxPain(
        expiry=chain.expiry,
        pcr_oi=p_oi / c_oi if c_oi else None,
        pcr_volume=p_vol / c_vol if c_vol else None,
        pcr_oi_change=p_chg / c_chg if c_chg > 0 and p_chg > 0 else None,
        max_pain=max_pain,
        total_call_oi=c_oi,
        total_put_oi=p_oi,
    )
