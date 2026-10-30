"""The market snapshot: everything the UI and MCP tools need, precomputed.

`build_snapshot` is a pure function of its inputs and `now` - the engine service calls it on every
update, tests call it directly. Sections that cannot be computed are left out and the reason goes
into `errors`; nothing is filled in with a guess.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from datetime import date, datetime, timedelta

from pydantic import Field

from mp_core import market_calendar as cal
from mp_core.analytics.expected_move import ExpectedRange, days_to_next_close, expected_move
from mp_core.analytics.levels import Level, compute_levels, prior_day, vwap
from mp_core.analytics.oi import BuildupObs, ChainBuildup, chain_buildup, futures_buildup
from mp_core.analytics.options import IVSummary, PcrMaxPain, iv_summary, pcr_max_pain
from mp_core.candles import resample
from mp_core.models import (
    AwareDatetime,
    Candle,
    Constituent,
    InstrumentKey,
    Interval,
    MacroSnapshot,
    OptionChain,
    Quote,
    Source,
    Sourced,
    StrictModel,
    derive,
    derived_source,
)
from mp_core.news.models import ScoredNewsItem
from mp_core.news.score import NewsScore, entity_weights, news_score
from mp_core.providers.base import DataUnavailable, MarketDataProvider
from mp_core.registry import get_index
from mp_core.signals.models import Signal

SNAPSHOT_SCHEMA = 1


class EngineInputs(StrictModel):
    symbol: str
    quote: Sourced[Quote]
    future_quote: Sourced[Quote] | None = None
    chains: dict[date, Sourced[OptionChain]] = {}
    prev_chains: dict[date, Sourced[OptionChain]] = {}  # previous snapshot per expiry (for build-up)
    daily: Sourced[list[Candle]] | None = None  # index daily bars, most recent last
    future_1m_today: Sourced[list[Candle]] | None = None  # front future, today's session
    constituents: Sourced[list[Constituent]] | None = None
    macro: MacroSnapshot | None = None
    news: list[ScoredNewsItem] = []
    news_poll: Sourced[int] | None = None  # as_of = last completed news poll


class FuturesView(StrictModel):
    key: InstrumentKey
    ltp: float
    basis_points: float  # future - spot
    oi: int | None
    buildup_15m: list[BuildupObs]  # most recent last


class ExpiryView(StrictModel):
    expiry: date
    days_to_expiry: float
    iv: IVSummary
    pcr: PcrMaxPain
    buildup: ChainBuildup | None = None  # vs the previous chain snapshot


class MarketSnapshot(StrictModel):
    schema_version: int = SNAPSHOT_SCHEMA
    symbol: str
    version: int = Field(ge=0)
    computed_at: AwareDatetime
    compute_ms: float
    source: Source  # synthetic if any input was synthetic
    stale_sections: list[str]
    quote: Sourced[Quote]
    future: Sourced[FuturesView] | None = None
    expected_range: Sourced[dict[str, ExpectedRange]] | None = None
    expiries: list[Sourced[ExpiryView]] = []
    levels: Sourced[list[Level]] | None = None
    constituents: Sourced[list[Constituent]] | None = None
    macro: MacroSnapshot | None = None
    news: Sourced[NewsScore] | None = None
    signal: Signal | None = None  # filled by the engine via mp_core.signals.fusion.fuse
    errors: list[str] = []


def build_snapshot(
    inputs: EngineInputs, now: datetime, *, rate: float, version: int = 0, cache: dict | None = None
) -> MarketSnapshot:
    """`cache` (optional, owned by the caller) memoises per-expiry analytics by chain timestamp: they
    depend only on the chain, which changes about once a minute while snapshots run at 4/s."""
    t0 = time.perf_counter()
    errors: list[str] = []
    quote = inputs.quote.restamp(now)
    spot = quote.value.ltp
    today = now.astimezone(cal.IST).date()
    chains = {e: c for e, c in sorted(inputs.chains.items()) if cal.expiry_close(e) > now}
    front = next(iter(chains.values()), None)

    # --- per-expiry option analytics
    expiry_views: list[Sourced[ExpiryView]] = []
    front_iv: float | None = None
    for e, sc in chains.items():
        ch = sc.value
        prev = inputs.prev_chains.get(e)
        ck = (e, ch.ts, prev.value.ts if prev is not None else None, rate)
        if cache is not None and ck in cache:
            view = cache[ck]
            expiry_views.append(derive(view, [sc], now=now))
            if sc is front:
                front_iv = view.iv.atm_iv
            continue
        try:
            # IVs belong to the moment the prices were observed: evaluating them at a later `now`
            # would shift every IV a little on each tick (time to expiry shrinks) with no new prices.
            ivs = iv_summary(ch, ch.ts, rate)
            bu, used = None, [sc]
            prev = inputs.prev_chains.get(e)
            if prev is not None and prev.value.ts < ch.ts:
                bu = chain_buildup(prev.value, ch)
                used.append(prev)
            view = ExpiryView(expiry=e, days_to_expiry=ivs.t_years * 365, iv=ivs, pcr=pcr_max_pain(ch), buildup=bu)
            if cache is not None:
                if len(cache) > 64:
                    cache.clear()
                cache[ck] = view
            expiry_views.append(derive(view, [sc], now=now))  # freshness follows the current chain
            if sc is front:
                front_iv = ivs.atm_iv
        except ValueError as ex:
            errors.append(f"expiry {e}: {ex}")

    # --- expected range (needs spot + front ATM IV)
    expected = None
    if front is None:
        errors.append("expected_range: no option chain")
    elif front_iv is None:
        errors.append("expected_range: front-expiry ATM IV not identifiable")
    else:
        fe = front.value.expiry
        horizons = {
            "session": days_to_next_close(now),
            "next_session": days_to_next_close(now, 2),
            "expiry": max((cal.expiry_close(fe) - now).total_seconds(), 0) / 86400,
        }
        expected = derive({k: expected_move(spot, front_iv, d) for k, d in horizons.items()}, [quote, front], now=now)

    # --- futures view
    future = None
    if inputs.future_quote is not None:
        fq = inputs.future_quote.value
        used: list[Sourced] = [inputs.future_quote, quote]
        bu: list[BuildupObs] = []
        if inputs.future_1m_today is not None and inputs.future_1m_today.value:
            bars = resample(fq.key, Interval.M15, inputs.future_1m_today.value, now)
            try:
                bu = futures_buildup(bars)[-8:]
                used.append(inputs.future_1m_today)
            except ValueError as ex:
                errors.append(f"futures buildup: {ex}")
        view = FuturesView(key=fq.key, ltp=fq.ltp, basis_points=fq.ltp - spot, oi=fq.oi, buildup_15m=bu)
        future = derive(view, used, now=now)
    else:
        errors.append("future: no front-month future quote")

    # --- levels
    levels = None
    if inputs.daily is None or not inputs.daily.value:
        errors.append("levels: no daily candles for prior day")
    else:
        try:
            pd = prior_day(inputs.daily.value, today)
            used = [inputs.daily]
            vw = None
            fut_bars = inputs.future_1m_today.value if inputs.future_1m_today else []
            fut_bars = [b for b in fut_bars if b.ts.astimezone(cal.IST).date() == today]
            if fut_bars:
                try:
                    vw = vwap(fut_bars)
                    used.append(inputs.future_1m_today)
                except ValueError as ex:
                    errors.append(f"levels.vwap: {ex}")
            else:
                errors.append("levels.vwap: no futures bars yet today")
            if front is not None:
                used.append(front)
            levels = derive(compute_levels(pd, vw, front.value if front else None), used, now=now)
        except ValueError as ex:
            errors.append(f"levels: {ex}")

    constituents = inputs.constituents.restamp(now) if inputs.constituents else None
    if constituents is None:
        errors.append("constituents: unavailable")
    macro = _restamp_macro(inputs.macro, now) if inputs.macro else None

    # --- news (weighted by *live* constituent weights when we have them)
    news = None
    if inputs.news_poll is None:
        errors.append("news: no news poll yet (is ingest-news running?)")
    else:
        live_w = {c.key.underlying: c.weight for c in constituents.value} if constituents else None
        weights = entity_weights(get_index(inputs.symbol), live_w)
        news = derive(news_score(inputs.symbol, inputs.news, weights, now), [inputs.news_poll], now=now)

    sections: dict[str, Sourced | None] = {
        "quote": quote,
        "future": future,
        "expected_range": expected,
        "levels": levels,
        "constituents": constituents,
        "news": news,
        **{f"expiry:{v.value.expiry}": v for v in expiry_views},
    }
    present = [s for s in sections.values() if s is not None]
    return MarketSnapshot(
        symbol=inputs.symbol,
        version=version,
        computed_at=now,
        compute_ms=round((time.perf_counter() - t0) * 1000, 3),
        source=derived_source([s.source for s in present]),
        stale_sections=sorted(k for k, s in sections.items() if s is not None and s.stale),
        quote=quote,
        future=future,
        expected_range=expected,
        expiries=expiry_views,
        levels=levels,
        constituents=constituents,
        macro=macro,
        news=news,
        errors=errors,
    )


def _restamp_macro(m: MacroSnapshot, now: datetime) -> MacroSnapshot:
    return MacroSnapshot(**{k: (v.restamp(now) if v is not None else None) for k, v in m})


def front_future_key(symbol: str, on: date) -> InstrumentKey:
    spec = get_index(symbol)
    return InstrumentKey(exchange=spec.exchange, underlying=spec.underlying, expiry=cal.front_future_expiry(spec, on))


async def gather_inputs(
    provider: MarketDataProvider,
    symbol: str,
    now: datetime,
    prev_chains: dict[date, Sourced[OptionChain]] | None = None,
    expiries: Sequence[date] | None = None,
) -> EngineInputs:
    """Collect EngineInputs straight from a provider (used by tests and one-shot tools; the
    services go through Redis instead)."""
    key = InstrumentKey.parse(symbol)
    today = now.astimezone(cal.IST).date()
    fut = front_future_key(symbol, today)

    async def opt(coro):
        try:
            return await coro
        except DataUnavailable:
            return None

    exps = list(expiries) if expiries is not None else await provider.get_expiries(key)
    chains = {}
    for e in exps:
        c = await opt(provider.get_option_chain(key, e))
        if c is not None:
            chains[e] = c
    o, _ = cal.session_bounds(today)
    return EngineInputs(
        symbol=key.symbol,
        quote=await provider.get_quote(key),
        future_quote=await opt(provider.get_quote(fut)),
        chains=chains,
        prev_chains=prev_chains or {},
        daily=await opt(provider.get_candles(key, Interval.D1, now - timedelta(days=10), now)),
        future_1m_today=await opt(provider.get_candles(fut, Interval.M1, o, now)),
        constituents=await opt(provider.get_constituents(key)),
        macro=await opt(provider.get_macro()),
    )
