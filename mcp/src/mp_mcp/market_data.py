"""market-data-mcp: quotes, candles, constituents, macro. Read-only."""

from __future__ import annotations

from datetime import timedelta

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from mp_core.models import Interval
from mp_mcp.common import Deps, check_symbol, dumps, envelope, require_snapshot, serve

INSTRUCTIONS = (
    "Market data for NSE indices (NSE:BANKNIFTY, NSE:NIFTY, NSE:FINNIFTY). Every response has "
    "source, as_of and stale - if stale is true or source is 'synthetic', say so when you use the "
    "numbers. Never estimate a number that is not in a tool response."
)


def create_server(deps: Deps) -> MCPServer:
    mcp = MCPServer("market-data-mcp", instructions=INSTRUCTIONS)

    @mcp.tool()
    async def get_quote(symbol: str = "NSE:BANKNIFTY") -> str:
        """Latest index quote (ltp, day open/high/low, previous close, % change) plus the front-month
        future (ltp, basis vs spot, open interest)."""
        key = check_symbol(symbol)
        snap = await require_snapshot(deps, key.symbol)
        q = snap.quote.value
        data = {
            "index": {
                "ltp": q.ltp,
                "open": q.open,
                "high": q.high,
                "low": q.low,
                "prev_close": q.prev_close,
                "change": q.change,
                "change_pct": q.change_pct,
                "ts": q.ts.isoformat(),
            },
        }
        parts = [snap.quote]
        if snap.future is not None:
            f = snap.future.value
            data["future"] = {"instrument": f.key.id, "ltp": f.ltp, "basis_points": f.basis_points, "oi": f.oi}
            parts.append(snap.future)
        return dumps(envelope(key.symbol, parts, data, await deps.store.now()))

    @mcp.tool()
    async def get_candles(symbol: str = "NSE:BANKNIFTY", interval: str = "15m", days: int = 1) -> str:
        """OHLC candles from recorded history. interval: 1m, 5m, 15m, 1h or 1d. days: calendar days
        back from now (1-30). Index candles have no volume; only completed bars are returned."""
        key = check_symbol(symbol)
        try:
            iv = Interval(interval)
        except ValueError:
            raise ToolError(f"interval must be one of {[i.value for i in Interval]}") from None
        if not 1 <= days <= 30:
            raise ToolError("days must be between 1 and 30")
        if iv is Interval.M1 and days > 2:
            raise ToolError("1m candles are limited to 2 days; use a larger interval")
        now = await deps.store.now()
        hist = await deps.history()
        bars, sources = await hist.candles(key, iv, now - timedelta(days=days), now, now)
        if not bars:
            raise ToolError(f"no recorded {interval} candles for {key.symbol} in the last {days} day(s)")
        source = "synthetic" if "synthetic" in sources else (sources.pop() if len(sources) == 1 else "derived")
        last_close = bars[-1].ts + (timedelta(minutes=iv.minutes) if iv is not Interval.D1 else timedelta(0))
        # a completed bar is current until the next one is due: interval + the usual 2-minute grace
        ttl = (iv.minutes * 60 + 120) if iv is not Interval.D1 else 86400 * 4
        rows = [
            [b.ts.isoformat(), round(b.open, 2), round(b.high, 2), round(b.low, 2), round(b.close, 2), b.volume]
            for b in bars
        ]
        return dumps(
            {
                "symbol": key.symbol,
                "source": source,
                "as_of": last_close.isoformat(),
                "stale": (now - last_close).total_seconds() > ttl,
                "now": now.isoformat(),
                "data": {
                    "interval": interval,
                    "columns": ["ts", "open", "high", "low", "close", "volume"],
                    "rows": rows,
                },
            }
        )

    @mcp.tool()
    async def get_constituents(symbol: str = "NSE:BANKNIFTY") -> str:
        """Index constituents with weight (%), ltp, % change and contribution to the index move in
        percentage points (weight * change / 100), sorted by weight."""
        key = check_symbol(symbol)
        snap = await require_snapshot(deps, key.symbol)
        if snap.constituents is None:
            raise ToolError(f"constituents unavailable for {key.symbol}")
        rows = [
            {
                "symbol": c.key.underlying,
                "name": c.name,
                "weight_pct": c.weight,
                "ltp": c.ltp,
                "change_pct": c.change_pct,
                "contribution_pct_pts": c.weight * c.change_pct / 100,
            }
            for c in sorted(snap.constituents.value, key=lambda c: -c.weight)
        ]
        return dumps(envelope(key.symbol, [snap.constituents], {"constituents": rows}, await deps.store.now()))

    @mcp.tool()
    async def get_macro() -> str:
        """India VIX, USD/INR, India 10-year yield (%), FII and DII cash-market net flows (INR crore).
        Each field has its own as_of: flows are end-of-day figures."""
        m = await deps.store.get_macro()
        if m is None:
            raise ToolError("macro data unavailable")
        now = await deps.store.now()
        fields = {k: v.restamp(now) for k, v in m if v is not None}
        data = {
            k: {"value": v.value, "as_of": v.as_of.isoformat(), "stale": v.stale, "source": v.source.value}
            for k, v in fields.items()
        }
        return dumps(envelope("macro", list(fields.values()), data, now))

    return mcp


def main() -> None:
    serve(create_server, default_port=8101)
