"""Rule-based fusion of technical, options and news inputs into a signal - fully transparent.

    component scores (each in [-1, 1], each built from Reasons that cite their snapshot input)
      technical  0.35   futures vs session VWAP, spot vs CPR, spot vs prior-day range
      options    0.35   PCR (OI), futures OI build-up (last 3 bars), pull to max pain near expiry
      news       0.30   the deterministic news score
    composite  = sum(w*s) / sum(w) over available components
    bias       = bullish if composite >= +0.20, bearish if <= -0.20, else neutral
    confidence = min(1, |composite| / 0.6) x agreement x completeness
                 agreement: share of available weight whose sign matches the composite
                 completeness: available weight / total weight

    No signal (status="no_signal", zones still reported for context) when:
      - a critical input is stale or missing (quote, expected range, levels, front expiry)
      - components conflict (one >= +0.30 while another <= -0.30)
      - bias is neutral
      - risk rules fail (reward:risk < 1.2, or the stop does not fit the loss budget for 1 lot)

Zones/stops/targets come from levels judged by *where they are relative to price* (a pivot "S1"
above price acts as resistance) and from the expected range for the horizon.
"""

from __future__ import annotations

import hashlib
import math
from datetime import datetime

from mp_core import market_calendar as cal
from mp_core.analytics.levels import Level
from mp_core.analytics.oi import Buildup
from mp_core.registry import get_index
from mp_core.signals.models import ComponentScore, Reason, Signal, Sizing, Target, Zone
from mp_core.snapshot import MarketSnapshot

RULES_VERSION = "fusion-v1"
WEIGHTS = {"technical": 0.35, "options": 0.35, "news": 0.30}
BIAS_THRESHOLD = 0.20
CONFLICT_THRESHOLD = 0.30
FULL_CONFIDENCE_AT = 0.60
MIN_REWARD_RISK = 1.2
ZONE_HALF_WIDTH_SIGMA = 0.10  # zone = level +/- 0.1 sigma
STOP_BUFFER_SIGMA = 0.15  # stop sits this far beyond the zone (or the next level)
CRITICAL_SECTIONS = ("quote", "expected_range", "levels")
LEVEL_PRIORITY = {
    "oi.put_rank1": 0,
    "oi.call_rank1": 0,
    "vwap.session": 1,
    "pivot.cpr.TC": 2,
    "pivot.cpr.BC": 2,
    "prior_day.high": 2,
    "prior_day.low": 2,
    "pivot.classic.P": 3,
}


class RiskConfig:
    def __init__(self, capital: float = 500_000.0, max_risk_pct: float = 1.0) -> None:
        if capital <= 0 or not 0 < max_risk_pct <= 5:
            raise ValueError("capital must be > 0 and max_risk_pct in (0, 5]")
        self.capital = capital
        self.max_risk_pct = max_risk_pct

    @property
    def max_loss(self) -> float:
        return self.capital * self.max_risk_pct / 100.0


def _clip(x: float) -> float:
    return max(-1.0, min(1.0, x))


def _level(levels: list[Level], basis: str) -> Level | None:
    return next((lv for lv in levels if lv.basis == basis), None)


# ------------------------------------------------------------------ components
def _technical(s: MarketSnapshot) -> ComponentScore:
    reasons: list[Reason] = []
    if s.levels is None:
        return ComponentScore(
            name="technical",
            score=0,
            weight=0,
            available=False,
            reasons=[Reason(component="technical", input="levels", value=None, effect=0, text="levels unavailable")],
        )
    lv, spot = s.levels.value, s.quote.value.ltp
    vw, up = _level(lv, "vwap.session"), _level(lv, "vwap.upper_1sd")
    if vw is not None and up is not None and s.future is not None and up.price > vw.price:
        fut = s.future.value.ltp  # VWAP is computed on the futures, so compare like with like
        z = (fut - vw.price) / (up.price - vw.price)
        eff = _clip(z / 2) * 0.4
        reasons.append(
            Reason(
                component="technical",
                input="levels[vwap.session]",
                value=round(vw.price, 2),
                effect=eff,
                text=f"futures {fut:,.1f} is {z:+.2f} sd from session VWAP {vw.price:,.1f}",
            )
        )
    tc, bc = _level(lv, "pivot.cpr.TC"), _level(lv, "pivot.cpr.BC")
    if tc is not None and bc is not None:
        eff = 0.3 if spot > tc.price else -0.3 if spot < bc.price else 0.0
        where = "above" if eff > 0 else "below" if eff < 0 else "inside"
        reasons.append(
            Reason(
                component="technical",
                input="levels[pivot.cpr]",
                value=f"{bc.price:.1f}-{tc.price:.1f}",
                effect=eff,
                text=f"spot {spot:,.1f} is {where} the central pivot range",
            )
        )
    pdh, pdl = _level(lv, "prior_day.high"), _level(lv, "prior_day.low")
    if pdh is not None and pdl is not None:
        eff = 0.3 if spot > pdh.price else -0.3 if spot < pdl.price else 0.0
        where = (
            "above the prior-day high"
            if eff > 0
            else "below the prior-day low"
            if eff < 0
            else "inside the prior-day range"
        )
        reasons.append(
            Reason(
                component="technical",
                input="levels[prior_day]",
                value=f"{pdl.price:.1f}-{pdh.price:.1f}",
                effect=eff,
                text=f"spot is {where}",
            )
        )
    available = bool(reasons)
    return ComponentScore(
        name="technical",
        score=_clip(sum(r.effect for r in reasons)),
        weight=WEIGHTS["technical"] if available else 0,
        available=available,
        reasons=reasons,
    )


def _options(s: MarketSnapshot) -> ComponentScore:
    reasons: list[Reason] = []
    if s.expiries:
        front = s.expiries[0].value
        path = f"expiries[{front.expiry}]"
        pcr = front.pcr.pcr_oi
        if pcr is not None:
            eff = _clip((pcr - 1.0) / 0.4) * 0.4
            reading = "put writers dominate - supportive" if pcr > 1 else "call writers dominate - capping"
            reasons.append(
                Reason(
                    component="options",
                    input=f"{path}.pcr.pcr_oi",
                    value=round(pcr, 3),
                    effect=eff,
                    text=f"put-call OI ratio {pcr:.2f} ({reading})",
                )
            )
        if front.days_to_expiry <= 2 and s.expected_range is not None:
            sigma = (
                s.expected_range.value["session"].sigma_points or s.expected_range.value["next_session"].sigma_points
            )
            gap = front.pcr.max_pain - s.quote.value.ltp
            if sigma > 0:
                eff = _clip(gap / sigma) * 0.25
                reasons.append(
                    Reason(
                        component="options",
                        input=f"{path}.pcr.max_pain",
                        value=front.pcr.max_pain,
                        effect=eff,
                        text=f"{front.days_to_expiry:.1f} days to expiry: max pain "
                        f"{front.pcr.max_pain:,.0f} is {gap:+,.0f} pts from spot",
                    )
                )
    if s.future is not None and s.future.value.buildup_15m:
        recent = s.future.value.buildup_15m[-3:]
        b = sum(o.buildup.bias for o in recent) / 3
        if any(o.buildup is not Buildup.NEUTRAL for o in recent):
            names = ", ".join(o.buildup.value for o in recent)
            reasons.append(
                Reason(
                    component="options",
                    input="future.buildup_15m[-3:]",
                    value=names,
                    effect=b * 0.35,
                    text=f"futures OI build-up over the last {len(recent)} bars: {names}",
                )
            )
    available = bool(reasons)
    return ComponentScore(
        name="options",
        score=_clip(sum(r.effect for r in reasons)),
        weight=WEIGHTS["options"] if available else 0,
        available=available,
        reasons=reasons,
    )


def _news(s: MarketSnapshot) -> ComponentScore:
    if s.news is None:
        why = "no news poll"
    elif s.news.stale:
        why = "news feed stale"
    elif s.news.value.n_used == 0:
        why = "no relevant classified news in the window"
    else:
        n = s.news.value
        top = n.top[0] if n.top else None
        reasons = [
            Reason(
                component="news",
                input="news.score",
                value=round(n.score, 3),
                effect=n.score,
                text=f"news score {n.score:+.2f} ({n.label}) from {n.n_used} relevant items",
            )
        ]
        if top is not None:
            reasons.append(
                Reason(
                    component="news",
                    input="news.top[0]",
                    value=top.title[:120],
                    effect=0,
                    text=f'largest contributor: "{top.title[:90]}" ({top.contribution:+.2f})',
                )
            )
        return ComponentScore(
            name="news", score=_clip(n.score), weight=WEIGHTS["news"], available=True, reasons=reasons
        )
    return ComponentScore(
        name="news",
        score=0,
        weight=0,
        available=False,
        reasons=[Reason(component="news", input="news", value=None, effect=0, text=why)],
    )


# ------------------------------------------------------------------ zones / risk
def _pick(levels: list[Level], lo: float, hi: float, prefer_high: bool) -> Level | None:
    """Most important level inside [lo, hi]; ties -> closest to price."""
    cands = [lv for lv in levels if lo <= lv.price <= hi]
    if not cands:
        return None
    return min(cands, key=lambda lv: (LEVEL_PRIORITY.get(lv.basis, 5), -lv.price if prefer_high else lv.price))


def _zones(levels: list[Level], spot: float, sigma: float) -> tuple[Zone, Zone]:
    h = ZONE_HALF_WIDTH_SIGMA * sigma
    sup = _pick(levels, spot - sigma, spot - 0.05 * sigma, prefer_high=True)
    res = _pick(levels, spot + 0.05 * sigma, spot + sigma, prefer_high=False)
    buy = (
        Zone(low=sup.price - h, high=sup.price + h, basis=[sup.basis])
        if sup
        else Zone(low=spot - 0.6 * sigma, high=spot - 0.4 * sigma, basis=["expected_range"])
    )
    sell = (
        Zone(low=res.price - h, high=res.price + h, basis=[res.basis])
        if res
        else Zone(low=spot + 0.4 * sigma, high=spot + 0.6 * sigma, basis=["expected_range"])
    )
    return buy, sell


def _targets(levels: list[Level], entry: float, sigma: float, up: bool) -> list[Target]:
    beyond = sorted(
        (
            lv
            for lv in levels
            if (lv.price > entry + 0.25 * sigma if up else lv.price < entry - 0.25 * sigma)
            and abs(lv.price - entry) <= 2 * sigma
        ),
        key=lambda lv: abs(lv.price - entry),
    )
    out, seen = [], set()
    for lv in beyond:
        p = round(lv.price, 1)
        if p not in seen:
            out.append(Target(price=lv.price, basis=lv.basis))
            seen.add(p)
        if len(out) == 2:
            break
    if not out:
        out = [Target(price=entry + (1 if up else -1) * sigma, basis="expected_range.1sigma")]
    return out


# ------------------------------------------------------------------ main
def fuse(s: MarketSnapshot, risk: RiskConfig | None = None) -> Signal:
    risk = risk or RiskConfig()
    spot = s.quote.value.ltp
    blockers: list[str] = []
    reasons: list[Reason] = []

    stale_critical = [sec for sec in CRITICAL_SECTIONS if sec in s.stale_sections]
    if s.expiries and f"expiry:{s.expiries[0].value.expiry}" in s.stale_sections:
        stale_critical.append("front expiry")
    if stale_critical:
        blockers.append(f"stale inputs: {', '.join(stale_critical)}")
    for sec, val in (("expected_range", s.expected_range), ("levels", s.levels)):
        if val is None:
            blockers.append(f"missing input: {sec}")

    er = s.expected_range.value if s.expected_range else None
    horizon = "session" if er and er["session"].calendar_days >= 1 / 24 else "next_session"
    sigma = er[horizon].sigma_points if er else spot * 0.01  # only used for context when blocked

    comps = [_technical(s), _options(s), _news(s)]
    avail = [c for c in comps if c.available]
    total_w = sum(WEIGHTS.values())
    w = sum(c.weight for c in avail)
    composite = _clip(sum(c.weight * c.score for c in avail) / w) if w else 0.0
    bias = "bullish" if composite >= BIAS_THRESHOLD else "bearish" if composite <= -BIAS_THRESHOLD else "neutral"
    sign = (composite > 0) - (composite < 0)
    agree = sum(c.weight for c in avail if (c.score > 0) - (c.score < 0) == sign) / w if (w and sign) else 0.0
    confidence = round(min(1.0, abs(composite) / FULL_CONFIDENCE_AT) * agree * (w / total_w), 3)
    for c in comps:
        reasons += c.reasons
    if not avail:
        blockers.append("no component had usable inputs")
    if (
        max((c.score for c in avail), default=0) >= CONFLICT_THRESHOLD
        and min((c.score for c in avail), default=0) <= -CONFLICT_THRESHOLD
    ):
        ups = [c.name for c in avail if c.score >= CONFLICT_THRESHOLD]
        downs = [c.name for c in avail if c.score <= -CONFLICT_THRESHOLD]
        blockers.append(f"conflicting inputs: {', '.join(ups)} bullish vs {', '.join(downs)} bearish")
    if bias == "neutral":
        blockers.append(f"composite {composite:+.2f} is inside the neutral band (+/-{BIAS_THRESHOLD})")

    levels = s.levels.value if s.levels else []
    buy, sell = _zones(levels, spot, sigma) if levels else (None, None)
    entry = stop = invalidation = rr = None
    targets: list[Target] = []
    sizing = None
    if bias != "neutral" and buy is not None and sell is not None:
        up = bias == "bullish"
        zone = buy if up else sell
        entry = (zone.low + zone.high) / 2
        if up:  # stop below the next support under the zone (or a buffer below the zone)
            nxt = max(
                (lv.price for lv in levels if lv.price < zone.low and lv.price >= zone.low - 0.5 * sigma),
                default=zone.low,
            )
            stop = nxt - STOP_BUFFER_SIGMA * sigma
        else:
            nxt = min(
                (lv.price for lv in levels if lv.price > zone.high and lv.price <= zone.high + 0.5 * sigma),
                default=zone.high,
            )
            stop = nxt + STOP_BUFFER_SIGMA * sigma
        invalidation = nxt
        targets = _targets(levels, entry, sigma, up)
        risk_pts = abs(entry - stop)
        rr = abs(targets[0].price - entry) / risk_pts if risk_pts > 0 else None
        reasons.append(
            Reason(
                component="risk",
                input=zone.basis[0],
                value=round(entry, 1),
                effect=0,
                text=f"entry zone {zone.low:,.0f}-{zone.high:,.0f} from {zone.basis[0]}; stop "
                f"{stop:,.0f} beyond {invalidation:,.0f}; first target {targets[0].price:,.0f} "
                f"({targets[0].basis}); reward:risk {rr:.2f}"
                if rr
                else "invalid stop",
            )
        )
        if rr is None or rr < MIN_REWARD_RISK:
            blockers.append(f"reward:risk {rr or 0:.2f} below {MIN_REWARD_RISK}")
        if s.future is not None and risk_pts > 0:
            lot = get_index(s.symbol).lot_size
            per_lot = risk_pts * lot
            lots = math.floor(risk.max_loss / per_lot)
            sizing = Sizing(
                instrument=s.future.value.key.id,
                lot_size=lot,
                entry=entry,
                stop_loss=stop,
                risk_per_lot=per_lot,
                max_loss=risk.max_loss,
                lots=lots,
                loss_at_stop=lots * per_lot,
            )
            if lots < 1:
                blockers.append(
                    f"stop distance costs INR {per_lot:,.0f} per lot, above the INR {risk.max_loss:,.0f} loss budget"
                )

    status = "active" if not blockers else "no_signal"
    digest = hashlib.sha1(f"{s.symbol}|{s.computed_at.isoformat()}|{s.version}|{RULES_VERSION}".encode()).hexdigest()
    return Signal(
        signal_id=f"{digest[:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}",
        symbol=s.symbol,
        ts=s.computed_at,
        snapshot_version=s.version,
        source=s.source,
        status=status,
        bias=bias,
        composite=round(composite, 4),
        confidence=confidence if status == "active" else round(confidence / 2, 3),
        horizon=horizon,
        spot=spot,
        sigma_points=sigma,
        components=comps,
        buy_zone=buy,
        sell_zone=sell,
        entry=entry if status == "active" else None,
        stop_loss=stop if status == "active" else None,
        invalidation=invalidation if status == "active" else None,
        targets=targets if status == "active" else [],
        reward_risk=rr,
        sizing=sizing if status == "active" else None,
        reasons=reasons,
        no_signal_reasons=blockers,
        rules_version=RULES_VERSION,
    )


def horizon_end(signal: Signal) -> datetime:
    """The close the signal's horizon refers to - the same session `days_to_next_close` measures:
    the current session (or the next one if the market is shut), plus one more for next_session."""
    d = signal.ts.astimezone(cal.IST).date()
    if not cal.is_trading_day(d) or signal.ts >= cal.session_bounds(d)[1]:
        d = cal.next_trading_day(d)
    if signal.horizon == "next_session":
        d = cal.next_trading_day(d)
    return cal.session_bounds(d)[1]
