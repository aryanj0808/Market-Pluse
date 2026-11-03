"""Scoring past signals against what the market actually did. Refuses synthetic data.

Per signal, once its horizon has closed and 1-minute index bars cover it:
  * range containment: was the close at the horizon inside spot +/- 1 sigma and +/- 2 sigma?
  * for active signals, a conservative trade walk over the bars after the signal:
      - filled only if price reaches the entry (bullish: low <= entry; bearish: high >= entry)
      - after the fill, a bar touching both stop and target counts as the stop (worst case)
      - in the bar that fills, only the stop can be hit: which came first inside that minute -
        the fill or the target - is unknown, so a target there is not credited
      - outcome target | stop | open (neither by the horizon close) | not_filled
      - MFE / MAE: best / worst excursion from entry after the fill, in index points
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Literal, Protocol

from mp_core.models import Candle, Source, StrictModel
from mp_core.signals.fusion import horizon_end
from mp_core.signals.models import Signal

OutcomeKind = Literal["target", "stop", "open", "not_filled", "no_trade"]


class SyntheticDataRefused(ValueError):
    """Evaluation never produces statistics from synthetic data."""


class Outcome(StrictModel):
    signal_id: str
    horizon_end: datetime
    outcome: OutcomeKind
    hit: bool | None  # target before stop; None when not decided (open / not filled / no trade)
    mfe: float | None
    mae: float | None
    close_at_horizon: float
    contained_1s: bool
    contained_2s: bool
    source: Source
    fill_ts: datetime | None = None  # open time of the bar that filled the entry
    exit_ts: datetime | None = None  # open time of the bar that hit the stop or target


def score_signal(
    signal: Signal, bars: Sequence[Candle], *, allow_synthetic: bool = False, cancel_at: datetime | None = None
) -> Outcome | None:
    """None until the horizon has closed and the bars reach it. (Bar provenance is checked by the
    caller with `check_bars_real` - candles themselves do not carry a source.) `allow_synthetic` is
    for pipeline checks on synthetic replays only; such outcomes stay labelled synthetic. `cancel_at`: the
    entry order is withdrawn then if it has not filled (the signal changed or ended)."""
    if signal.source is Source.SYNTHETIC and not allow_synthetic:
        raise SyntheticDataRefused(f"signal {signal.signal_id} is synthetic - not scored")
    end = horizon_end(signal)
    after = sorted((b for b in bars if signal.ts <= b.ts < end), key=lambda b: b.ts)
    if not after or after[-1].ts + timedelta(minutes=1) < end:
        return None
    close = after[-1].close
    sig = signal.sigma_points
    contained_1s = abs(close - signal.spot) <= sig
    contained_2s = abs(close - signal.spot) <= 2 * sig

    if signal.status != "active" or signal.entry is None or signal.stop_loss is None or not signal.targets:
        return Outcome(
            signal_id=signal.signal_id,
            horizon_end=end,
            outcome="no_trade",
            hit=None,
            mfe=None,
            mae=None,
            close_at_horizon=close,
            contained_1s=contained_1s,
            contained_2s=contained_2s,
            source=signal.source,
        )
    up = signal.bias == "bullish"
    entry, stop, target = signal.entry, signal.stop_loss, signal.targets[0].price
    filled, outcome, mfe, mae = False, "open", 0.0, 0.0
    fill_ts = exit_ts = None
    for b in after:
        fill_bar = False
        if not filled:
            if cancel_at is not None and b.ts >= cancel_at:
                break
            if (b.low <= entry) if up else (b.high >= entry):
                filled = fill_bar = True
                fill_ts = b.ts
            else:
                continue
        fav = (b.high - entry) if up else (entry - b.low)
        adv = (entry - b.low) if up else (b.high - entry)
        mfe, mae = max(mfe, fav), max(mae, adv)
        hit_stop = (b.low <= stop) if up else (b.high >= stop)
        hit_target = not fill_bar and ((b.high >= target) if up else (b.low <= target))
        if hit_stop or hit_target:
            outcome, exit_ts = ("stop" if hit_stop else "target"), b.ts
            break
    if not filled:
        return Outcome(
            signal_id=signal.signal_id,
            horizon_end=end,
            outcome="not_filled",
            hit=None,
            mfe=None,
            mae=None,
            close_at_horizon=close,
            contained_1s=contained_1s,
            contained_2s=contained_2s,
            source=signal.source,
        )
    return Outcome(
        signal_id=signal.signal_id,
        horizon_end=end,
        outcome=outcome,
        hit={"target": True, "stop": False}.get(outcome),
        mfe=mfe,
        mae=mae,
        close_at_horizon=close,
        contained_1s=contained_1s,
        contained_2s=contained_2s,
        source=signal.source,
        fill_ts=fill_ts,
        exit_ts=exit_ts,
    )


def as_future_trade(signal: Signal) -> Signal | None:
    """The same signal with entry, stop and targets moved onto the future it would be traded on
    (the levels plus the basis captured at signal time), so it can be scored on futures bars.
    None when the signal carries no futures prices."""
    sz = signal.sizing
    if sz is None or sz.future_entry is None or sz.future_stop_loss is None or signal.entry is None:
        return None
    basis = sz.future_entry - sz.entry
    return signal.model_copy(
        update={
            "entry": sz.future_entry,
            "stop_loss": sz.future_stop_loss,
            "targets": [t.model_copy(update={"price": t.price + basis}) for t in signal.targets],
            "spot": signal.spot + basis,
        }
    )


def check_bars_real(bar_sources: set[str]) -> None:
    if "synthetic" in bar_sources:
        raise SyntheticDataRefused("price history for this window is synthetic - not scored")


class SignalStats(StrictModel):
    symbol: str
    since: datetime | None
    n_scored: int
    n_excluded_synthetic: int
    n_active: int
    n_filled: int
    n_target: int
    n_stop: int
    n_open: int
    hit_rate: float | None  # target / (target + stop)
    fill_rate: float | None
    avg_mfe: float | None
    avg_mae: float | None
    containment_1s: float | None  # expected ~0.68 if the implied vol is fair
    containment_2s: float | None  # expected ~0.95
    message: str


def compute_stats(
    symbol: str, outcomes: Sequence[Outcome], since: datetime | None = None, n_synthetic_signals: int = 0
) -> SignalStats:
    """Stats over real outcomes only. Synthetic outcomes are excluded and counted, never averaged in."""
    synthetic = [o for o in outcomes if o.source is Source.SYNTHETIC]
    real = [o for o in outcomes if o.source is not Source.SYNTHETIC]
    n_syn = len(synthetic) + n_synthetic_signals
    if not real:
        msg = (
            f"refusing to report statistics: all {n_syn} signals so far are synthetic"
            if n_syn
            else "no scored signals yet"
        )
        return SignalStats(
            symbol=symbol,
            since=since,
            n_scored=0,
            n_excluded_synthetic=n_syn,
            n_active=0,
            n_filled=0,
            n_target=0,
            n_stop=0,
            n_open=0,
            hit_rate=None,
            fill_rate=None,
            avg_mfe=None,
            avg_mae=None,
            containment_1s=None,
            containment_2s=None,
            message=msg,
        )
    active = [o for o in real if o.outcome != "no_trade"]
    filled = [o for o in active if o.outcome != "not_filled"]
    t = sum(o.outcome == "target" for o in filled)
    s = sum(o.outcome == "stop" for o in filled)

    def avg(xs: list[float]) -> float | None:
        return sum(xs) / len(xs) if xs else None

    n = len(real)
    return SignalStats(
        symbol=symbol,
        since=since,
        n_scored=n,
        n_excluded_synthetic=n_syn,
        n_active=len(active),
        n_filled=len(filled),
        n_target=t,
        n_stop=s,
        n_open=sum(o.outcome == "open" for o in filled),
        hit_rate=t / (t + s) if t + s else None,
        fill_rate=len(filled) / len(active) if active else None,
        avg_mfe=avg([o.mfe for o in filled if o.mfe is not None]),
        avg_mae=avg([o.mae for o in filled if o.mae is not None]),
        containment_1s=sum(o.contained_1s for o in real) / n,
        containment_2s=sum(o.contained_2s for o in real) / n,
        message=f"{n} real signals scored"
        + (f"; {n_syn} synthetic excluded" if n_syn else "")
        + ("" if n >= 30 else " - fewer than 30, treat rates as anecdotal"),
    )


class EvalHistory(Protocol):
    async def unscored_signals(self, symbol: str, until: datetime, include_synthetic: bool = False) -> list[Signal]: ...
    async def candles(self, key, interval, start, end, now): ...
    async def record_outcome(self, o: Outcome) -> None: ...
    async def outcomes(self, symbol: str, since: datetime | None = None) -> list[Outcome]: ...
    async def count_synthetic_signals(self, symbol: str) -> int: ...


async def evaluate_pending(history: EvalHistory, symbol: str, now: datetime) -> dict[str, int]:
    """Score every real signal whose horizon has closed. Synthetic signals are never fetched;
    windows whose price history is synthetic are refused and counted."""
    from mp_core.models import InstrumentKey, Interval

    key = InstrumentKey.parse(symbol)
    stats = {"pending": 0, "scored": 0, "not_ready": 0, "refused_synthetic_bars": 0}
    for sig in await history.unscored_signals(symbol, now):
        stats["pending"] += 1
        end = horizon_end(sig)
        if end > now:
            stats["not_ready"] += 1
            continue
        bars, sources = await history.candles(key, Interval.M1, sig.ts - timedelta(minutes=1), end, now)
        try:
            check_bars_real(sources)
            outcome = score_signal(sig, bars)
        except SyntheticDataRefused:
            stats["refused_synthetic_bars"] += 1
            continue
        if outcome is None:
            stats["not_ready"] += 1
            continue
        await history.record_outcome(outcome)
        stats["scored"] += 1
    return stats
