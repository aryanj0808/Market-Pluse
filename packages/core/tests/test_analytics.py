"""Expected move, levels, OI build-up, option-chain analytics - on hand-built inputs (exact
answers) and on the synthetic dataset (the analytics must recover what the generator did)."""

import math
from datetime import UTC, date, datetime, timedelta

import pytest

from mp_core import market_calendar as cal
from mp_core.analytics.black_scholes import price
from mp_core.analytics.expected_move import calendar_days_until, days_to_next_close, expected_move
from mp_core.analytics.levels import (
    LevelKind,
    PriorDay,
    compute_levels,
    nearest,
    oi_levels,
    pivots,
    prior_day,
    vwap,
)
from mp_core.analytics.oi import Buildup, chain_buildup, classify_buildup, futures_buildup
from mp_core.analytics.options import iv_summary, pcr_max_pain, term_structure
from mp_core.clock import SimClock
from mp_core.dataset import DayLabel
from mp_core.models import (
    Candle,
    InstrumentKey,
    Interval,
    OptionChain,
    OptionChainRow,
    OptionQuote,
    OptionType,
)
from mp_core.providers.replay import ReplayProvider

from conftest import ist

BN = InstrumentKey.parse("NSE:BANKNIFTY")
EXP = date(2026, 10, 27)
T0 = datetime(2026, 10, 20, 5, 0, tzinfo=UTC)
R = 0.065
WIDE = [50000.0 + 100 * i for i in range(41)]  # +/-2000 points: wide enough for 25-delta strikes


def bar(ts, o, h, lo, c, v=0, oi=None, key=BN, interval=Interval.M1):
    return Candle(key=key, interval=interval, ts=ts, open=o, high=h, low=lo, close=c, volume=v, oi=oi)


def make_chain(spot, ts=T0, vol=lambda k: 0.18, oi=None, expiry=EXP, strikes=None, oi_change=None) -> OptionChain:
    strikes = strikes or [spot - 300 + 100 * i for i in range(7)]
    t = cal.years_to_expiry(ts, expiry)
    rows = []
    for k in strikes:
        qs = {}
        for ot in (OptionType.CE, OptionType.PE):
            px = price(spot, k, t, R, vol(k), ot)
            o = oi(k, ot) if oi else 1000
            qs[ot] = OptionQuote(
                key=InstrumentKey(exchange="NSE", underlying="BANKNIFTY", expiry=expiry, strike=k, option_type=ot),
                ltp=px,
                bid=px,
                ask=px,
                volume=10,
                oi=o,
                oi_change=oi_change(k, ot) if oi_change else 0,
            )
        rows.append(OptionChainRow(strike=k, call=qs[OptionType.CE], put=qs[OptionType.PE]))
    return OptionChain(underlying=BN, expiry=expiry, ts=ts, spot=spot, future=spot * math.exp(R * t), rows=rows)


# ---------------------------------------------------------------- expected move
class TestExpectedMove:
    def test_formula(self):
        em = expected_move(52000, 0.16, 7)
        sigma = 52000 * 0.16 * math.sqrt(7 / 365)
        assert em.sigma_points == pytest.approx(sigma)
        assert (em.one_sigma.lower, em.one_sigma.upper) == pytest.approx((52000 - sigma, 52000 + sigma))
        assert (em.two_sigma.lower, em.two_sigma.upper) == pytest.approx((52000 - 2 * sigma, 52000 + 2 * sigma))
        assert em.sigma_pct == pytest.approx(100 * 0.16 * math.sqrt(7 / 365))

    def test_one_year_one_sigma_is_iv(self):
        assert expected_move(100, 0.2, 365).sigma_points == pytest.approx(20)

    def test_zero_days(self):
        em = expected_move(52000, 0.16, 0)
        assert em.sigma_points == 0 and em.one_sigma.contains(52000)

    @pytest.mark.parametrize("spot, iv, days", [(0, 0.2, 1), (52000, 0, 1), (52000, 16, 1), (52000, 0.2, -1)])
    def test_rejects_bad_inputs(self, spot, iv, days):
        with pytest.raises(ValueError):
            expected_move(spot, iv, days)

    def test_days_helpers(self):
        fri_close = ist(date(2026, 10, 23), 15, 30)
        assert calendar_days_until(fri_close, fri_close + timedelta(days=3)) == pytest.approx(3)
        # Friday after close -> Monday's close is 3 calendar days away
        assert days_to_next_close(fri_close) == pytest.approx(3)
        # during Monday's session, the current session is the first
        assert days_to_next_close(ist(date(2026, 10, 26), 9, 30)) == pytest.approx(6 / 24)
        assert days_to_next_close(ist(date(2026, 10, 26), 9, 30), sessions=2) == pytest.approx(1 + 6 / 24)


# ---------------------------------------------------------------- levels
class TestLevels:
    def test_pivots_hand_computed(self):
        pv = pivots(high=52500, low=51500, close=52200)
        p = (52500 + 51500 + 52200) / 3
        assert pv.p == pytest.approx(p)
        assert (pv.r1, pv.s1) == pytest.approx((2 * p - 51500, 2 * p - 52500))
        assert (pv.r2, pv.s2) == pytest.approx((p + 1000, p - 1000))
        assert (pv.r3, pv.s3) == pytest.approx((52500 + 2 * (p - 51500), 51500 - 2 * (52500 - p)))
        assert pv.cpr_bc == pytest.approx(52000) and pv.cpr_tc == pytest.approx(2 * p - 52000)
        assert pv.s3 < pv.s2 < pv.s1 < pv.p < pv.r1 < pv.r2 < pv.r3

    def test_cpr_ordered_when_close_below_mid(self):
        pv = pivots(high=52500, low=51500, close=51600)
        assert pv.cpr_bc < pv.cpr_tc and pv.cpr_width_pct > 0

    def test_pivots_reject_inconsistent(self):
        with pytest.raises(ValueError):
            pivots(high=100, low=90, close=101)

    def test_vwap_hand_computed(self):
        t = ist(date(2026, 10, 20), 9, 15)
        bars = [bar(t, 100, 102, 98, 100, v=100), bar(t + timedelta(minutes=1), 100, 106, 100, 103, v=300)]
        v = vwap(bars)
        tp1, tp2 = 100.0, (106 + 100 + 103) / 3
        expected = (tp1 * 100 + tp2 * 300) / 400
        assert v.vwap == pytest.approx(expected)
        assert v.sd == pytest.approx(math.sqrt((100 * (tp1 - expected) ** 2 + 300 * (tp2 - expected) ** 2) / 400))
        assert v.lower_1sd < v.vwap < v.upper_1sd and v.volume == 400

    def test_vwap_refuses_index_candles_and_mixed_sessions(self):
        t = ist(date(2026, 10, 20), 9, 15)
        with pytest.raises(ValueError, match="futures candles"):
            vwap([bar(t, 100, 101, 99, 100)])
        with pytest.raises(ValueError, match="single session"):
            vwap([bar(t, 100, 101, 99, 100, v=1), bar(t + timedelta(days=1), 100, 101, 99, 100, v=1)])

    def test_prior_day(self):
        d = [
            bar(cal.session_bounds(x)[0], 100, 110 + i, 90, 105, interval=Interval.D1)
            for i, x in enumerate([date(2026, 10, 19), date(2026, 10, 20), date(2026, 10, 21)])
        ]
        pd = prior_day(d, date(2026, 10, 21))
        assert pd.day == date(2026, 10, 20) and pd.high == 111
        with pytest.raises(ValueError):
            prior_day(d, date(2026, 10, 19))
        with pytest.raises(ValueError, match="daily"):
            prior_day([bar(T0, 1, 1, 1, 1)], date(2026, 10, 21))

    def test_oi_levels(self):
        call_oi = {52000: 500, 52300: 9000, 52200: 4000}
        put_oi = {51800: 7000, 51700: 3000}
        ch = make_chain(
            52000,
            oi=lambda k, ot: (call_oi if ot is OptionType.CE else put_oi).get(k, 100),
            oi_change=lambda k, ot: 800 if (k, ot) == (52200, OptionType.CE) else 0,
        )
        lv = {x.basis: x for x in oi_levels(ch)}
        assert lv["oi.call_rank1"].price == 52300 and lv["oi.call_rank1"].kind is LevelKind.RESISTANCE
        assert lv["oi.call_rank2"].price == 52200
        assert lv["oi.put_rank1"].price == 51800 and lv["oi.put_rank1"].kind is LevelKind.SUPPORT
        assert lv["oi.call_max_add"].price == 52200
        assert "oi.put_max_add" not in lv  # no put OI was added

    def test_compute_levels_sorted_with_bases(self):
        lv = compute_levels(
            PriorDay(day=date(2026, 10, 19), high=52500, low=51500, close=52200), chain=make_chain(52000)
        )
        assert [x.price for x in lv] == sorted(x.price for x in lv)
        assert len({x.basis for x in lv}) == len(lv)
        below, above = nearest(lv, 52100)
        assert below.price < 52100 < above.price


# ---------------------------------------------------------------- OI build-up
class TestBuildup:
    @pytest.mark.parametrize(
        "dp, doi, expected",
        [
            (0.5, 2, Buildup.LONG_BUILDUP),
            (-0.5, 2, Buildup.SHORT_BUILDUP),
            (0.5, -2, Buildup.SHORT_COVERING),
            (-0.5, -2, Buildup.LONG_UNWINDING),
            (0.01, 5, Buildup.NEUTRAL),
            (1.0, 0.05, Buildup.NEUTRAL),
        ],
    )
    def test_classification(self, dp, doi, expected):
        assert classify_buildup(dp, doi) is expected

    def test_bias(self):
        assert Buildup.LONG_BUILDUP.bias == Buildup.SHORT_COVERING.bias == 1
        assert Buildup.SHORT_BUILDUP.bias == Buildup.LONG_UNWINDING.bias == -1
        assert Buildup.NEUTRAL.bias == 0

    def test_futures_buildup_series(self):
        fut = InstrumentKey.parse("NSE:BANKNIFTY:2026-10-27")
        t = ist(date(2026, 10, 20), 9, 15)
        closes_oi = [(100, 1000), (101, 1100), (100, 1200), (101, 1100), (100, 1000)]
        bars = [
            bar(t + timedelta(minutes=15 * i), c, c, c, c, v=1, oi=o, key=fut) for i, (c, o) in enumerate(closes_oi)
        ]
        assert [o.buildup for o in futures_buildup(bars)] == [
            Buildup.LONG_BUILDUP,
            Buildup.SHORT_BUILDUP,
            Buildup.SHORT_COVERING,
            Buildup.LONG_UNWINDING,
        ]

    def test_futures_buildup_refuses_index_and_rolls(self):
        with pytest.raises(ValueError, match="OI"):
            futures_buildup([bar(T0, 1, 1, 1, 1), bar(T0 + timedelta(minutes=1), 1, 1, 1, 1)])
        a = bar(T0, 1, 1, 1, 1, oi=1, key=InstrumentKey.parse("NSE:BANKNIFTY:2026-09-29"))
        b = bar(T0 + timedelta(minutes=1), 1, 1, 1, 1, oi=1, key=InstrumentKey.parse("NSE:BANKNIFTY:2026-10-27"))
        with pytest.raises(ValueError, match="roll"):
            futures_buildup([a, b])

    def test_chain_buildup(self):
        prev = make_chain(52000, ts=T0)
        now = make_chain(
            52300,
            ts=T0 + timedelta(minutes=5),
            strikes=[52000 + 100 * i for i in range(7)],
            oi=lambda k, ot: 1500 if ot is OptionType.CE else 900,
        )
        cb = chain_buildup(prev, now)
        ce = {r.strike: r.buildup for r in cb.rows if r.option_type is OptionType.CE}
        pe = {r.strike: r.buildup for r in cb.rows if r.option_type is OptionType.PE}
        assert ce[52000] is Buildup.LONG_BUILDUP  # call premium up, OI up
        assert pe[52000] is Buildup.LONG_UNWINDING  # put premium down, OI down
        assert cb.counts["CE.long_buildup"] == 4  # only strikes present in both snapshots
        with pytest.raises(ValueError):
            chain_buildup(now, prev)


# ---------------------------------------------------------------- option-chain analytics
class TestOptionsAnalytics:
    def test_iv_summary_flat_vol(self):
        s = iv_summary(make_chain(52000, strikes=WIDE), T0, R)
        assert s.atm_iv == pytest.approx(0.18, abs=1e-5)
        assert s.skew_25d == pytest.approx(0, abs=1e-5)
        assert s.n_failed == 0 and s.forward > s.spot

    def test_skew_is_none_when_chain_too_narrow(self):
        s = iv_summary(make_chain(52000), T0, R)  # +/-300 points: 25-delta strikes are outside
        assert s.atm_iv is not None and s.skew_25d is None

    def test_delta_strikes(self):
        from mp_core.analytics.black_scholes import greeks
        from mp_core.analytics.options import delta_strikes

        kp, kc = delta_strikes(52000, 0.2, 0.1)
        s_eff = 52000 * math.exp(-R * 0.1)  # spot whose forward is 52000; no dividends -> delta = N(d1)
        assert greeks(s_eff, kc, 0.1, R, 0.2, OptionType.CE).delta == pytest.approx(0.25, abs=1e-9)
        assert greeks(s_eff, kp, 0.1, R, 0.2, OptionType.PE).delta == pytest.approx(-0.25, abs=1e-9)

    def test_iv_summary_detects_put_skew(self):
        ch = make_chain(52000, vol=lambda k: 0.18 - 0.02 * (k - 52000) / 300, strikes=WIDE)
        s = iv_summary(ch, T0, R)
        assert s.atm_iv == pytest.approx(0.18 - 0.02 * (s.forward - 52000) / 300, abs=5e-4)
        assert s.skew_25d > 0.05

    def test_term_structure(self):
        near = make_chain(52000, vol=lambda k: 0.20, strikes=WIDE)
        far = make_chain(52000, vol=lambda k: 0.15, expiry=date(2026, 11, 24), strikes=WIDE)
        ts = term_structure([far, near], T0, R)
        assert [p.expiry for p in ts] == [EXP, date(2026, 11, 24)]
        assert ts[0].atm_iv == pytest.approx(0.20, abs=1e-5) and ts[1].atm_iv == pytest.approx(0.15, abs=1e-5)

    def test_pcr_and_max_pain_hand_computed(self):
        call_oi = {51700: 100, 51800: 100, 51900: 200, 52000: 3000, 52100: 5000, 52200: 4000, 52300: 1000}
        put_oi = {51700: 1000, 51800: 4000, 51900: 5000, 52000: 3000, 52100: 200, 52200: 100, 52300: 100}
        ch = make_chain(52000, oi=lambda k, ot: (call_oi if ot is OptionType.CE else put_oi)[k])
        res = pcr_max_pain(ch)
        assert res.pcr_oi == pytest.approx(sum(put_oi.values()) / sum(call_oi.values()))

        def pain(x):
            return sum(o * max(x - k, 0) for k, o in call_oi.items()) + sum(
                o * max(k - x, 0) for k, o in put_oi.items()
            )

        assert res.max_pain == min(call_oi, key=pain) == 52000


# ---------------------------------------------------------------- on the synthetic dataset
@pytest.fixture(scope="module")
def labels(synth_dir):
    import json

    return [DayLabel.model_validate(x) for x in json.loads((synth_dir / "events.json").read_text())]


async def test_index_equals_weighted_constituents(synth_dir):
    for d, hh, mm in [(date(2026, 9, 2), 11, 0), (date(2026, 9, 3), 9, 30), (date(2026, 9, 23), 15, 30)]:
        p = ReplayProvider(synth_dir, SimClock(ist(d, hh, mm)))
        q = (await p.get_quote(BN)).value
        cons = (await p.get_constituents(BN)).value
        assert sum(c.weight for c in cons) == pytest.approx(100)
        assert sum(c.weight * c.change_pct for c in cons) / 100 == pytest.approx(q.change_pct, abs=0.01)


async def test_constituent_weights_drift(synth_dir):
    w = []
    for d in (date(2026, 9, 2), date(2026, 9, 28)):
        p = ReplayProvider(synth_dir, SimClock(ist(d, 11, 0)))
        w.append({c.key.underlying: c.weight for c in (await p.get_constituents(BN)).value})
    assert any(abs(w[0][s] - w[1][s]) > 0.1 for s in w[0])


async def test_iv_summary_on_synthetic_chain(synth_dir):
    p = ReplayProvider(synth_dir, SimClock(ist(date(2026, 9, 10), 11, 0)))
    for i, e in enumerate(await p.get_expiries(BN)):
        ch = (await p.get_option_chain(BN, e)).value
        s = iv_summary(ch, ch.ts, R)
        assert s.atm_iv is not None and 0.05 < s.atm_iv < 1.0
        if i == 0:  # the generator has put skew
            assert s.skew_25d is not None and s.skew_25d > 0
        else:  # +/-25 strikes may not reach 25-delta for the next month: None, never extrapolated
            assert s.skew_25d is None or s.skew_25d > 0
        ok = [x for x in s.strikes if x.call_iv and x.put_iv]
        assert len(ok) > 10
        # calls and puts at a strike were priced off one IV; parity-consistent analytics recover it
        assert max(abs(x.call_iv - x.put_iv) for x in ok) < 0.01


async def test_expiry_day_crush_is_measured(synth_dir, labels):
    exp = next(lb.day for lb in labels if lb.is_expiry_day)
    morning = ReplayProvider(synth_dir, SimClock(ist(exp, 9, 30)))
    late = ReplayProvider(synth_dir, SimClock(ist(exp, 15, 0)))
    iv_am = iv_summary((ch := (await morning.get_option_chain(BN, exp)).value), ch.ts, R).atm_iv
    iv_pm = iv_summary((ch := (await late.get_option_chain(BN, exp)).value), ch.ts, R).atm_iv
    assert iv_pm < 0.8 * iv_am


async def test_levels_and_expected_move_on_synthetic(synth_dir):
    d = date(2026, 9, 10)
    now = ist(d, 11, 0)
    p = ReplayProvider(synth_dir, SimClock(now))
    daily = (await p.get_candles(BN, Interval.D1, ist(date(2026, 9, 1), 0, 0), now)).value
    pd = prior_day(daily, d)
    fut = next(k for k in (InstrumentKey.parse("NSE:BANKNIFTY:2026-09-29"),))
    fut_bars = (await p.get_candles(fut, Interval.M1, cal.session_bounds(d)[0], now)).value
    ch = (await p.get_option_chain(BN, (await p.get_expiries(BN))[0])).value
    lv = compute_levels(pd, vwap(fut_bars), ch)
    assert {"prior_day.high", "pivot.classic.R1", "vwap.session", "oi.call_rank1", "oi.put_rank1"} <= {
        x.basis for x in lv
    }
    s = iv_summary(ch, now, R)
    em = expected_move(ch.spot, s.atm_iv, days_to_next_close(now))
    assert em.one_sigma.lower < ch.spot < em.one_sigma.upper
    assert 0 < em.sigma_pct < 2


async def test_futures_buildup_on_synthetic(synth_dir):
    p = ReplayProvider(synth_dir, SimClock(ist(date(2026, 9, 10), 15, 30)))
    fut = InstrumentKey.parse("NSE:BANKNIFTY:2026-09-29")
    bars = (await p.get_candles(fut, Interval.M15, ist(date(2026, 9, 2), 0, 0), ist(date(2026, 9, 10), 16, 0))).value
    seen = {o.buildup for o in futures_buildup(bars)}
    assert {Buildup.LONG_BUILDUP, Buildup.SHORT_BUILDUP, Buildup.SHORT_COVERING, Buildup.LONG_UNWINDING} <= seen


def test_one_day_range_containment_is_plausible(synth_dir, labels):
    """Generator sanity, NOT evaluation: implied vol > realized by design (vol premium), so
    1-sigma daily containment should be at least ~60% on this dataset."""
    import csv

    closes = {}
    with (synth_dir / "candles_1m.csv").open() as f:
        for row in csv.DictReader(f):
            if row["key"] == "NSE:BANKNIFTY":
                closes[datetime.fromisoformat(row["ts"]).astimezone(cal.IST).date()] = float(row["close"])
    days = sorted(closes)
    inside = 0
    for lb, (a, b) in zip(labels[:-1], zip(days, days[1:], strict=False), strict=False):
        em = expected_move(closes[a], lb.atm_iv_close, (b - a).days)
        inside += em.one_sigma.contains(closes[b])
    assert inside / (len(days) - 1) > 0.6
