"""Deterministic synthetic market generator.

Purpose: exercise the analytics with realistic *shapes* - volatility regimes, overnight
gaps, intraday jumps, leverage effect (IV up when spot falls), volatility smile, IV crush
on expiry day and after event (gap) days, OI clustering at round strikes, futures roll.

Everything it writes is tagged `source="synthetic"`. It cannot tell you whether a signal
works; evaluation code refuses to score synthetic data.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import numpy as np

from mp_core import market_calendar as cal
from mp_core.analytics import black_scholes as bs
from mp_core.dataset import (
    ConstituentInfo,
    DayLabel,
    MacroRow,
    Manifest,
    write_candles,
    write_json,
    write_jsonl,
)
from mp_core.models import (
    Candle,
    InstrumentKey,
    Interval,
    OptionChain,
    OptionChainRow,
    OptionQuote,
    OptionType,
    Source,
    Tick,
)
from mp_core.registry import IndexSpec, get_index

GENERATOR_VERSION = "synth-v2"
STEP_SECONDS = 15
STEPS_PER_DAY = cal.SESSION_MINUTES * 60 // STEP_SECONDS  # 1500
STEPS_PER_MIN = 60 // STEP_SECONDS

DEFAULT_SPOT = {"BANKNIFTY": 56000.0, "NIFTY": 25500.0, "FINNIFTY": 26500.0}


@dataclass(frozen=True)
class SynthParams:
    regime_vol: dict[str, float] = field(default_factory=lambda: {"calm": 0.11, "normal": 0.16, "stressed": 0.30})
    # daily regime transition probabilities, rows = from, cols = to (calm, normal, stressed)
    transition: tuple[tuple[float, float, float], ...] = (
        (0.85, 0.13, 0.02),
        (0.10, 0.82, 0.08),
        (0.03, 0.22, 0.75),
    )
    gap_prob: float = 0.08
    gap_sd: float = 0.012
    gap_min: float = 0.006
    overnight_sd: float = 0.002
    jump_prob: float = 0.04
    jump_sd: float = 0.008
    vol_premium: float = 1.10  # implied over realized
    leverage: float = 4.0  # d ln(IV) / d ln(S)
    skew: float = 0.06  # IV change per standardized-moneyness unit (puts richer)
    smile: float = 0.025
    post_gap_iv_boost: float = 0.25
    event_boost_decay: float = 0.5  # per day after the event -> IV crush
    pre_expiry_iv: tuple[float, float] = (1.05, 1.08)  # multipliers 1 and 2 days before expiry
    expiry_crush_floor: float = 0.55  # front IV multiplier at expiry close
    strikes_each_side: int = 25
    chain_every_min: int = 5
    expiries_in_chain: int = 2
    oi_peak: float = 2_000_000.0
    oi_width_steps: float = 6.0
    risk_free_rate: float = 0.065


def _round_tick(x: float, tick: float = 0.05) -> float:
    return round(max(round(x / tick) * tick, tick), 2)


class SyntheticMarket:
    def __init__(
        self,
        symbol: str,
        start: date,
        n_days: int,
        seed: int,
        params: SynthParams | None = None,
        spot0: float | None = None,
    ) -> None:
        self.spec: IndexSpec = get_index(symbol)
        self.key = self.spec.key
        self.p = params or SynthParams()
        self.seed = seed
        self.rng = np.random.default_rng(seed)
        self.days = self._first_n_trading_days(start, n_days)
        self.spot0 = spot0 or DEFAULT_SPOT.get(self.spec.underlying, 20000.0)
        self._oi_noise: dict[tuple[int, int, int], float] = {}

    @staticmethod
    def _first_n_trading_days(start: date, n: int) -> list[date]:
        if n < 1:
            raise ValueError("n_days must be >= 1")
        out, d = [], start
        while len(out) < n:
            if cal.is_trading_day(d):
                out.append(d)
            d += timedelta(days=1)
        return out

    # ------------------------------------------------------------------ helpers
    def _intraday_profile(self) -> np.ndarray:
        """U-shaped variance profile (busy open & close), mean 1."""
        t = (np.arange(STEPS_PER_DAY) + 0.5) / STEPS_PER_DAY
        w = 1.0 + 1.2 * np.exp(-t / 0.08) + 0.5 * np.exp(-(1 - t) / 0.08)
        return w / w.mean()

    def _strike_noise(self, day_idx: int, strike: float, side: int) -> float:
        k = (day_idx, int(round(strike * 100)), side)
        if k not in self._oi_noise:
            r = np.random.default_rng([self.seed, day_idx + 1000, k[1], side])
            self._oi_noise[k] = float(np.exp(r.normal(0, 0.25)))
        return self._oi_noise[k]

    def _oi(
        self,
        strike: float,
        side: int,
        day_idx: int,
        anchor: float,
        spot: float,
        expiry: date,
        on: date,
        progress: float,
    ) -> int:
        """Deterministic OI for (strike, side) - re-computable for the previous day close."""
        step = self.spec.strike_step
        dte = (expiry - on).days
        life = 1.0 - 0.6 * min(dte, 30) / 30.0
        if dte == 0:
            life *= 1.0 - 0.5 * progress  # expiry-day unwinding
        width = self.p.oi_width_steps * step
        bell = math.exp(-(((strike - anchor) / width) ** 2))
        round_bonus = 1.6 if round(strike / step) % 5 == 0 else 1.0
        otm = strike >= spot if side == 0 else strike <= spot
        side_bias = 1.0 if otm else 0.45
        oi = self.p.oi_peak * life * (0.15 + bell) * round_bonus * side_bias
        return int(oi * self._strike_noise(day_idx, strike, side))

    def _smile_iv(self, atm_iv: float, strike: float, fwd: float, t: float) -> float:
        sd = max(atm_iv * math.sqrt(max(t, 1e-6)), 1e-4)
        m = max(min(math.log(strike / fwd) / sd, 3.0), -3.0)
        return max(atm_iv * (1.0 - self.p.skew * m + self.p.smile * m * m), 0.04)

    # ------------------------------------------------------------------ main
    def generate(self, out_dir: Path) -> Manifest:
        out_dir.mkdir(parents=True, exist_ok=True)
        p, rng = self.p, self.rng
        regimes = list(p.regime_vol)
        profile = self._intraday_profile()

        candles: list[Candle] = []
        ticks: list[Tick] = []
        chains: list[OptionChain] = []
        macro: list[MacroRow] = []
        labels: list[DayLabel] = []

        # Index = sum_i w_i * P_i / B_i * spot0: constituents drive the index, never the reverse.
        cons_specs = self.spec.constituents
        n_cons = len(cons_specs)
        w0 = np.array([c.weight for c in cons_specs]) / 100.0
        betas = np.array([c.beta for c in cons_specs])
        base_px = np.round(rng.uniform(200, 1800, n_cons), 2)
        cons_px = base_px.astype(float).copy()  # previous close
        cons_keys = [InstrumentKey(exchange=self.key.exchange, underlying=c.symbol, is_index=False) for c in cons_specs]
        idio_daily = 0.008
        idio_idx_var = float(np.sum(w0**2)) * idio_daily**2 * 252

        regime = "normal"
        prev_close = self.spot0
        prev_anchor = self.spot0
        event_boost = 0.0
        fut_oi: dict[date, float] = {}
        fut_rng = np.random.default_rng([self.seed, 7])
        usdinr, in10y = 88.0, 6.40
        fii_prev = float(rng.normal(-500, 2500))
        dii_prev = float(-0.6 * fii_prev + rng.normal(0, 1000))
        flows_as_of = datetime.combine(cal.prev_trading_day(self.days[0]), time(19, 0), cal.IST).astimezone(UTC)

        for di, day in enumerate(self.days):
            # --- regime & overnight
            if di > 0:
                regime = regimes[rng.choice(3, p=p.transition[regimes.index(regime)])]
            vol = p.regime_vol[regime]
            is_gap = di > 0 and rng.random() < p.gap_prob
            if is_gap:
                r_on = float(np.sign(rng.normal()) * max(abs(rng.normal(0, p.gap_sd)), p.gap_min))
            else:
                r_on = float(rng.normal(0, p.overnight_sd)) if di > 0 else 0.0

            prior_boost = event_boost
            event_boost = p.post_gap_iv_boost if is_gap else event_boost * p.event_boost_decay
            front_expiries = cal.upcoming_expiries(self.spec, day, p.expiries_in_chain)
            is_expiry = front_expiries[0] == day
            iv_crush = is_expiry or (prior_boost >= 0.2 and not is_gap)

            # --- intraday path (15s steps): common factor + per-constituent idiosyncratic noise
            jump = float(rng.normal(0, p.jump_sd)) if rng.random() < p.jump_prob else 0.0
            jump_at = int(rng.integers(30, STEPS_PER_DAY - 30))
            if n_cons:
                w_now = w0 * cons_px / base_px
                w_now /= w_now.sum()
                wbeta = float(w_now @ betas)
                factor_vol = math.sqrt(max(vol**2 - idio_idx_var, (0.5 * vol) ** 2)) / wbeta
                f_sd = factor_vol / math.sqrt(252) * np.sqrt(profile / STEPS_PER_DAY)
                f_rets = rng.normal(0, 1, STEPS_PER_DAY) * f_sd
                f_rets[jump_at] += jump / wbeta
                on_idio = rng.normal(0, 0.0015, n_cons) if di > 0 else np.zeros(n_cons)
                common_on = r_on / wbeta

                def _index_gap(c: float, w: np.ndarray = w_now, eps: np.ndarray = on_idio) -> float:
                    return math.log(float(w @ np.exp(betas * c + eps)))

                if is_gap and abs(_index_gap(common_on)) < p.gap_min:  # keep labelled gaps real gaps
                    common_on *= 1.02 * p.gap_min / abs(_index_gap(common_on))
                c_open = cons_px * np.exp(betas * common_on + on_idio)
                idio = rng.normal(0, 1, (n_cons, STEPS_PER_DAY)) * idio_daily * np.sqrt(profile / STEPS_PER_DAY)
                c_rets = betas[:, None] * f_rets[None, :] + idio
                c_paths = c_open[:, None] * np.exp(np.concatenate([np.zeros((n_cons, 1)), np.cumsum(c_rets, 1)], 1))
                path = self.spot0 * ((w0 / base_px) @ c_paths)
                r_on = math.log(path[0] / prev_close) if di > 0 else 0.0
            else:
                sd_step = vol / math.sqrt(252) * np.sqrt(profile / STEPS_PER_DAY)
                f_rets = rng.normal(0, 1, STEPS_PER_DAY) * sd_step
                f_rets[jump_at] += jump
                path = prev_close * math.exp(r_on) * np.exp(np.concatenate([[0.0], np.cumsum(f_rets)]))
            day_open = float(path[0])
            rets = np.diff(np.log(path))
            realized = float(np.std(rets) * math.sqrt(STEPS_PER_DAY * 252))

            base_iv = vol * p.vol_premium * (1 + event_boost) * math.exp(rng.normal(0, 0.05))
            atm_iv_path = np.clip(base_iv * np.exp(-p.leverage * np.log(path / day_open)), 0.05, 1.5)

            sess_open, _ = cal.session_bounds(day)

            # --- index candles + ticks
            candles += self._minute_candles(self.key, path, sess_open, None, None)
            ticks += [
                Tick(key=self.key, ts=sess_open + timedelta(seconds=STEP_SECONDS * k), ltp=round(float(path[k]), 2))
                for k in range(STEPS_PER_DAY + 1)
            ]

            # --- futures: front and next month trade side by side; OI rolls from one to the other.
            # The front contract draws from the main stream (keeps seeds stable); the next month
            # has its own stream.
            front_exp = cal.front_future_expiry(self.spec, day)
            next_exp = cal.front_future_expiry(self.spec, front_exp + timedelta(days=1))
            front_dte = (front_exp - day).days
            for fut_expiry, frng in ((front_exp, rng), (next_exp, fut_rng)):
                is_front = fut_expiry == front_exp
                oi_now = fut_oi.setdefault(fut_expiry, (0.6 if is_front else 0.12) * p.oi_peak * 5)
                fut_key = InstrumentKey(exchange=self.key.exchange, underlying=self.key.underlying, expiry=fut_expiry)
                hour_drift = frng.choice([-1.0, 1.0], size=7)
                fut_path = np.empty_like(path)
                fut_oi_path = np.empty(len(path), dtype=np.int64)
                fut_vol = np.empty(len(path), dtype=np.int64)
                cum_vol = 0
                for k in range(len(path)):
                    ts_k = sess_open + timedelta(seconds=STEP_SECONDS * k)
                    t_k = cal.years_to_expiry(ts_k, fut_expiry)
                    fut_path[k] = path[k] * math.exp(p.risk_free_rate * t_k) * (1 + frng.normal(0, 0.00005))
                    if is_front:
                        drift = hour_drift[min(k // 240, 6)] * 120.0 - (900.0 if front_dte <= 2 else 0.0)
                    else:  # rollover: the next month gains what the front loses
                        drift = hour_drift[min(k // 240, 6)] * 40.0 + (800.0 if front_dte <= 2 else 25.0)
                    oi_now = max(oi_now + drift + frng.normal(0, 600.0 if is_front else 250.0), 10_000.0)
                    fut_oi_path[k] = int(oi_now)
                    cum_vol += int(abs(frng.normal(0, 800 if is_front else 150)) * profile[min(k, STEPS_PER_DAY - 1)])
                    fut_vol[k] = cum_vol
                fut_oi[fut_expiry] = oi_now
                candles += self._minute_candles(fut_key, fut_path, sess_open, fut_vol, fut_oi_path)
                ticks += [
                    Tick(
                        key=fut_key,
                        ts=sess_open + timedelta(seconds=STEP_SECONDS * k),
                        ltp=round(float(fut_path[k]), 2),
                        volume=int(fut_vol[k]),
                        oi=int(fut_oi_path[k]),
                    )
                    for k in range(len(path))
                ]

            # --- constituents (1m candles only)
            for i in range(n_cons):
                c_vol = np.cumsum(np.abs(rng.normal(0, 3000, STEPS_PER_DAY + 1)).astype(np.int64))
                candles += self._minute_candles(cons_keys[i], c_paths[i], sess_open, c_vol, None)
            if n_cons:
                cons_px = c_paths[:, -1].copy()

            # --- option chains every N minutes
            anchor_today = 0.5 * (prev_anchor + day_open)
            for m in range(p.chain_every_min, cal.SESSION_MINUTES + 1, p.chain_every_min):
                k = m * STEPS_PER_MIN
                ts = sess_open + timedelta(minutes=m)
                spot = float(path[k])
                progress = m / cal.SESSION_MINUTES
                anchor = anchor_today + (spot - anchor_today) * 0.5 * progress
                for ei, exp in enumerate(front_expiries):
                    dte = (exp - day).days
                    mult = 1.0
                    if dte == 0:
                        mult = 1.0 - (1.0 - p.expiry_crush_floor) * progress
                    elif dte in (1, 2):
                        mult = p.pre_expiry_iv[dte - 1]
                    mult *= 1.0 - 0.03 * ei  # mild downward term structure
                    chains.append(
                        self._chain(day, di, ts, spot, float(atm_iv_path[k]) * mult, exp, anchor, prev_anchor, progress)
                    )

            # --- macro, every 5 minutes; FII/DII published 19:00 IST
            for m in range(0, cal.SESSION_MINUTES + 1, 5):
                k = m * STEPS_PER_MIN
                usdinr *= math.exp(rng.normal(0, 0.003 / math.sqrt(76)))
                in10y += float(rng.normal(0, 0.02 / math.sqrt(76)))
                macro.append(
                    MacroRow(
                        ts=sess_open + timedelta(minutes=m),
                        india_vix=round(float(atm_iv_path[k]) * 0.80 * 100 + rng.normal(0, 0.15), 2),
                        usdinr=round(usdinr, 4),
                        in10y_yield=round(in10y, 3),
                        fii_net_cr=round(fii_prev, 1),
                        dii_net_cr=round(dii_prev, 1),
                        flows_as_of=flows_as_of,
                    )
                )
            day_ret = math.log(path[-1] / day_open)
            fii_prev = float(rng.normal(-300 + 40000 * day_ret, 2500))
            dii_prev = float(-0.6 * fii_prev + rng.normal(0, 1000))
            flows_as_of = datetime.combine(day, time(19, 0), cal.IST).astimezone(UTC)

            labels.append(
                DayLabel(
                    day=day,
                    regime=regime,
                    realized_vol_annual=round(realized, 4),
                    gap_pct=round(100 * r_on, 3),
                    is_gap_day=is_gap,
                    jump_pct=round(100 * jump, 3),
                    is_expiry_day=is_expiry,
                    iv_crush=iv_crush,
                    atm_iv_open=round(float(atm_iv_path[0]), 4),
                    atm_iv_close=round(float(atm_iv_path[-1]), 4),
                )
            )
            prev_close = float(path[-1])
            prev_anchor = anchor_today + (prev_close - anchor_today) * 0.5

        manifest = Manifest(
            source=Source.SYNTHETIC,
            symbol=self.key.symbol,
            days=self.days,
            constituents=[
                ConstituentInfo(symbol=c.symbol, name=c.name, weight=c.weight, base_price=float(b))
                for c, b in zip(cons_specs, base_px, strict=True)
            ],
            seed=self.seed,
            generator=GENERATOR_VERSION,
            cadence_seconds={"tick": STEP_SECONDS, "option_chain": 60.0 * p.chain_every_min, "macro": 300.0},
            notes="Synthetic data for development. Prices, OI, IV, weights and flows are not real.",
        )
        candles.sort(key=lambda c: (c.ts, c.key.id))
        ticks.sort(key=lambda t: (t.ts, t.key.id))
        write_candles(out_dir / "candles_1m.csv", candles)
        write_jsonl(out_dir / "ticks.jsonl.gz", ticks)
        write_jsonl(out_dir / "chains.jsonl.gz", chains)
        write_jsonl(out_dir / "macro.jsonl", macro)
        write_json(out_dir / "events.json", labels)
        write_json(out_dir / "manifest.json", manifest)
        return manifest

    def _minute_candles(
        self,
        key: InstrumentKey,
        path: np.ndarray,
        sess_open: datetime,
        cum_volume: np.ndarray | None,
        oi: np.ndarray | None,
    ) -> list[Candle]:
        out = []
        for j in range(cal.SESSION_MINUTES):
            seg = path[j * STEPS_PER_MIN : (j + 1) * STEPS_PER_MIN + 1]
            vol = 0
            if cum_volume is not None:
                vol = int(cum_volume[(j + 1) * STEPS_PER_MIN] - cum_volume[j * STEPS_PER_MIN])
            out.append(
                Candle(
                    key=key,
                    interval=Interval.M1,
                    ts=sess_open + timedelta(minutes=j),
                    open=round(float(seg[0]), 2),
                    high=round(float(seg.max()), 2),
                    low=round(float(seg.min()), 2),
                    close=round(float(seg[-1]), 2),
                    volume=vol,
                    oi=None if oi is None else int(oi[(j + 1) * STEPS_PER_MIN]),
                )
            )
        return out

    def _chain(
        self,
        day: date,
        di: int,
        ts: datetime,
        spot: float,
        atm_iv: float,
        expiry: date,
        anchor: float,
        prev_anchor: float,
        progress: float,
    ) -> OptionChain:
        p, step = self.p, self.spec.strike_step
        t = cal.years_to_expiry(ts, expiry)
        fwd = spot * math.exp(p.risk_free_rate * t)
        atm = round(spot / step) * step
        prev_day = cal.prev_trading_day(day)
        rows = []
        for i in range(-p.strikes_each_side, p.strikes_each_side + 1):
            strike = float(atm + i * step)
            quotes = {}
            for side, ot in ((0, OptionType.CE), (1, OptionType.PE)):
                iv = self._smile_iv(atm_iv, strike, fwd, t)
                theo = bs.price(spot, strike, t, p.risk_free_rate, iv, ot)
                spread = max(0.05, 0.004 * theo)
                ltp = _round_tick(theo)
                oi = self._oi(strike, side, di, anchor, spot, expiry, day, progress)
                oi_prev = (
                    self._oi(strike, side, di - 1, prev_anchor, prev_anchor, expiry, prev_day, 1.0)
                    if expiry >= prev_day
                    else 0
                )
                closeness = math.exp(-(((strike - spot) / (p.oi_width_steps * step)) ** 2))
                volume = int(oi * 3.0 * progress * (0.1 + closeness) * self._strike_noise(di, strike, side + 2))
                quotes[ot] = OptionQuote(
                    key=InstrumentKey(
                        exchange=self.key.exchange,
                        underlying=self.key.underlying,
                        expiry=expiry,
                        strike=strike,
                        option_type=ot,
                    ),
                    ltp=ltp,
                    bid=_round_tick(max(theo - spread / 2, 0.05)),
                    ask=_round_tick(theo + spread / 2),
                    volume=volume,
                    oi=oi,
                    oi_change=oi - oi_prev,
                )
            rows.append(OptionChainRow(strike=strike, call=quotes[OptionType.CE], put=quotes[OptionType.PE]))
        return OptionChain(
            underlying=self.key, expiry=expiry, ts=ts, spot=round(spot, 2), future=round(fwd, 2), rows=rows
        )


def generate(
    symbol: str, out_dir: Path, *, start: date, days: int, seed: int, params: SynthParams | None = None
) -> Manifest:
    return SyntheticMarket(symbol, start, days, seed, params).generate(out_dir)
