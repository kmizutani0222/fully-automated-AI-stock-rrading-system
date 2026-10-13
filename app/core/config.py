from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    database_url: str = "postgresql+psycopg://asts:asts@localhost:55432/asts"
    trading_mode: str = "PAPER"
    live_trading_enabled: bool = False
    trading_enabled: bool = False
    market_data_provider: str = "yfinance"
    log_level: str = "INFO"
    default_timeframe: str = "5m"
    csv_timezone: str = "UTC"
    admin_token: str = "dev-admin-token"
    runtime_autostart: bool = False
    runtime_bar_delay_seconds: int = 30


def assert_phase1_safety(settings: Settings) -> None:
    """Live orders are not implemented. Asking for them must stop the process."""
    if settings.live_trading_enabled:
        raise RuntimeError(
            "Phase 1 refuses to start because LIVE_TRADING_ENABLED is true. Live trading is not implemented."
        )
    if settings.trading_mode.strip().upper() == "LIVE":
        raise RuntimeError(
            "Phase 1 refuses to start because TRADING_MODE is LIVE. Live trading is not implemented."
        )


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    assert_phase1_safety(settings)
    return settings
