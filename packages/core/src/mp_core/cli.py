"""`mp` command line: generate synthetic datasets, inspect replays, load bhavcopies."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from mp_core import market_calendar as cal
from mp_core.clock import SimClock
from mp_core.models import Interval
from mp_core.providers.base import DataUnavailable
from mp_core.providers.replay import ReplayProvider


def _cmd_synth(a: argparse.Namespace) -> None:
    from mp_core.synthetic.generator import generate

    out = Path(a.out or f"data/replay/synthetic/{a.symbol.split(':')[-1].upper()}")
    m = generate(a.symbol, out, start=date.fromisoformat(a.start), days=a.days, seed=a.seed)
    labels = json.loads((out / "events.json").read_text())
    print(f"wrote {out}  source={m.source}  days={m.days[0]}..{m.days[-1]} ({len(m.days)})  seed={m.seed}")
    for lb in labels:
        flags = [
            f
            for f, on in (
                ("GAP", lb["is_gap_day"]),
                ("JUMP", lb["jump_pct"] != 0),
                ("EXPIRY", lb["is_expiry_day"]),
                ("IV-CRUSH", lb["iv_crush"]),
            )
            if on
        ]
        print(
            f"  {lb['day']}  {lb['regime']:<8} rv={lb['realized_vol_annual']:.1%}  gap={lb['gap_pct']:+.2f}%  "
            f"iv {lb['atm_iv_open']:.1%}->{lb['atm_iv_close']:.1%}  {' '.join(flags)}"
        )


async def _replay_summary(a: argparse.Namespace) -> None:
    probe = ReplayProvider(a.dir, SimClock(datetime.fromisoformat("2000-01-01T00:00:00+00:00")))
    day = date.fromisoformat(a.day) if a.day else probe.days[min(1, len(probe.days) - 1)]
    hh, mm = map(int, a.time.split(":"))
    at = datetime.combine(day, datetime.min.time().replace(hour=hh, minute=mm), cal.IST)
    p = ReplayProvider(a.dir, SimClock(at))
    idx = p.index_key
    q = await p.get_quote(idx)
    print(f"[{p.source}] {idx.symbol} @ {at:%Y-%m-%d %H:%M IST}")
    print(
        f"  quote   ltp={q.value.ltp:,.2f}  chg={q.value.change_pct:+.2f}%  H/L={q.value.high:,.2f}/{q.value.low:,.2f}"
        f"  as_of={q.as_of:%H:%M:%S}Z stale={q.stale}"
    )
    c = await p.get_candles(idx, Interval.M15, at - timedelta(hours=2), at)
    print(
        f"  15m bars (last 2h, complete only): {len(c.value)}"
        f"  last={c.value[-1].ts:%H:%M}Z close={c.value[-1].close:,.2f}"
    )
    exps = await p.get_expiries(idx)
    print(f"  expiries {', '.join(map(str, exps))}")
    ch = (await p.get_option_chain(idx, exps[0])).value
    atm = min(ch.rows, key=lambda r: abs(r.strike - ch.spot))
    print(
        f"  chain {exps[0]} spot={ch.spot:,.2f} ATM {atm.strike:g}: CE {atm.call.ltp} (OI {atm.call.oi:,})"
        f"  PE {atm.put.ltp} (OI {atm.put.oi:,})"
    )
    cons = (await p.get_constituents(idx)).value
    top = sorted(cons, key=lambda x: -x.weight)[:3]
    print("  top constituents " + ", ".join(f"{x.key.underlying} {x.change_pct:+.2f}%" for x in top))
    mac = await p.get_macro()
    print(
        f"  macro VIX={mac.india_vix.value} USDINR={mac.usdinr.value} 10Y={mac.in10y_yield.value}"
        f" FII={mac.fii_net_cr.value:+,.0f}cr (as_of {mac.fii_net_cr.as_of:%Y-%m-%d %H:%M}Z)"
    )
    n = 0
    async for _ in p.stream_ticks([idx]):
        n += 1
        if n == 20:
            break
    print(f"  streamed {n} ticks; clock advanced to {p.clock.now():%H:%M:%S}Z")


async def _analyze(a: argparse.Namespace) -> None:
    from mp_core.analytics.expected_move import days_to_next_close, expected_move
    from mp_core.analytics.levels import compute_levels, nearest, prior_day, vwap
    from mp_core.analytics.oi import futures_buildup
    from mp_core.analytics.options import iv_summary, pcr_max_pain
    from mp_core.models import InstrumentKey
    from mp_core.registry import get_index
    from mp_core.settings import get_settings

    probe = ReplayProvider(a.dir, SimClock(datetime.fromisoformat("2000-01-01T00:00:00+00:00")))
    day = date.fromisoformat(a.day) if a.day else probe.days[min(1, len(probe.days) - 1)]
    hh, mm = map(int, a.time.split(":"))
    now = datetime.combine(day, datetime.min.time().replace(hour=hh, minute=mm), cal.IST)
    p = ReplayProvider(a.dir, SimClock(now))
    idx, r = p.index_key, get_settings().risk_free_rate
    spot = (await p.get_quote(idx)).value.ltp
    print(f"[{p.source}] {idx.symbol} @ {now:%Y-%m-%d %H:%M IST}  spot={spot:,.2f}")

    exps = await p.get_expiries(idx)
    chain = (await p.get_option_chain(idx, exps[0])).value
    for e in exps:
        s = iv_summary((await p.get_option_chain(idx, e)).value, now, r)
        skew = "n/a (chain too narrow)" if s.skew_25d is None else f"{100 * s.skew_25d:+.2f} vol pts"
        print(
            f"  IV {e} ({s.t_years * 365:.1f}d): ATM {100 * s.atm_iv:.2f}%  25d skew {skew}"
            f"  [{s.n_ok} solved, {s.n_failed} unidentifiable]"
        )
    atm_iv = iv_summary(chain, now, r).atm_iv
    for label, days in (
        ("today's close", days_to_next_close(now)),
        ("next session", days_to_next_close(now, 2)),
        ("expiry", (cal.expiry_close(exps[0]) - now).total_seconds() / 86400),
    ):
        em = expected_move(spot, atm_iv, days)
        print(
            f"  expected move to {label:<13} ({days:5.2f}d): +/-{em.sigma_points:,.0f} ({em.sigma_pct:.2f}%)"
            f"  1s {em.one_sigma.lower:,.0f}-{em.one_sigma.upper:,.0f}"
            f"  2s {em.two_sigma.lower:,.0f}-{em.two_sigma.upper:,.0f}"
        )

    pm = pcr_max_pain(chain)
    print(f"  PCR(OI)={pm.pcr_oi:.2f}  PCR(vol)={pm.pcr_volume:.2f}  max pain={pm.max_pain:,.0f}")

    daily = (await p.get_candles(idx, Interval.D1, now - timedelta(days=10), now)).value
    fut = InstrumentKey(
        exchange=idx.exchange, underlying=idx.underlying, expiry=cal.front_future_expiry(get_index(idx.symbol), day)
    )
    fut_today = (await p.get_candles(fut, Interval.M1, cal.session_bounds(day)[0], now)).value
    levels = compute_levels(prior_day(daily, day), vwap(fut_today), chain)
    below, above = nearest(levels, spot)
    print(
        f"  levels ({len(levels)}): nearest support {below.name} {below.price:,.0f} [{below.basis}]"
        f"  nearest resistance {above.name} {above.price:,.0f} [{above.basis}]"
    )
    rows = [(lv.price, f"{lv.kind:<10} {lv.name:<22} {lv.basis}") for lv in levels]
    rows.append((spot, f"{'-' * 10} SPOT {'-' * 17}"))
    for price, text in sorted(rows, key=lambda x: x[0]):
        print(f"    {price:>10,.1f}  {text}")
    bu = futures_buildup((await p.get_candles(fut, Interval.M15, cal.session_bounds(day)[0], now)).value)
    print("  futures 15m build-up: " + ", ".join(f"{o.end.astimezone(cal.IST):%H:%M} {o.buildup}" for o in bu[-4:]))


def _cmd_synth_news(a: argparse.Namespace) -> None:
    from mp_core.synthetic.news import generate_news

    news, labels = generate_news(Path(a.dir), seed=a.seed)
    rows = [json.loads(x) for x in news.read_text("utf-8").splitlines()]
    print(f"wrote {len(rows)} synthetic news items -> {news}  (labels: {labels.name})")
    for r in rows[:5]:
        print(f"  {r['published_at'][:16]}  {r['feed']:<17} {r['title'][:80]}")


async def _news_eval(a: argparse.Namespace) -> None:
    """Classify a sample of synthetic fixture items with Haiku and compare with the ground truth."""
    from mp_core.news.classifier import NewsClassifier
    from mp_core.news.feeds import FixtureSource
    from mp_core.synthetic.news import load_labels

    d = Path(a.dir)
    labels = load_labels(d / "news_labels.jsonl")
    seen, sample = set(), []
    for it in FixtureSource(d / "news.jsonl").items:  # one per distinct title
        if it.title not in seen and it.id in labels:
            seen.add(it.title)
            sample.append(it)
    sample = sample[: a.limit]
    clf = NewsClassifier.from_env(max_concurrency=4)
    if clf is None:
        raise SystemExit("no Anthropic credentials: set ANTHROPIC_API_KEY (or `ant auth login`)")
    print(f"classifying {len(sample)} synthetic items with {clf.model} (one call each, ~1-2k tokens per call)")
    results = await asyncio.gather(*(clf.classify(i) for i in sample))
    ok = [(i, r) for i, r in zip(sample, results, strict=True) if r.status == "classified"]
    agree_rel = sum(r.classification.relevant == labels[i.id].relevant for i, r in ok)
    pairs = [(r.classification, labels[i.id]) for i, r in ok if labels[i.id].relevant]
    sign = sum((c.sentiment > 0.05) - (c.sentiment < -0.05) == (t.sentiment > 0) - (t.sentiment < 0) for c, t in pairs)
    impact = sum(c.impact == t.impact for c, t in pairs)
    ents = sum(bool(set(c.entities) & set(t.entities)) for c, t in pairs)
    print(
        f"classified {len(ok)}/{len(sample)}  (dropped {sum(r.status == 'dropped' for r in results)}, "
        f"unclassified {sum(r.status == 'unclassified' for r in results)})"
    )
    if ok:
        print(f"  relevant agrees       {agree_rel}/{len(ok)}")
    if pairs:
        print(
            f"  sentiment sign agrees {sign}/{len(pairs)}   impact agrees {impact}/{len(pairs)}   "
            f"entity overlap {ents}/{len(pairs)}"
        )
    print("note: labels are the generator's intent for synthetic headlines - a sanity check, not a benchmark.")


async def _bhav_load(a: argparse.Namespace) -> None:
    from mp_core.loaders.bhavcopy import read_bhavcopy_zip
    from mp_core.settings import get_settings
    from mp_core.store.history import History

    s = get_settings()
    if not s.database_url:
        raise SystemExit("DATABASE_URL is not set")
    hist = await History(s.database_url).connect()
    try:
        for f in a.files:
            recs = read_bhavcopy_zip(Path(f), a.symbol and [a.symbol])
            print(f"{f}: {await hist.record_fo_eod(recs)} rows")
    finally:
        await hist.close()


async def _backtest(a: argparse.Namespace) -> None:
    from mp_core.loaders.bhavcopy import read_bhavcopy_zip
    from mp_core.settings import get_settings
    from mp_core.signals.backtest import run_backtest

    underlying = a.symbol.split(":")[-1].upper()
    d0, d1 = date.fromisoformat(a.start), date.fromisoformat(a.end)
    if a.zips is not None:  # straight from downloaded files - no database needed
        records = [r for f in a.zips for r in read_bhavcopy_zip(Path(f), [underlying])]
    else:
        from mp_core.store.history import History

        hist = await History(get_settings().database_url).connect()
        try:
            records = await hist.fo_eod(underlying, d0 - timedelta(days=7), d1 + timedelta(days=7))
        finally:
            await hist.close()
    if not records:
        raise SystemExit(f"no bhavcopy records for {underlying}; download zips and pass --zips or load them")
    res = run_backtest(a.strategy, records, underlying, d0, d1, get_settings().risk_free_rate)
    print(json.dumps(res.model_dump(mode="json"), indent=2))


async def _evaluate(a: argparse.Namespace) -> None:
    from datetime import UTC

    from mp_core.settings import get_settings
    from mp_core.signals.evaluation import compute_stats, evaluate_pending
    from mp_core.store.history import History

    hist = await History(get_settings().database_url).connect()
    try:
        print(await evaluate_pending(hist, a.symbol, datetime.now(UTC)))
        stats = compute_stats(
            a.symbol, await hist.outcomes(a.symbol), n_synthetic_signals=await hist.count_synthetic_signals(a.symbol)
        )
        print(json.dumps(stats.model_dump(mode="json"), indent=2))
    finally:
        await hist.close()


async def _signal(a: argparse.Namespace) -> None:
    from mp_core.settings import get_settings
    from mp_core.signals.fusion import RiskConfig, fuse
    from mp_core.snapshot import build_snapshot, gather_inputs

    probe = ReplayProvider(a.dir, SimClock(datetime.fromisoformat("2000-01-01T00:00:00+00:00")))
    day = date.fromisoformat(a.day) if a.day else probe.days[min(1, len(probe.days) - 1)]
    hh, mm = map(int, a.time.split(":"))
    now = datetime.combine(day, datetime.min.time().replace(hour=hh, minute=mm), cal.IST)
    p = ReplayProvider(a.dir, SimClock(now))
    s = get_settings()
    snap = build_snapshot(await gather_inputs(p, p.index_key.symbol, now), now, rate=s.risk_free_rate)
    sig = fuse(snap, RiskConfig(s.capital, s.max_risk_pct))
    print(
        f"[{sig.source}] {sig.symbol} @ {now:%Y-%m-%d %H:%M IST}  spot {sig.spot:,.1f}  1-sigma ({sig.horizon}) "
        f"{sig.sigma_points:,.0f} pts   (news not included: no news poll in this one-shot view)"
    )
    print(f"  {sig.status.upper()}  bias={sig.bias}  composite={sig.composite:+.2f}  confidence={sig.confidence:.2f}")
    for c in sig.components:
        print(f"  {c.name:<9} {c.score:+.2f}  {'(unavailable)' if not c.available else ''}")
    for r in sig.reasons:
        print(f"    [{r.component}] {r.text}   <- {r.input}")
    if sig.buy_zone and sig.sell_zone:
        print(
            f"  buy zone {sig.buy_zone.low:,.0f}-{sig.buy_zone.high:,.0f} ({sig.buy_zone.basis[0]})   "
            f"sell zone {sig.sell_zone.low:,.0f}-{sig.sell_zone.high:,.0f} ({sig.sell_zone.basis[0]})"
        )
    if sig.status == "active":
        print(
            f"  entry {sig.entry:,.0f}  stop {sig.stop_loss:,.0f}  targets "
            f"{', '.join(f'{t.price:,.0f} ({t.basis})' for t in sig.targets)}  reward:risk {sig.reward_risk:.2f}"
        )
        z = sig.sizing
        print(
            f"  sizing: {z.lots} lot(s) of {z.lot_size}, INR {z.loss_at_stop:,.0f} at stop "
            f"(budget INR {z.max_loss:,.0f})"
        )
    for b in sig.no_signal_reasons:
        print(f"  no signal: {b}")


async def _upstox(a: argparse.Namespace) -> None:
    from mp_core.providers.upstox import authorize_url, exchange_code, provider_from_settings
    from mp_core.settings import get_settings

    s = get_settings()
    if a.cmd == "upstox-login":
        if not (s.upstox_api_key and s.upstox_api_secret):
            raise SystemExit("set UPSTOX_API_KEY and UPSTOX_API_SECRET (from the Upstox developer console) in .env")
        print("1. Open this URL, log in to Upstox and approve:")
        print("   " + authorize_url(s.upstox_api_key, s.upstox_redirect_uri))
        print("2. You will be redirected to " + s.upstox_redirect_uri + "?code=XXXX - copy the code.")
        code = input("code: ").strip()
        token = await exchange_code(code, s.upstox_api_key, s.upstox_api_secret, s.upstox_redirect_uri)
        print()
        print("Today's access token (valid until ~3:30 AM IST tomorrow). Put it in .env as UPSTOX_ACCESS_TOKEN:")
        print(token)
        return
    from mp_core.providers.upstox import UpstoxError

    try:
        p = await provider_from_settings()
        try:
            print(json.dumps(await p.check(), indent=2))
        finally:
            await p.close()
    except UpstoxError as ex:
        raise SystemExit(f"Upstox check failed: {ex}") from None


def _set_env_var(path: Path, name: str, value: str) -> None:
    """Replace (or append) NAME=value in a .env file, leaving every other line untouched."""
    lines = path.read_text("utf-8").splitlines() if path.exists() else []
    out = [ln for ln in lines if not ln.startswith(f"{name}=")]
    at = next((i for i, ln in enumerate(lines) if ln.startswith(f"{name}=")), None)
    out.insert(at if at is not None else len(out), f"{name}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


async def _kite(a: argparse.Namespace) -> None:
    from mp_core.providers.kite import KiteError, exchange_request_token, login_url, parse_request_token
    from mp_core.providers.kite import provider_from_settings as kite_provider
    from mp_core.settings import get_settings

    s = get_settings()
    if a.cmd == "kite-login":
        if not (s.kite_api_key and s.kite_api_secret):
            raise SystemExit("set KITE_API_KEY and KITE_API_SECRET (from developers.kite.trade) in .env")
        print("1. Open this URL and log in to Zerodha:")
        print("   " + login_url(s.kite_api_key))
        print("2. You are sent to your redirect URL with ?request_token=... (an error page is fine).")
        print("   Paste the whole address-bar URL, or just the request_token, within a few minutes:")
        token = parse_request_token(a.token or input("> "))
        try:
            got = await exchange_request_token(token, s.kite_api_key, s.kite_api_secret)
        except KiteError as ex:
            raise SystemExit(f"Kite login failed: {ex}") from None
        print(f"\nLogged in as {got['user']}. Token valid until ~6 AM IST tomorrow.")
        if a.no_save:
            print("KITE_ACCESS_TOKEN=" + got["access_token"])
        else:
            _set_env_var(Path(a.env), "KITE_ACCESS_TOKEN", got["access_token"])
            print(f"Saved KITE_ACCESS_TOKEN to {a.env} - restart the services to use it.")
        return
    try:
        p = await kite_provider()
        try:
            print(json.dumps(await p.check(), indent=2))
        finally:
            await p.close()
    except (KiteError, DataUnavailable) as ex:
        raise SystemExit(f"Kite check failed: {ex}") from None


def _cmd_bhav(a: argparse.Namespace) -> None:
    from mp_core.loaders.bhavcopy import read_bhavcopy_zip

    recs = read_bhavcopy_zip(Path(a.file), a.symbol and [a.symbol])
    print(f"{len(recs)} records")
    for r in recs[: a.head]:
        print(" ", r.trade_date, r.key.id, "close", r.close, "OI", r.oi, "chg", r.oi_change)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="mp")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("synth", help="generate a synthetic replay dataset")
    s.add_argument("--symbol", default="NSE:BANKNIFTY")
    s.add_argument("--start", default="2026-09-01")
    s.add_argument("--days", type=int, default=25)
    s.add_argument("--seed", type=int, default=2)
    s.add_argument("--out")
    r = sub.add_parser("replay", help="print what the ReplayProvider sees at a moment")
    r.add_argument("--dir", default="data/replay/synthetic/BANKNIFTY")
    r.add_argument("--day", help="YYYY-MM-DD (default: 2nd day of dataset)")
    r.add_argument("--time", default="11:00", help="HH:MM IST")
    an = sub.add_parser("analyze", help="run Phase 2 analytics on a replay moment")
    an.add_argument("--dir", default="data/replay/synthetic/BANKNIFTY")
    an.add_argument("--day", help="YYYY-MM-DD (default: 2nd day of dataset)")
    an.add_argument("--time", default="11:00", help="HH:MM IST")
    sn = sub.add_parser("synth-news", help="generate synthetic news (+ ground-truth labels) for a dataset")
    sn.add_argument("--dir", default="data/replay/synthetic/BANKNIFTY")
    sn.add_argument("--seed", type=int, default=7)
    ne = sub.add_parser("news-eval", help="classify a sample of synthetic news with Claude Haiku (uses the API)")
    ne.add_argument("--dir", default="data/replay/synthetic/BANKNIFTY")
    ne.add_argument("--limit", type=int, default=20)
    sub.add_parser("upstox-login", help="get today's Upstox access token (OAuth, needs UPSTOX_API_KEY/SECRET)")
    sub.add_parser("upstox-check", help="verify the Upstox token and adapter against the real API")
    kl = sub.add_parser("kite-login", help="get today's Zerodha Kite access token (needs KITE_API_KEY/SECRET)")
    kl.add_argument("token", nargs="?", help="request_token or the full redirect URL (prompted if omitted)")
    kl.add_argument("--env", default=".env", help="file to save KITE_ACCESS_TOKEN into")
    kl.add_argument("--no-save", action="store_true", help="print the token instead of saving it")
    sub.add_parser("kite-check", help="verify the Kite token and adapter against the real API")
    sg = sub.add_parser("signal", help="fused signal at a replay moment (technical + options; no news)")
    sg.add_argument("--dir", default="data/replay/synthetic/BANKNIFTY")
    sg.add_argument("--day")
    sg.add_argument("--time", default="11:15", help="HH:MM IST")
    bl = sub.add_parser("bhavcopy-load", help="load F&O bhavcopy zips into Timescale (fo_eod)")
    bl.add_argument("files", nargs="+")
    bl.add_argument("--symbol", help="only this underlying, e.g. BANKNIFTY")
    bt = sub.add_parser("backtest", help="EOD backtest on real bhavcopy data")
    bt.add_argument("strategy", choices=["expected_range", "oi_levels"])
    bt.add_argument("--from", dest="start", required=True)
    bt.add_argument("--to", dest="end", required=True)
    bt.add_argument("--symbol", default="NSE:BANKNIFTY")
    bt.add_argument("--zips", nargs="*", help="read these bhavcopy zips instead of the database")
    ev = sub.add_parser("evaluate", help="score past signals now (the evaluator service does this nightly)")
    ev.add_argument("--symbol", default="NSE:BANKNIFTY")
    b = sub.add_parser("bhavcopy", help="parse a downloaded F&O bhavcopy zip")
    b.add_argument("file")
    b.add_argument("--symbol")
    b.add_argument("--head", type=int, default=10)
    a = ap.parse_args(argv)
    if a.cmd == "synth":
        _cmd_synth(a)
    elif a.cmd == "replay":
        asyncio.run(_replay_summary(a))
    elif a.cmd == "analyze":
        asyncio.run(_analyze(a))
    elif a.cmd in ("upstox-login", "upstox-check"):
        asyncio.run(_upstox(a))
    elif a.cmd in ("kite-login", "kite-check"):
        asyncio.run(_kite(a))
    elif a.cmd == "signal":
        asyncio.run(_signal(a))
    elif a.cmd == "synth-news":
        _cmd_synth_news(a)
    elif a.cmd == "news-eval":
        asyncio.run(_news_eval(a))
    elif a.cmd in ("bhavcopy-load", "backtest", "evaluate"):
        fn = {"bhavcopy-load": _bhav_load, "backtest": _backtest, "evaluate": _evaluate}[a.cmd]
        if sys.platform == "win32":  # async psycopg needs a selector loop on Windows
            asyncio.run(fn(a), loop_factory=asyncio.SelectorEventLoop)
        else:
            asyncio.run(fn(a))
    else:
        _cmd_bhav(a)


if __name__ == "__main__":
    sys.exit(main())
