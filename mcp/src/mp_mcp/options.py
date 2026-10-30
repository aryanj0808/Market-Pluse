"""options-mcp: option chain, IV summary, OI build-up, PCR / max pain. Read-only."""

from __future__ import annotations

from datetime import date

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from mp_core.analytics.options import option_mark
from mp_core.snapshot import ExpiryView, MarketSnapshot
from mp_mcp.common import Deps, check_symbol, dumps, envelope, require_snapshot, serve

INSTRUCTIONS = (
    "Options analytics for NSE indices, precomputed by deterministic code. IVs are annualised "
    "decimals (0.18 = 18%). OI is in units (contracts x lot size). An IV of null means it is not "
    "identifiable (expired, at minimum tick, or no time value) - do not substitute a guess."
)


def _pick_expiry(snap: MarketSnapshot, expiry: str | None):
    if not snap.expiries:
        raise ToolError(f"no option expiries in snapshot for {snap.symbol}")
    if expiry is None:
        return snap.expiries[0]
    try:
        want = date.fromisoformat(expiry)
    except ValueError:
        raise ToolError("expiry must be YYYY-MM-DD") from None
    for e in snap.expiries:
        if e.value.expiry == want:
            return e
    raise ToolError(f"expiry {expiry} not available; have {[str(e.value.expiry) for e in snap.expiries]}")


def _iv_brief(v: ExpiryView) -> dict:
    return {
        "expiry": str(v.expiry),
        "days": v.days_to_expiry,
        "atm_iv": v.iv.atm_iv,
        "skew_25d": v.iv.skew_25d,
        "forward": v.iv.forward,
        "iv_solved": v.iv.n_ok,
        "iv_unidentifiable": v.iv.n_failed,
    }


def create_server(deps: Deps) -> MCPServer:
    mcp = MCPServer("options-mcp", instructions=INSTRUCTIONS)

    @mcp.tool()
    async def get_option_chain(
        symbol: str = "NSE:BANKNIFTY", expiry: str | None = None, strikes_each_side: int = 10
    ) -> str:
        """Option chain around ATM: per strike, call and put mark price, OI, OI change vs previous
        day, volume and implied vol. expiry YYYY-MM-DD (default: nearest). strikes_each_side 1-30."""
        key = check_symbol(symbol)
        if not 1 <= strikes_each_side <= 30:
            raise ToolError("strikes_each_side must be 1-30")
        snap = await require_snapshot(deps, key.symbol)
        ev = _pick_expiry(snap, expiry)
        chain = await deps.store.get_chain(key.symbol, ev.value.expiry)
        if chain is None:
            raise ToolError(f"chain {ev.value.expiry} not in cache")
        ivs = {s.strike: s for s in ev.value.iv.strikes}
        rows = sorted(chain.value.rows, key=lambda r: abs(r.strike - chain.value.spot))[: 2 * strikes_each_side + 1]
        out = []
        for r in sorted(rows, key=lambda r: r.strike):
            s = ivs.get(r.strike)
            row: dict = {"strike": r.strike}
            for side, q, iv in (("ce", r.call, s and s.call_iv), ("pe", r.put, s and s.put_iv)):
                if q is not None:
                    row[side] = {
                        "price": option_mark(q),
                        "oi": q.oi,
                        "oi_change": q.oi_change,
                        "volume": q.volume,
                        "iv": iv,
                    }
            out.append(row)
        data = {
            "expiry": str(chain.value.expiry),
            "spot": chain.value.spot,
            "chain_ts": chain.value.ts.isoformat(),
            "rows": out,
        }
        return dumps(envelope(key.symbol, [chain], data, await deps.store.now()))

    @mcp.tool()
    async def get_iv_summary(symbol: str = "NSE:BANKNIFTY") -> str:
        """ATM implied vol, 25-delta skew (put IV minus call IV; positive = puts richer) and days to
        expiry for each listed expiry - i.e. the IV term structure."""
        key = check_symbol(symbol)
        snap = await require_snapshot(deps, key.symbol)
        if not snap.expiries:
            raise ToolError(f"no option expiries for {key.symbol}")
        data = {"term_structure": [_iv_brief(e.value) for e in snap.expiries]}
        return dumps(envelope(key.symbol, list(snap.expiries), data, await deps.store.now()))

    @mcp.tool()
    async def get_oi_buildup(symbol: str = "NSE:BANKNIFTY") -> str:
        """Open-interest build-up: front-month futures classified per 15-minute bar (long build-up,
        short build-up, short covering, long unwinding, neutral), plus per-expiry option counts by
        category since the previous chain snapshot and the strikes with the largest OI change."""
        key = check_symbol(symbol)
        snap = await require_snapshot(deps, key.symbol)
        parts, data = [], {}
        if snap.future is not None:
            parts.append(snap.future)
            data["futures_15m"] = [
                {
                    "end": o.end.isoformat(),
                    "price_change_pct": o.price_change_pct,
                    "oi_change_pct": o.oi_change_pct,
                    "buildup": o.buildup.value,
                }
                for o in snap.future.value.buildup_15m
            ]
        opts = []
        for e in snap.expiries:
            bu = e.value.buildup
            if bu is None:
                continue
            parts.append(e)
            top = sorted(bu.rows, key=lambda r: -abs(r.oi_change))[:6]
            opts.append(
                {
                    "expiry": str(e.value.expiry),
                    "window": [bu.start.isoformat(), bu.end.isoformat()],
                    "counts": bu.counts,
                    "largest_oi_changes": [
                        {
                            "strike": r.strike,
                            "type": r.option_type.value,
                            "oi_change": r.oi_change,
                            "price_change_pct": r.price_change_pct,
                            "buildup": r.buildup.value,
                        }
                        for r in top
                    ],
                }
            )
        data["options"] = opts
        if not parts:
            raise ToolError(f"no build-up data for {key.symbol} yet")
        return dumps(envelope(key.symbol, parts, data, await deps.store.now()))

    @mcp.tool()
    async def get_pcr_maxpain(symbol: str = "NSE:BANKNIFTY", expiry: str | None = None) -> str:
        """Put-call ratio (by OI, volume and OI change) and max pain strike for one expiry
        (YYYY-MM-DD, default nearest). Covers the strikes in the cached chain only."""
        key = check_symbol(symbol)
        snap = await require_snapshot(deps, key.symbol)
        ev = _pick_expiry(snap, expiry)
        p = ev.value.pcr
        data = {
            "expiry": str(p.expiry),
            "pcr_oi": p.pcr_oi,
            "pcr_volume": p.pcr_volume,
            "pcr_oi_change": p.pcr_oi_change,
            "max_pain": p.max_pain,
            "total_call_oi": p.total_call_oi,
            "total_put_oi": p.total_put_oi,
            "spot": snap.quote.value.ltp,
        }
        return dumps(envelope(key.symbol, [ev, snap.quote], data, await deps.store.now()))

    return mcp


def main() -> None:
    serve(create_server, default_port=8102)
