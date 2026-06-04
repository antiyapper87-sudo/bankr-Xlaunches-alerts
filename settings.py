from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "local"

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    authorized_user_ids: str = "544999608"

    socialdata_api_key: str = ""
    coingecko_api_key: str = ""
    coingecko_discovery_enabled: bool = False
    coingecko_discovery_limit: int = 25
    coingecko_poll_interval: int = 720
    coingecko_rate_limit_per_min: int = 12

    database_url: str = "postgresql+asyncpg://user:pass@localhost:5432/bankr_alerts"
    local_database_url: str = "sqlite+aiosqlite:///data/bot.db"
    database_auto_create: bool = True

    redis_url: str = "redis://localhost:6379/0"
    rq_queue_name: str = "launches"

    default_tenant_id: str = "default"
    default_min_score: float = 6.0
    free_daily_signal_limit: int = 10

    max_enrichment_concurrency: int = 8
    max_verdict_concurrency: int = 4
    max_delivery_concurrency: int = 20

    auto_verdict_enabled: bool = True
    trading_enabled: bool = False
    allow_unsafe_trading: bool = False
    dynamic_thresholds_enabled: bool = False
    llm_scoring_enabled: bool = False

    model_config = SettingsConfigDict(env_file=".env.local", extra="ignore")


settings = Settings()


def resolve_database_url() -> str:
    if settings.app_env in {"production", "staging"}:
        return settings.database_url
    return settings.local_database_url
