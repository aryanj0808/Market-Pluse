"""End-of-day backtests on NSE F&O bhavcopy data (real history only).

Strategies:
  expected_range  Day t: ATM IV of the nearest expiry from settlement prices -> 1-session expected move
                  (spot x IV x sqrt(calendar days to the next session / 365)). Score: was the next close
                  inside +/-1 sigma and +/-2 sigma? (A fair IV gives ~68% / ~95%.)
  oi_levels       Day t: highest call-OI strike = resistance, highest put-OI strike = support (nearest
                  expiry). Score on day t+1: did the high stay below resistance, the low above support,
                  and the close inside [support, resistance]?

Proxies, stated in every result: the index's own high/low is not in the F&O bhavcopy, so the
front-month future's high/low stands in for it; the index close comes from UDiFF's underlying
price, so legacy-format days (before Jul 2024) are skipped and counted, never estimated.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import date

from mp_core import market_calendar as cal
from mp_core.analytics.black_scholes import implied_vol
from mp_core.analytics.expected_move import expected_move
from mp_core.loaders.bhavcopy import FoEodRecord
from mp_core.models import InstrumentKind, OptionType, StrictModel

STRATEGIES = ("expected_range", "oi_levels")


class DayData(StrictModel):
    day: date
    spot: float
    expiry: date
    atm_strike: float
    atm_iv: float | None
    call_oi_max_strike: float
    put_oi_max_strike: float
    fut_high: float | None
    fut_low: float | None


class BacktestResult(StrictModel):
    strategy: str
    underlying: str
    start: date
    end: date
    n_days: int  # scored day pairs
    n_skipped: int
    metrics: dict[str, float | None]
    notes: list[str]
    source: str = "nse_bhavcopy"


def daily_data(records: Sequence[FoEodRecord], underlying: str, rate: float) -> tuple[list[DayData], list[str]]:
    by_day: dict[date, list[FoEodRecord]] = defaultdict(list)
    for r in records:
        if r.key.underlying == underlying:
            by_day[r.trade_date].append(r)
    out, skipped = [], []
    for d in sorted(by_day):
        rows = by_day[d]
        opts = [r for r in rows if r.key.kind is InstrumentKind.OPTION and r.key.expiry >= d]
        spot = next((r.underlying_price for r in opts if r.underlying_price), None)
        if spot is None or not opts:
            skipped.append(f"{d}: no underlying price (legacy bhavcopy format) or no options")
            continue
        expiry = min(r.key.expiry for r in opts if r.key.expiry > d) if any(r.key.expiry > d for r in opts) else None
        if expiry is None:
            skipped.append(f"{d}: no future expiry")
            continue
        chain = [r for r in opts if r.key.expiry == expiry]
        calls = {r.key.strike: r for r in chain if r.key.option_type is OptionType.CE}
        puts = {r.key.strike: r for r in chain if r.key.option_type is OptionType.PE}
        both = sorted(set(calls) & set(puts))
        if not both:
            skipped.append(f"{d}: chain has no strike with both a call and a put")
            continue
        atm = min(both, key=lambda k: abs(k - spot))
        t = cal.years_to_expiry(cal.session_bounds(d)[1], expiry)
        ivs = []
        for ot, q in ((OptionType.CE, calls[atm]), (OptionType.PE, puts[atm])):
            px = q.settle or q.close
            if px > 0:
                r_iv = implied_vol(px, spot, atm, t, rate, ot)
                if r_iv.ok:
                    ivs.append(r_iv.iv)
        futs = sorted(
            (r for r in rows if r.key.kind is InstrumentKind.FUTURE and r.key.expiry >= d), key=lambda r: r.key.expiry
        )
        fut = futs[0] if futs else None
        out.append(
            DayData(
                day=d,
                spot=spot,
                expiry=expiry,
                atm_strike=atm,
                atm_iv=sum(ivs) / len(ivs) if ivs else None,
                call_oi_max_strike=max(calls.values(), key=lambda r: (r.oi, -r.key.strike)).key.strike,
                put_oi_max_strike=max(puts.values(), key=lambda r: (r.oi, r.key.strike)).key.strike,
                fut_high=fut.high if fut and fut.high > 0 else None,
                fut_low=fut.low if fut and fut.low > 0 else None,
            )
        )
    return out, skipped


def run_backtest(
    strategy: str, records: Sequence[FoEodRecord], underlying: str, start: date, end: date, rate: float = 0.065
) -> BacktestResult:
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown strategy {strategy!r}; choose from {STRATEGIES}")
    days, skipped = daily_data([r for r in records if start <= r.trade_date <= end], underlying, rate)
    pairs = list(zip(days, days[1:], strict=False))
    notes = [
        "index high/low proxied by the front-month future's high/low"
        if strategy == "oi_levels"
        else "IV = mean of ATM call and put implied vols from settlement prices, nearest expiry",
        f"{len(skipped)} day(s) skipped" + (f", e.g. {skipped[0]}" if skipped else ""),
    ]
    if strategy == "expected_range":
        scored = [(a, b) for a, b in pairs if a.atm_iv]
        in1 = in2 = 0
        for a, b in scored:
            em = expected_move(a.spot, a.atm_iv, (b.day - a.day).days)
            in1 += em.one_sigma.contains(b.spot)
            in2 += em.two_sigma.contains(b.spot)
        n = len(scored)
        metrics = {
            "containment_1s": in1 / n if n else None,
            "containment_2s": in2 / n if n else None,
            "expected_1s": 0.6827,
            "expected_2s": 0.9545,
            "avg_atm_iv": sum(a.atm_iv for a, _ in scored) / n if n else None,
        }
    else:
        scored = [(a, b) for a, b in pairs if b.fut_high and b.fut_low]
        res = sum(b.fut_high < a.call_oi_max_strike for a, b in scored)
        sup = sum(b.fut_low > a.put_oi_max_strike for a, b in scored)
        inside = sum(a.put_oi_max_strike <= b.spot <= a.call_oi_max_strike for a, b in scored)
        n = len(scored)
        metrics = {
            "resistance_held": res / n if n else None,
            "support_held": sup / n if n else None,
            "close_inside_range": inside / n if n else None,
            "avg_range_width_pct": sum((a.call_oi_max_strike - a.put_oi_max_strike) / a.spot * 100 for a, _ in scored)
            / n
            if n
            else None,
        }
    if n < 30:
        notes.append(f"only {n} scored days - treat rates as anecdotal")
    return BacktestResult(
        strategy=strategy,
        underlying=underlying,
        start=start,
        end=end,
        n_days=n,
        n_skipped=len(skipped),
        metrics=metrics,
        notes=notes,
    )
