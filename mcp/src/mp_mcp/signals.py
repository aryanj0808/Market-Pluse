"""signals-mcp: expected range, levels, the fused signal, a one-call market snapshot, backtests and
signal statistics. Read-only; every number is computed by mp-core."""

from __future__ import annotations

from datetime import date, timedelta

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from mp_core.analytics.expected_move import expected_move
from mp_core.digest import levels_view, market_digest_data, signal_view, snapshot_parts
from mp_core.settings import get_settings
from mp_core.signals.backtest import STRATEGIES, run_backtest
from mp_core.signals.evaluation import compute_stats
from mp_mcp.common import Deps, check_symbol, dumps, envelope, require_snapshot, serve

INSTRUCTIONS = (
    "Precomputed analysis for NSE indices. get_market_snapshot answers most questions in one call. "
    "Signals come from a transparent rule set (fusion-v1): each reason cites the snapshot input that "
    "produced it - quote those reasons rather than inventing your own. A status of 'no_signal' is a "
    "valid answer; give its no_signal_reasons. If source is 'synthetic' or stale is true, say so. This "
    "is an analysis tool: never present a signal as a recommendation to trade."
)


def create_server(deps: Deps) -> MCPServer:
    mcp = MCPServer("signals-mcp", instructions=INSTRUCTIONS)

    @mcp.tool()
    async def get_market_snapshot(symbol: str = "NSE:BANKNIFTY") -> str:
        """Everything in one call, read from the cache: quote and future, expected range (session /
        next session / expiry), IV term structure, PCR and max pain, levels within 2 sigma, news score
        with top items, the fused signal (zones, stop, targets, reasons), top constituent movers,
        macro, and which sections are stale or missing."""
        key = check_symbol(symbol)
        snap = await require_snapshot(deps, key.symbol)
        data = market_digest_data(snap)
        return dumps(envelope(key.symbol, snapshot_parts(snap), data, await deps.store.now()))

    @mcp.tool()
    async def get_signal(symbol: str = "NSE:BANKNIFTY") -> str:
        """The current fused signal: status (active / no_signal), bias, composite score and
        confidence, buy and sell zones, entry, stop loss, invalidation level, targets, reward:risk,
        position sizing for the configured loss budget, every reason with the input it came from,
        and the reasons there is no signal (stale or conflicting inputs, poor reward:risk...)."""
        key = check_symbol(symbol)
        snap = await require_snapshot(deps, key.symbol)
        if snap.signal is None:
            raise ToolError("no signal in the snapshot yet")
        return dumps(envelope(key.symbol, snapshot_parts(snap), signal_view(snap), await deps.store.now()))

    @mcp.tool()
    async def get_expected_range(symbol: str = "NSE:BANKNIFTY", days: float = 1.0) -> str:
        """Expected move over `days` calendar days (0.1-60): spot x IV x sqrt(days/365), with 1-sigma
        and 2-sigma bands. IV is the ATM implied vol of the first expiry at least `days` away (else
        the longest listed)."""
        key = check_symbol(symbol)
        if not 0.1 <= days <= 60:
            raise ToolError("days must be between 0.1 and 60")
        snap = await require_snapshot(deps, key.symbol)
        exps = [e for e in snap.expiries if e.value.iv.atm_iv]
        if not exps:
            raise ToolError("no expiry with an identifiable ATM IV")
        pick = next((e for e in exps if e.value.days_to_expiry >= days), exps[-1])
        em = expected_move(snap.quote.value.ltp, pick.value.iv.atm_iv, days)
        data = {
            **em.model_dump(mode="json"),
            "iv_expiry": str(pick.value.expiry),
            "iv_expiry_days": pick.value.days_to_expiry,
        }
        return dumps(envelope(key.symbol, [snap.quote, pick], data, await deps.store.now()))

    @mcp.tool()
    async def get_levels(symbol: str = "NSE:BANKNIFTY") -> str:
        """Key levels nearest first: prior-day high/low/close, classic pivots and CPR, session VWAP
        bands, highest call/put OI strikes and largest OI additions. role_now says whether each level
        is currently above price (resistance) or below (support), whatever its design name."""
        key = check_symbol(symbol)
        snap = await require_snapshot(deps, key.symbol)
        if snap.levels is None:
            raise ToolError("levels unavailable: " + "; ".join(e for e in snap.errors if e.startswith("levels")))
        data = {"spot": snap.quote.value.ltp, "levels": levels_view(snap)}
        return dumps(envelope(key.symbol, [snap.quote, snap.levels], data, await deps.store.now()))

    @mcp.tool()
    async def backtest(strategy: str, start: str, end: str, symbol: str = "NSE:BANKNIFTY") -> str:
        """Backtest on real NSE F&O bhavcopy history (EOD). strategy: 'expected_range' (next-day
        close inside the IV-implied 1/2-sigma range?) or 'oi_levels' (do the highest call/put OI
        strikes hold as next-day resistance/support?). start/end: YYYY-MM-DD. Refuses synthetic data."""
        key = check_symbol(symbol)
        if strategy not in STRATEGIES:
            raise ToolError(f"strategy must be one of {list(STRATEGIES)}")
        try:
            d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
        except ValueError:
            raise ToolError("start and end must be YYYY-MM-DD") from None
        if d1 < d0 or (d1 - d0).days > 3 * 366:
            raise ToolError("end must be after start, and the window at most 3 years")
        hist = await deps.history()
        records = await hist.fo_eod(key.underlying, d0 - timedelta(days=7), d1 + timedelta(days=7))
        if not records:
            raise ToolError(
                f"no NSE bhavcopy data loaded for {key.underlying} {start}..{end} - load files with "
                "`mp bhavcopy load <zip>`"
            )
        res = run_backtest(strategy, records, key.underlying, d0, d1, get_settings().risk_free_rate)
        return dumps({"symbol": key.symbol, "source": res.source, "data": res.model_dump(mode="json")})

    @mcp.tool()
    async def get_signal_stats(symbol: str = "NSE:BANKNIFTY", days: int = 90) -> str:
        """How past signals actually did (real data only): hit rate (target before stop), fill rate,
        average MFE/MAE in points, and how often the close stayed inside the 1/2-sigma expected range
        (fair IV gives ~68%/95%). Synthetic signals are excluded and counted, never scored."""
        key = check_symbol(symbol)
        if not 1 <= days <= 3650:
            raise ToolError("days must be 1-3650")
        hist = await deps.history()
        now = await deps.store.now()
        since = now - timedelta(days=days)
        stats = compute_stats(
            key.symbol,
            await hist.outcomes(key.symbol, since),
            since,
            n_synthetic_signals=await hist.count_synthetic_signals(key.symbol),
        )
        return dumps({"symbol": key.symbol, "now": now.isoformat(), "data": stats.model_dump(mode="json")})

    return mcp


def main() -> None:
    serve(create_server, default_port=8104)
