"""news-mcp: classified news and the deterministic news score. Read-only."""

from __future__ import annotations

from datetime import timedelta

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from mp_core.news.models import Impact
from mp_core.news.score import entity_weights, news_score, relevant_items
from mp_core.news.store import NewsStore
from mp_core.registry import get_index
from mp_mcp.common import Deps, check_symbol, dumps, envelope, serve

INSTRUCTIONS = (
    "News for NSE indices. Each item was classified once by Claude Haiku (sentiment -1..1, impact, "
    "horizon, entities, reason); `model` says which classifier labelled it - 'fixture-labels' means a "
    "synthetic test label, not an LLM. The news score is computed by deterministic code: "
    "sum(weight*sentiment)/(sum(weight)+1), weight = impact x time decay x index relevance, where "
    "relevance is the constituent's index weight (or a per-index weight for sector/macro tags). "
    "Cite items by title and published_at; never invent news."
)


def create_server(deps: Deps) -> MCPServer:
    mcp = MCPServer("news-mcp", instructions=INSTRUCTIONS)
    news = NewsStore(deps.store.kv)

    async def _weights(symbol: str) -> dict[str, float]:
        cons = await deps.store.get_constituents(symbol)
        live = {c.key.underlying: c.weight for c in cons.value} if cons else None
        return entity_weights(get_index(symbol), live)

    async def _poll():
        poll = await news.last_poll()
        if poll is None:
            raise ToolError("no news poll has completed yet - is ingest-news running?")
        return poll

    @mcp.tool()
    async def get_news(symbol: str = "NSE:BANKNIFTY", hours: int = 24, min_impact: str = "low") -> str:
        """Classified news relevant to the index from the last `hours` (1-168), newest first, with
        sentiment, impact (low/med/high), horizon, entities, the classifier's reason, and this
        index's relevance weight. min_impact filters by impact. Also reports how many items are
        still unclassified or were dropped, and per-feed health."""
        key = check_symbol(symbol)
        if not 1 <= hours <= 168:
            raise ToolError("hours must be 1-168")
        try:
            Impact(min_impact)
        except ValueError:
            raise ToolError("min_impact must be low, med or high") from None
        poll = await _poll()
        now = await deps.store.now()
        items = await news.recent(now - timedelta(hours=hours), now)
        weights = await _weights(key.symbol)
        rows = [
            {k: v for k, v in r.items() if k not in ("id", "source")}
            for r in relevant_items(items, weights, Impact(min_impact))
        ]
        health = [
            {
                "feed": h.feed,
                "ok": h.last_error is None,
                "last_ok": h.last_ok.isoformat() if h.last_ok else None,
                "error": h.last_error,
                "verified": h.verified,
            }
            for h in await news.get_health()
        ]
        data = {
            "window_hours": hours,
            "items": rows,
            "unclassified": sum(s.status == "unclassified" for s in items),
            "dropped": sum(s.status == "dropped" for s in items),
            "duplicates_collapsed": sum(s.status == "duplicate" for s in items),
            "feeds": health,
        }
        return dumps(envelope(key.symbol, [poll], data, now))

    @mcp.tool()
    async def get_news_score(symbol: str = "NSE:BANKNIFTY") -> str:
        """The index's news score in [-1, 1] (label bearish/neutral/bullish at +/-0.15) with the
        items that contributed most and each one's weight, decay and relevance."""
        key = check_symbol(symbol)
        poll = await _poll()
        now = await deps.store.now()
        snap = await deps.store.get_snapshot(key.symbol)
        if snap is not None and snap.news is not None:
            ns, part = snap.news.value, snap.news
        else:  # engine not running for this symbol: same scoring function, computed here
            items = await news.recent(now - timedelta(days=20), now)
            ns, part = news_score(key.symbol, items, await _weights(key.symbol), now), poll
        return dumps(envelope(key.symbol, [part], ns, now))

    return mcp


def main() -> None:
    serve(create_server, default_port=8103)
