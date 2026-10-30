# Market Pulse

Index analysis & signal platform for NSE:BANKNIFTY / NIFTY / FINNIFTY. Analysis only - no order placement.

```
packages/core/   models, providers, analytics, snapshot engine, stores (all business logic, all numbers)
services/        ingest-market, engine (3), ingest-news (4), evaluator (5), gateway (6), alerts (7)
mcp/             market-data-mcp, options-mcp (Phase 3), news-mcp (Phase 4), signals-mcp (Phase 5)
web/             React + Vite dashboard, served by the gateway                    (Phase 6)
scripts/         probe_stream.py - measures the stream the way a browser sees it
data/replay/     replay datasets (git-ignored; regenerate with `mp synth`)
infra/           docker-compose.yml, TimescaleDB schema + migrations
```

## How data flows

```
MarketDataProvider ──► ingest-market ──► Redis (raw cache + shared clock) ──► engine ──► Redis snapshot
 (replay / broker)          │                                                              │
                            └──► TimescaleDB (ticks, 1m candles, option chains)            ├──► MCP servers
                                                                                           └──► gateway ──► dashboard
```

The engine recomputes the whole `MarketSnapshot` (IV, expected range, levels, PCR/max pain, OI build-up,
constituents, macro, news score) on every update in a few milliseconds; MCP tools and the UI only ever read it.

### News (Phase 4)

```
feeds (RSS/Atom) or synthetic fixture ──► ingest-news ──► de-duplicate ──► Claude Haiku (once per item)
                                               │                              │
                                               └──► Redis news cache ◄────────┘──► Timescale news_items
```

- **De-duplication:** canonical URL identity, plus fuzzy title matching within 6 hours. Titles that differ
  in their numbers or name different companies are never merged.
- **Classification:** `claude-haiku-5-5` returns strict JSON `{relevant, entities, sentiment, impact,
  horizon, reason}` (structured outputs, entities limited to known tickers + tags). Invalid output is
  retried once, then the item is dropped and logged. An item that is `classified` or `dropped` is never
  sent again - also enforced in Timescale, so a Redis flush cannot cause a second paid call.
- **Score (deterministic):** `sum(w*s) / (sum(w) + 1)`, with `w = impact x time decay x index relevance`.
  Relevance is the constituent's live index weight (HDFC Bank news counts ~60x Yes Bank news for
  BANKNIFTY) or a per-index weight for sector/macro tags (`indices.toml`); half-life depends on horizon.
- **Without credentials** items stay `unclassified`. During a synthetic replay the pipeline can use the
  fixture's ground-truth labels instead (`model: "fixture-labels"` - a test double, not an LLM, and it
  refuses non-synthetic news).

```powershell
uv run mp synth-news                            # synthetic news + labels for the replay dataset
uv run mp news-eval --limit 20                  # Haiku vs the labels (makes 20 API calls)
```

Settings: `MP_NEWS_MODE` (live | fixture | auto), `MP_NEWS_POLL_SECONDS`, `MP_NEWS_CLASSIFIER`
(haiku | fixture-labels | none | auto), `ANTHROPIC_API_KEY`.

### Signals, risk and evaluation (Phase 5)

The engine fuses each snapshot into a signal with a fixed, documented rule set (`signals/fusion.py`,
`rules_version: fusion-v1`):

| Component | Weight | Inputs |
|---|---|---|
| technical | 0.35 | futures vs session VWAP, spot vs CPR, spot vs prior-day range |
| options | 0.35 | PCR (OI), last 3 futures OI build-ups, pull to max pain within 2 days of expiry |
| news | 0.30 | the news score |

`composite = sum(w*s)/sum(w)` over available components; bullish/bearish beyond +/-0.20; confidence =
strength x agreement x completeness. Zones, stop, invalidation and targets come from levels judged by
their position relative to price, and from the expected range. Every reason cites the snapshot input it
came from. **No signal** when a critical input is stale or missing, when components conflict, when the
composite is neutral, when reward:risk < 1.2, or when one lot's stop distance exceeds the loss budget
(`MP_CAPITAL`, `MP_MAX_RISK_PCT`; sizing is a suggestion - nothing is ever traded).

Signals are logged to Timescale with their inputs (on every status/bias change and every 15 minutes).
The `evaluator` scores them nightly: did price fill the entry, hit target or stop first (a bar touching
both counts as the stop), MFE/MAE, and did the close stay inside the 1/2-sigma range. **Synthetic signals
are never scored** - excluded in code and rejected by a CHECK constraint - and stats say so instead of
printing numbers.

```powershell
uv run mp signal --day 2026-09-22 --time 11:15  # the fused signal at a replay moment
uv run mp bhavcopy-load data/bhavcopy/*.zip     # load real NSE F&O EOD files into fo_eod
uv run mp backtest expected_range --from 2025-01-01 --to 2025-12-31 [--zips data/bhavcopy/*.zip]
uv run mp evaluate                              # score pending real signals now
```

## Setup (macOS, one command)

```bash
bash setup_mac.sh https://github.com/<you>/market-pulse.git   # fresh Mac (download scripts/setup_mac.sh first)
bash scripts/setup_mac.sh                                       # or from inside an existing clone
```

Installs Homebrew, git, gh, uv, Python 3.12 and Docker Desktop if missing, creates `.env` (generated
database password; optional keys asked with hidden input), builds a synthetic dataset ending today, starts
all services and opens the dashboard. Re-running keeps existing `.env` values, data and volumes.

## Setup (Windows)

```powershell
uv sync                                         # Python 3.12 venv + deps
copy .env.example .env                          # then set POSTGRES_PASSWORD (and DATABASE_URL)
uv run mp synth                                 # synthetic BANKNIFTY dataset (seed 2, 25 days)
uv run mp synth-news                            # synthetic news for it
docker compose -f infra/docker-compose.yml --env-file .env up -d --build --wait
```

That starts Redis, TimescaleDB, `ingest-market` (replaying the dataset at 60x: one market minute per
second), `ingest-news`, `engine`, `evaluator`, the **dashboard on http://127.0.0.1:8000**, and the MCP
servers on `http://127.0.0.1:8101/mcp` (market data), `:8102/mcp` (options), `:8103/mcp` (news) and
`:8104/mcp` (signals).

### Dashboard and gateway (Phase 6)

The gateway (`services/.../gateway.py`, FastAPI) only reads Redis - it never waits on an LLM, an MCP
call or the engine. `GET /api/snapshot/{symbol}` returns the cached snapshot; `WS /api/stream/{symbol}`
sends it once, then **per-section diffs** (only what changed: a quote tick is ~8 KB vs a 60 KB snapshot),
at most 4 per second (any 1-second window), newest state wins. Also `/api/chain`, `/api/candles`,
`/api/news`, `/api/stats`, and the built React app on `/`.

The React app (Vite + TypeScript, Zustand, TanStack Query/Virtual, lightweight-charts, Tailwind) renders
from the WebSocket store - no polling. Unchanged sections keep their identity so memoised panels don't
re-render; the chart is fed with `series.update`; the news feed and option chain are virtualised. Panels:
price chart with levels, expected-range bands and signal lines; signal card with every reason and the
input it came from; option-chain OI/IV heatmap; news feed with impact badges; constituents heatmap; a
freshness badge on every panel; and an Accuracy page that refuses to show numbers for synthetic data.

```powershell
cd web; npm install; npm run dev                 # dev server on :5173, proxies /api to the gateway
uv run gateway                                   # gateway on :8000 (needs Redis + engine)
npm --prefix web test                            # unit + full-dashboard smoke test on real payloads
uv run python scripts/probe_stream.py            # latency / rate / diff-size measurement
```

Latency (replay at 60x, this machine): tick -> gateway send median ~145 ms, p95 ~255 ms, almost all of it
the engine's 4/s spacing (a tick can wait up to 250 ms by definition); the gateway adds ~3 ms. The
client hop can't be measured across Docker - its clock drifts against Windows by tens of ms - so the
browser measurement runs the path natively; see Phase 8 and `docs/perf.md` for the end-to-end numbers.
Replay settings: `MP_REPLAY_SPEED` (0 = as fast as possible), `MP_REPLAY_START` (ISO time, IST).

Inspect without the stack:

```powershell
uv run mp replay --time 11:00                   # what the ReplayProvider sees at 11:00 IST
uv run mp analyze --day 2026-09-23 --time 11:00 # Phase 2 analytics at that moment
```

Tests:

```powershell
uv run pytest                                   # unit tests (no infrastructure needed)
uv run pytest -m integration                    # Redis + Timescale (needs compose up)
```

Use `127.0.0.1`, not `localhost`, in URLs: on Windows `localhost` tries IPv6 first and stalls.

### Ask Claude, alerts and live data (Phase 7)

**Ask Claude** (button in the dashboard header): the question plus the current `get_market_snapshot`
digest go to `claude-sonnet-5-5` (effort `MP_ASK_EFFORT`, default `low`; server-side refusal fallback on).
The answer streams into a side panel over its own request (`POST /api/ask/{symbol}`, SSE), so it never
blocks the dashboard. Claude may only use numbers from that data and must cite the field; afterwards
**every number in the answer is checked against the data** and any it cannot find is flagged in the
panel. Questions are single-turn (fresh data each time), limited to 6/minute per client.

**Alerts** (`alerts` service, Telegram): signal activated / flipped / withdrawn, price leaving the
session's 1-sigma range, critical data stale during market hours (and recovered), new high-impact
relevant news. Cooldowns per alert, a 20/hour cap, and **no alerts from synthetic data** unless
`MP_ALERTS_SYNTHETIC=true`. Without `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` it runs dry (logs only).
Create a bot with @BotFather, send it a message, and read your chat id from
`https://api.telegram.org/bot<token>/getUpdates`.

**Upstox live data** (`MP_PROVIDER=upstox`): read-only adapter for quotes, 1m-1d candles, the option
chain, India VIX and constituents; ticks are polled once a second (the protobuf WebSocket feed is a later
upgrade). Upstox data APIs are free for account holders. Setup:

```powershell
# .env: UPSTOX_API_KEY / UPSTOX_API_SECRET from the Upstox developer console, redirect URI as registered
uv run mp upstox-login      # daily: prints the authorize URL, exchanges the code, prints the access token
uv run mp upstox-check      # verifies token, a live quote and expiries against the real API
```

Not available from Upstox and therefore reported missing (never guessed): USD/INR, bond yields,
FII/DII flows. Constituent weights still come from `indices.toml` - update them from NSE's monthly
index factsheet. **The adapter has only been tested against mocked responses** (this network blocks
Upstox); run `mp upstox-check` from an open network before trusting it.

**Zerodha Kite live data** (`MP_PROVIDER=kite`): same read-only surface. Kite has no option-chain
endpoint, so the chain is the NFO instruments ±25 strikes around spot, priced with one batched
`/quote` call (bid/ask from market depth). Kite quotes carry no previous-day OI: the adapter saves each
session's last OI to `data/kite/oi_baseline.json` and reports OI change against it (on the very first
day the change is session-relative, and the log says so). Market data needs the paid Kite Connect plan.

1. On developers.kite.trade create an app with redirect URL **`http://127.0.0.1:8000/`** (http, not
   https - nothing needs to be listening there; you only copy the URL it lands on).
2. Put `KITE_API_KEY` and `KITE_API_SECRET` in `.env` (never commit or share the secret).
3. Every morning (the token expires ~6 AM IST):

```powershell
uv run mp kite-login        # open the printed URL, log in, paste the address-bar URL back; saves KITE_ACCESS_TOKEN to .env
uv run mp kite-check        # verifies token, a live BANKNIFTY quote and expiries against the real API
# then set MP_PROVIDER=kite in .env and restart: docker compose -f infra/docker-compose.yml --env-file .env up -d
```

The `request_token` in the redirect URL is single-use and expires within minutes: paste it promptly.
If `kite-check` fails with a certificate error, the network intercepts TLS: use an open network or set
`MP_CA_BUNDLE` to IT's root certificate (PEM). Verification is never turned off. **Like Upstox, the Kite
adapter has only been tested against mocked responses so far.**

### Hardening and performance (Phase 8)

- **Health:** every background service writes a Redis heartbeat every 5 s; Docker marks a service
  unhealthy when its heartbeat is older than 30 s (stuck, not just crashed), and MCP servers are checked
  over the network. `GET /api/health` lists every service. `uv run healthcheck engine` checks one by hand.
- **Database outages never stop live data:** Timescale access reconnects with exponential backoff
  (1 s -> 60 s) and fails fast meanwhile; ingest, news and signal logging treat recording as best-effort,
  and missed 1-minute bars are written once the database is back.
- **Backoff and limits:** a failing news feed is retried at poll x 2^n (max 30 min); the gateway's REST
  API is limited to 20 req/s per client (burst 40, `429` + `Retry-After`); Ask-Claude to 6/min; Telegram
  to 20/hour; Upstox and Telegram retry on 429/5xx.
- **Latency, proven in a real browser** (`docs/perf.md`): first meaningful paint ~140-190 ms (target
  < 1.5 s), tick-to-screen p95 ~275 ms (< 300 ms), snapshot p95 < 30 ms (< 100 ms). Re-run any time:

```powershell
uv run python scripts/perf_browser.py      # or: uv run pytest -m perf   (needs Redis + Edge; ~1-2 min)
```

  The measured path (ingest, engine, gateway) runs natively on this machine so that the browser and the
  server share one clock. Nearly all of the ~275 ms is the 4-updates/s cap itself (one update every
  260 ms); the rest of the path adds 10-30 ms - so the margin under 300 ms is small by construction.

## Connecting Claude to the MCP servers

With the Docker stack running (HTTP):

```powershell
claude mcp add --transport http market-data http://127.0.0.1:8101/mcp
claude mcp add --transport http options     http://127.0.0.1:8102/mcp
claude mcp add --transport http news        http://127.0.0.1:8103/mcp
claude mcp add --transport http signals     http://127.0.0.1:8104/mcp
```

`ANTHROPIC_API_KEY` is optional until Phase 7 (Ask Claude): without it news stays unclassified - or,
during a synthetic replay, uses the fixture's test labels.

Or as local stdio processes (Claude Desktop / Code start them; Redis and the engine must be running):

```powershell
claude mcp add market-data -- uv --directory D:/Aryan/Trading run market-data-mcp
claude mcp add options     -- uv --directory D:/Aryan/Trading run options-mcp
```

| Server | Tools |
|---|---|
| market-data-mcp | `get_quote`, `get_candles` (recorded history), `get_constituents`, `get_macro` |
| options-mcp | `get_option_chain`, `get_iv_summary`, `get_oi_buildup`, `get_pcr_maxpain` |
| news-mcp | `get_news` (relevant, classified items with reasons), `get_news_score` |
| signals-mcp | `get_market_snapshot` (everything, one call), `get_signal`, `get_expected_range`, `get_levels`, `backtest`, `get_signal_stats` |

Every tool returns compact JSON: `{symbol, source, as_of, stale, now, data}`. Tools are read-only.

## Security

- **Secrets** live only in `.env` (git-ignored, excluded from the Docker image). Each container
  receives only the credentials it uses (`x-no-secrets` in the compose file); the broker API secrets
  never enter a container - they are used by the login CLI on the host.
- **Logs** pass through a redacting formatter (`mp_core.logs`): bot tokens, API keys, `Authorization`
  values, OAuth `code`/`request_token` query values and database passwords are replaced with
  `<redacted>`; HTTP client request lines are not logged.
- **Network**: every port is published on 127.0.0.1 only. The gateway and MCP servers reject other
  `Host` names (DNS rebinding) and requests from other websites (`Origin`), so a page open in your
  browser cannot ask Claude on your key or read the stream. Add names with `MP_ALLOWED_HOSTS`.
- **Untrusted input**: news links must be http(s) (no `javascript:`), headlines are rendered as text
  and HTML-escaped in Telegram; Claude has no tools and only sees the read-only digest.
- **No order placement** exists in any adapter. Containers run as a non-root user.

## Data provenance

Every value is wrapped in `Sourced[T]` with `source`, `as_of` (UTC) and `stale` (computed against a TTL).
Derived values inherit it: anything computed from a synthetic input is synthetic, and a derived value is
stale as soon as any input is. Staleness is judged against the shared clock - replay time during a
replay - and TTLs are never tighter than the dataset's own update cadence. `source="synthetic"` data is
for development only; evaluation refuses to score it (also enforced by a CHECK constraint on
`signal_outcomes`). Recording is idempotent: restarting ingest never duplicates rows.

## Known gaps

- This development network blocks NSE, RBI, all three broker APIs and the news sites, so live
  recording and live news polling have to run from another network (home, a cloud VM) or after an
  IT whitelist. The feed URLs in `news_feeds.toml` are `verified = false` until fetched once.
- `mp_core/data/holidays.toml` is empty - fill it from NSE's official holiday circular.
- Lot sizes / expiry rules in `indices.toml` are `verified = false` until checked against a real bhavcopy.
- The bhavcopy loader is written to NSE's published layout but has not yet parsed a real file.
- Not yet run against real services (blocked on this network or awaiting credentials): Claude (Haiku news
  classification, Ask-Claude), Telegram, Upstox, NSE bhavcopy downloads. Each has a one-command check:
  `mp news-eval`, the Ask panel, a Telegram test alert, `mp upstox-check`, `mp kite-check`, `mp bhavcopy-load`.
- Docker Desktop's data disk was moved to `D:\DockerData\wsl\disk` (a junction at the old path points
  there). Tests write their temp data under the repo (`.pytest_tmp/`), not `%TEMP%`.
- Databases created before migrations 002-005 need them applied by hand (init scripts only run on a
  fresh volume):
  `docker compose -f infra/docker-compose.yml --env-file .env exec -T timescale sh -c 'psql -U $POSTGRES_USER -d $POSTGRES_DB' < infra/timescale/init/005_signal_outcomes.sql`
  (same for 002, 003 and 004).
