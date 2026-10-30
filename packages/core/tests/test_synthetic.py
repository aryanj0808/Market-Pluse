"""The generator must actually produce the behaviours the analytics will be tested on."""

import hashlib
import json
import math
from datetime import date

import numpy as np
import pytest

from mp_core import market_calendar as cal
from mp_core.analytics.black_scholes import price
from mp_core.dataset import DayLabel, iter_jsonl, read_candles, read_manifest
from mp_core.models import OptionChain, OptionType, Source
from mp_core.synthetic.generator import SynthParams, generate

R = SynthParams().risk_free_rate


def implied_vol(px: float, s: float, k: float, t: float, ot: OptionType) -> float:
    lo, hi = 1e-4, 3.0
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        lo, hi = (mid, hi) if price(s, k, t, R, mid, ot) < px else (lo, mid)
    return 0.5 * (lo + hi)


def atm_iv(ch: OptionChain) -> float:
    row = min(ch.rows, key=lambda r: abs(r.strike - ch.spot))
    t = cal.years_to_expiry(ch.ts, ch.expiry)
    mid = 0.5 * (row.call.bid + row.call.ask)
    return implied_vol(mid, ch.spot, row.strike, t, OptionType.CE)


@pytest.fixture(scope="module")
def labels(synth_dir) -> list[DayLabel]:
    return [DayLabel.model_validate(x) for x in json.loads((synth_dir / "events.json").read_text())]


@pytest.fixture(scope="module")
def chains(synth_dir) -> list[OptionChain]:
    return [OptionChain.model_validate(r) for r in iter_jsonl(synth_dir / "chains.jsonl.gz")]


def test_deterministic_for_same_seed(tmp_path):
    def digest(d):
        return {
            f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted(d.iterdir()) if f.suffix != ".gz"
        }  # gzip headers embed mtime; compare plain files

    a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    for d, seed in ((a, 1), (b, 1), (c, 2)):
        generate("NSE:NIFTY", d, start=date(2026, 9, 1), days=2, seed=seed)
    assert digest(a) == digest(b)
    assert digest(a) != digest(c)


def test_tagged_synthetic(synth_dir):
    m = read_manifest(synth_dir)
    assert m.source is Source.SYNTHETIC and m.seed == 2 and len(m.days) == 25


@pytest.mark.parametrize("symbol", ["NSE:NIFTY", "NSE:FINNIFTY"])
def test_other_indices_generate(tmp_path, symbol):
    m = generate(symbol, tmp_path, start=date(2026, 9, 1), days=1, seed=3)
    ch = OptionChain.model_validate(next(iter_jsonl(tmp_path / "chains.jsonl.gz")))
    assert m.symbol == symbol and ch.underlying.symbol == symbol


def test_volatility_regimes(labels):
    by_regime: dict[str, list[float]] = {}
    for lb in labels:
        by_regime.setdefault(lb.regime, []).append(lb.realized_vol_annual)
    assert len(by_regime) >= 2
    means = {k: np.mean(v) for k, v in by_regime.items()}
    order = [r for r in ("calm", "normal", "stressed") if r in means]
    assert [means[r] for r in order] == sorted(means[r] for r in order)
    assert means.get("stressed", 1) > 1.5 * means["normal"]


def test_gap_days(labels):
    gaps = [lb for lb in labels if lb.is_gap_day]
    assert gaps, "seed should produce at least one gap day"
    assert all(abs(lb.gap_pct) >= 0.6 for lb in gaps)
    assert max(abs(lb.gap_pct) for lb in labels if not lb.is_gap_day) < 0.6 * 1.5


def test_gap_visible_in_candles(synth_dir, labels):
    candles = [c for c in read_candles(synth_dir / "candles_1m.csv", "BANKNIFTY") if c.key.id == "NSE:BANKNIFTY"]
    by_day: dict[date, list] = {}
    for c in candles:
        by_day.setdefault(c.ts.astimezone(cal.IST).date(), []).append(c)
    gap = next(lb for lb in labels if lb.is_gap_day)
    prev = cal.prev_trading_day(gap.day)
    observed = 100 * math.log(by_day[gap.day][0].open / by_day[prev][-1].close)
    assert observed == pytest.approx(gap.gap_pct, abs=0.01)


def test_iv_crush_on_expiry_day(chains, labels):
    exp_day = next(lb.day for lb in labels if lb.is_expiry_day)
    front = [c for c in chains if c.expiry == exp_day and c.ts.astimezone(cal.IST).date() == exp_day]
    early, late = atm_iv(front[0]), atm_iv(front[-2])  # last snapshot is at expiry (t=0)
    assert late < 0.75 * early, f"expected crush, got {early:.3f} -> {late:.3f}"


def test_iv_elevated_before_expiry_vs_next_month(chains, labels):
    exp_day = next(lb.day for lb in labels if lb.is_expiry_day)
    day_before = cal.prev_trading_day(exp_day)
    snaps = [c for c in chains if c.ts.astimezone(cal.IST).date() == day_before]
    ts = snaps[0].ts
    front = next(c for c in snaps if c.ts == ts and c.expiry == exp_day)
    back = next(c for c in snaps if c.ts == ts and c.expiry > exp_day)
    assert atm_iv(front) > atm_iv(back)


def test_leverage_effect(chains):
    front = sorted((c for c in chains if (c.expiry - c.ts.date()).days > 3), key=lambda c: (c.expiry, c.ts))
    pairs = [
        (a, b) for a, b in zip(front, front[1:], strict=False) if a.expiry == b.expiry and a.ts.date() == b.ts.date()
    ]
    d_spot = [math.log(b.spot / a.spot) for a, b in pairs[:300]]
    d_iv = [math.log(atm_iv(b) / atm_iv(a)) for a, b in pairs[:300]]
    assert np.corrcoef(d_spot, d_iv)[0, 1] < -0.5


def test_put_skew(chains):
    ch = next(c for c in chains if (c.expiry - c.ts.date()).days > 10)
    t = cal.years_to_expiry(ch.ts, ch.expiry)
    otm_put = ch.rows[2]  # well below spot
    atm = min(ch.rows, key=lambda r: abs(r.strike - ch.spot))
    iv_put = implied_vol(0.5 * (otm_put.put.bid + otm_put.put.ask), ch.spot, otm_put.strike, t, OptionType.PE)
    iv_atm = implied_vol(0.5 * (atm.put.bid + atm.put.ask), ch.spot, atm.strike, t, OptionType.PE)
    assert iv_put > iv_atm


def test_chain_no_arbitrage_parity(chains):
    for ch in chains[::97]:
        t = cal.years_to_expiry(ch.ts, ch.expiry)
        for row in ch.rows:
            parity = ch.spot - row.strike * math.exp(-R * t)
            mid_c, mid_p = 0.5 * (row.call.bid + row.call.ask), 0.5 * (row.put.bid + row.put.ask)
            assert mid_c - mid_p == pytest.approx(parity, abs=max(0.2, 0.002 * abs(parity)) + 0.1)


def test_oi_clusters_at_round_strikes(chains):
    step = 100
    hits = 0
    sample = [c for c in chains if (c.expiry - c.ts.date()).days > 5][::50]
    for ch in sample:
        top_call = max(ch.rows, key=lambda r: r.call.oi).strike
        top_put = max(ch.rows, key=lambda r: r.put.oi).strike
        hits += (round(top_call / step) % 5 == 0) + (round(top_put / step) % 5 == 0)
    assert hits / (2 * len(sample)) > 0.6


def _futures_by_key(synth_dir):
    out: dict[str, list] = {}
    for c in read_candles(synth_dir / "candles_1m.csv", "BANKNIFTY"):
        if c.key.underlying == "BANKNIFTY" and c.key.expiry is not None:
            out.setdefault(c.key.id, []).append(c)
    return out


def test_futures_oi_produces_all_buildup_types(synth_dir):
    front = _futures_by_key(synth_dir)["NSE:BANKNIFTY:2026-09-29"]
    seen = set()
    for a, b in zip(front[::15], front[15::15], strict=False):
        if a.ts.date() == b.ts.date():
            seen.add((b.close > a.close, b.oi > a.oi))
    assert seen == {(True, True), (True, False), (False, True), (False, False)}


def test_front_and_next_month_trade_and_oi_rolls(synth_dir):
    keys_by_day: dict[date, set[str]] = {}
    for k, bars in _futures_by_key(synth_dir).items():
        for c in bars:
            keys_by_day.setdefault(c.ts.astimezone(cal.IST).date(), set()).add(k)
    assert keys_by_day[date(2026, 9, 29)] == {"NSE:BANKNIFTY:2026-09-29", "NSE:BANKNIFTY:2026-10-27"}
    assert keys_by_day[date(2026, 9, 30)] == {"NSE:BANKNIFTY:2026-10-27", "NSE:BANKNIFTY:2026-11-24"}
    fut = _futures_by_key(synth_dir)

    def oi_at_close(key, d):
        return [c.oi for c in fut[key] if c.ts.astimezone(cal.IST).date() == d][-1]

    sep, octo = "NSE:BANKNIFTY:2026-09-29", "NSE:BANKNIFTY:2026-10-27"
    before, on = date(2026, 9, 25), date(2026, 9, 29)
    assert oi_at_close(sep, on) < oi_at_close(sep, before)  # front unwinds into expiry
    assert oi_at_close(octo, on) > oi_at_close(octo, before)  # next month picks it up
