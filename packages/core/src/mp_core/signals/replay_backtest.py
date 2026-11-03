"""Intraday backtest of the live signal rules (`fuse`) over a replay dataset - the same code path as
the engine: ReplayProvider -> gather_inputs -> build_snapshot -> fuse -> SignalLogger.

For every trading day in range the clock steps through the session (default every minute). Each
snapshot is fused exactly as the engine would; the SignalLogger decides which signals are logged
(status/bias change, or 15 minutes unchanged). Logged active signals are then traded like an
account would: one position at a time; an entry order rests until it fills, or until the next
logged signal (a re-logged signal replaces it with fresh levels, a withdrawn one cancels it);
while a position is open, new signals are ignored. Each trade is scored on the *future* (entry/stop/
targets plus the basis at signal time, on futures 1m bars), net of the costs the signal budgeted:

    target  +(target - entry) - costs        stop  -(entry - stop) - costs
    open    mark-to-market at the horizon close - costs              not_filled  0

Look-ahead: snapshots only ever see data <= the replay clock. Scoring needs the bars *after* a
signal, so it uses a separate provider whose clock sits at the end of the dataset - it never
feeds back into a signal.

Not included (stated in every result): news (no news poll in a replay backtest; the news
component is unavailable, which lowers confidence but not direction). Synthetic datasets only run
with `allow_synthetic`, and the result then says it measures the pipeline, not an edge.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path

from mp_core import market_calendar as cal
from mp_core.clock import SimClock
from mp_core.models import InstrumentKey, Interval, OptionChain, Source, Sourced, StrictModel
from mp_core.providers.base import DataUnavailable
from mp_core.providers.replay import ReplayProvider
from mp_core.signals.evaluation import Outcome, as_future_trade, score_signal
from mp_core.signals.fusion import RULES_VERSION, RiskConfig, fuse, horizon_end
from mp_core.signals.logger import SignalLogger
from mp_core.signals.models import Signal
from mp_core.snapshot import build_snapshot, gather_inputs

FIRST_STEP = timedelta(minutes=5)  # skip the opening minutes: no 1m bars yet for VWAP / build-up
LAST_ENTRY = timedelta(minutes=15)  # no new signals in the last 15 minutes of the session


class Trade(StrictModel):
    signal_id: str
    ts: datetime
    bias: str
    lots: int
    outcome: str
    pnl_points: float  # per unit of the future, after costs (0 when not filled)
    pnl_inr: float  # lots x lot size x pnl_points
    reward_risk: float | None


class ReplayBacktestResult(StrictModel):
    symbol: str
    rules_version: str
    source: Source
    start: date
    end: date
    days: int
    snapshots: int
    signals_logged: int
    active: int
    filled: int
    targets: int
    stops: int
    open: int
    win_rate: float | None  # target / (target + stop)
    expectancy_points: float | None  # mean pnl per filled trade, after costs
    total_pnl_inr: float
    max_drawdown_inr: float
    trades: list[Trade]
    notes: list[str]


class _MemoryHistory:
    def __init__(self) -> None:
        self.signals: list[Signal] = []

    async def record_signal(self, signal: Signal, inputs: dict) -> None:
        self.signals.append(signal)


def _trade(sig: Signal, fut_bars, cancel_at: datetime | None) -> tuple[Outcome | None, Trade | None]:
    """Score one logged signal as a trade on the future."""
    fut_sig = as_future_trade(sig)
    if fut_sig is None or sig.sizing is None:
        return None, None
    t = score_signal(fut_sig, fut_bars, allow_synthetic=True, cancel_at=cancel_at)
    if t is None:
        return None, None
    sz = sig.sizing
    cost = sz.costs_per_lot / sz.lot_size
    up = sig.bias == "bullish"
    entry, stop, target = fut_sig.entry, fut_sig.stop_loss, fut_sig.targets[0].price
    sign = 1 if up else -1
    pnl = {
        "target": sign * (target - entry) - cost,
        "stop": sign * (stop - entry) - cost,
        "open": sign * (t.close_at_horizon - entry) - cost,
        "not_filled": 0.0,
    }[t.outcome]
    return t, Trade(
        signal_id=sig.signal_id,
        ts=sig.ts,
        bias=sig.bias,
        lots=sz.lots,
        outcome=t.outcome,
        pnl_points=pnl,
        pnl_inr=pnl * sz.lots * sz.lot_size,
        reward_risk=sig.reward_risk,
    )


async def run_replay_backtest(
    directory: str | Path,
    *,
    start: date | None = None,
    end: date | None = None,
    step: timedelta = timedelta(minutes=1),
    risk: RiskConfig | None = None,
    rate: float = 0.065,
    allow_synthetic: bool = False,
    progress: Callable[[date], None] | None = None,
) -> ReplayBacktestResult:
    probe = ReplayProvider(directory, SimClock(datetime.fromisoformat("2000-01-01T00:00:00+00:00")))
    if probe.source is Source.SYNTHETIC and not allow_synthetic:
        raise ValueError("synthetic dataset: pass allow_synthetic=True for a pipeline check (it measures no edge)")
    days = [d for d in probe.days[1:] if cal.is_trading_day(d)]  # day 1 has no prior day for levels
    days = [d for d in days if (start is None or d >= start) and (end is None or d <= end)]
    if not days:
        raise ValueError("no trading days of the dataset fall in the requested range")
    symbol = probe.index_key.symbol
    fut_key = None
    clock = SimClock(cal.session_bounds(days[0])[0])
    p = ReplayProvider(directory, clock)
    history = _MemoryHistory()
    logger = SignalLogger(history)
    risk = risk or RiskConfig()
    snapshots = 0
    for d in days:
        if progress:
            progress(d)
        o, c = cal.session_bounds(d)
        t = o + FIRST_STEP
        prev_chains: dict[date, Sourced[OptionChain]] = {}
        chains_now: dict[date, Sourced[OptionChain]] = {}
        while t <= c - LAST_ENTRY:
            clock.set(t)
            try:
                inputs = await gather_inputs(p, symbol, t, prev_chains=prev_chains)
            except DataUnavailable:
                t += step
                continue
            # the store keeps the previous chain per expiry when a newer one arrives - do the same
            for e, ch in inputs.chains.items():
                old = chains_now.get(e)
                if old is not None and old.value.ts < ch.value.ts:
                    prev_chains[e] = old
                chains_now[e] = ch
            snap = build_snapshot(inputs, t, rate=rate, version=snapshots)
            snap = snap.model_copy(update={"signal": fuse(snap, risk)})
            await logger.maybe_log(snap)
            snapshots += 1
            t += step

    # score with a provider that can see the whole dataset (scoring only - never feeds a signal)
    last = cal.session_bounds(probe.days[-1])[1]
    scorer = ReplayProvider(directory, SimClock(last + timedelta(minutes=1)))
    trades: list[Trade] = []
    logged = history.signals
    flat_from: datetime | None = None  # no position before this time
    for i, sig in enumerate(logged):
        if sig.status != "active" or sig.sizing is None or (flat_from is not None and sig.ts < flat_from):
            continue
        end_t = horizon_end(sig)
        if end_t > last:
            continue
        cancel_at = logged[i + 1].ts if i + 1 < len(logged) else None  # replaced or withdrawn then
        fut_key = InstrumentKey.parse(sig.sizing.instrument)
        try:
            fut_bars = (await scorer.get_candles(fut_key, Interval.M1, sig.ts - timedelta(minutes=1), end_t)).value
        except DataUnavailable:
            continue
        out, tr = _trade(sig, fut_bars, cancel_at)
        if tr is None or out is None or out.outcome == "not_filled":
            continue
        trades.append(tr)
        # busy until the exit (the bar after the stop/target bar), or the horizon close if still open
        flat_from = out.exit_ts + timedelta(minutes=1) if out.exit_ts is not None else end_t

    filled = trades
    n_t = sum(x.outcome == "target" for x in filled)
    n_s = sum(x.outcome == "stop" for x in filled)
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for x in sorted(trades, key=lambda x: x.ts):
        equity += x.pnl_inr
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    notes = [
        "one position at a time; an unfilled entry is replaced by the next logged signal or cancelled",
        "trades scored on the front future: entry/stop/target = signal levels + basis at signal time",
        "P&L is after the costs and slippage the signal budgeted (RiskConfig)",
        "news not included: no news poll in a replay backtest (the live engine also uses news)",
        f"clock step {int(step.total_seconds())} s; first {int(FIRST_STEP.total_seconds() // 60)} min and last "
        f"{int(LAST_ENTRY.total_seconds() // 60)} min of each session skipped",
    ]
    if probe.source is Source.SYNTHETIC:
        notes.insert(0, "SYNTHETIC DATA: this checks the pipeline end to end; it says nothing about a real edge")
    if len(filled) < 30:
        notes.append(f"only {len(filled)} filled trades - treat every rate as anecdotal")
    return ReplayBacktestResult(
        symbol=symbol,
        rules_version=RULES_VERSION,
        source=probe.source,
        start=days[0],
        end=days[-1],
        days=len(days),
        snapshots=snapshots,
        signals_logged=len(history.signals),
        active=sum(s.status == "active" for s in history.signals),
        filled=len(filled),
        targets=n_t,
        stops=n_s,
        open=sum(x.outcome == "open" for x in filled),
        win_rate=n_t / (n_t + n_s) if n_t + n_s else None,
        expectancy_points=sum(x.pnl_points for x in filled) / len(filled) if filled else None,
        total_pnl_inr=sum(x.pnl_inr for x in trades),
        max_drawdown_inr=max_dd,
        trades=trades,
        notes=notes,
    )
