"""Runtime settings from environment / `.env`. TTLs are per data kind."""

from __future__ import annotations

from functools import cache

from pydantic import AliasChoices, BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class TTLs(BaseModel):
    """Seconds after `as_of` at which a data point is considered stale."""

    tick: float = 5
    quote: float = 5
    candles_intraday: float = 120
    candles_daily: float = 86400 * 4  # survives a long weekend
    option_chain: float = 60
    constituents: float = 300
    india_vix: float = 900
    usdinr: float = 900
    in10y_yield: float = 3600
    fii_dii: float = 86400 * 4  # published end-of-day
    eod: float = 86400 * 4
    news: float = 900  # since the last completed news poll


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="MP_", extra="ignore")

    risk_free_rate: float = Field(default=0.065, ge=0, le=0.2)
    default_symbol: str = "NSE:BANKNIFTY"
    provider: str = "replay"
    replay_dir: str = "data/replay/synthetic/BANKNIFTY"
    ttl: TTLs = TTLs()

    # infrastructure (no MP_ prefix, shared with docker compose)
    redis_url: str = Field(
        default="redis://127.0.0.1:6379/0", validation_alias=AliasChoices("REDIS_URL", "MP_REDIS_URL")
    )
    database_url: str | None = Field(default=None, validation_alias=AliasChoices("DATABASE_URL", "MP_DATABASE_URL"))

    # replay driving (ignored for live providers)
    replay_start: str | None = None  # ISO datetime, IST if no offset; default 2nd day 09:15
    replay_speed: float | None = 60.0  # market seconds per wall second; 0/None = as fast as possible
    record: bool = True  # write ticks/candles/chains to Timescale

    # news (Phase 4)
    news_mode: str = "auto"  # live | fixture | auto (fixture when provider=replay)
    news_fixture: str | None = None  # default: <replay_dir>/news.jsonl
    news_poll_seconds: float = 180.0  # wall-clock seconds between polls
    news_classifier: str = "auto"  # haiku | fixture-labels | none | auto

    # risk (Phase 5) - position sizing suggestions only; nothing is ever traded
    capital: float = Field(default=500_000.0, gt=0)  # INR
    max_risk_pct: float = Field(default=1.0, gt=0, le=5)  # max loss per trade, % of capital
    cost_bps: float = Field(default=3.0, ge=0)  # round-trip exchange charges + taxes, bps of notional
    slippage_bps: float = Field(default=1.0, ge=0)  # per fill
    brokerage_per_order: float = Field(default=20.0, ge=0)  # INR

    # paper trading (services/trader). "live" is refused: no live broker adapter exists, by design,
    # until the rollout gates are passed (README: Trading)
    trading_mode: str = "off"  # off | paper
    daily_loss_limit_pct: float = Field(default=2.0, gt=0, le=10)  # of capital; breaker halts + flattens
    max_drawdown_pct: float = Field(default=6.0, gt=0, le=50)  # of capital, from the equity high-water mark
    max_open_lots: int = Field(default=1, gt=0)
    max_trades_per_day: int = Field(default=3, gt=0)
    freeze_qty: int | None = None  # exchange freeze quantity (units per order) - set from NSE's circular
    audit_dir: str = "data/audit"

    # alerts (Phase 7)
    telegram_bot_token: str | None = Field(default=None, validation_alias=AliasChoices("TELEGRAM_BOT_TOKEN"))
    telegram_chat_id: str | None = Field(default=None, validation_alias=AliasChoices("TELEGRAM_CHAT_ID"))
    alerts_synthetic: bool = False  # send alerts computed from synthetic data (off: never page about fake prices)
    alerts_max_per_hour: int = 20

    # Upstox live market data (Phase 7) - MP_PROVIDER=upstox
    upstox_access_token: str | None = Field(default=None, validation_alias=AliasChoices("UPSTOX_ACCESS_TOKEN"))
    upstox_api_key: str | None = Field(default=None, validation_alias=AliasChoices("UPSTOX_API_KEY"))
    upstox_api_secret: str | None = Field(default=None, validation_alias=AliasChoices("UPSTOX_API_SECRET"))
    upstox_redirect_uri: str = Field(
        default="http://127.0.0.1:8000/", validation_alias=AliasChoices("UPSTOX_REDIRECT_URI")
    )
    upstox_instruments_cache: str = "data/upstox/NSE.json.gz"
    upstox_poll_seconds: float = 1.0

    # Zerodha Kite Connect live market data - MP_PROVIDER=kite
    kite_api_key: str | None = Field(default=None, validation_alias=AliasChoices("KITE_API_KEY"))
    kite_api_secret: str | None = Field(default=None, validation_alias=AliasChoices("KITE_API_SECRET"))
    kite_access_token: str | None = Field(default=None, validation_alias=AliasChoices("KITE_ACCESS_TOKEN"))
    kite_cache_dir: str = "data/kite"
    kite_poll_seconds: float = 1.0


@cache
def get_settings() -> Settings:
    return Settings()
