from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "moex_signal_bot/.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    telegram_bot_token: str = ""
    database_url: str = "sqlite+aiosqlite:///./data/moex_bot.db"
    log_level: str = "INFO"

    moex_base_url: str = "https://iss.moex.com/iss"
    moex_api_token: str = ""
    moex_request_timeout_seconds: float = 20.0
    moex_request_concurrency: int = Field(default=5, ge=1, le=20)
    moex_max_retries: int = Field(default=3, ge=1, le=8)
    enable_orderbook: bool = False

    universe_size: int = Field(default=20, ge=1, le=500)
    timeframes: str = "15m,1h,1d"
    default_timeframe: str = "15m"
    ingestion_interval_minutes: int = Field(default=15, ge=1, le=59)
    scheduler_timezone: str = "Europe/Moscow"

    blue_chip_tickers: str = (
        "SBER,GAZP,LKOH,YDEX,NVTK,GMKN,TATN,ROSN,PLZL,MOEX,"
        "MTSS,MGNT,CHMF,NLMK,ALRS,VTBR,SIBN,PHOR,IRAO,SNGS"
    )
    echelon1_min_market_cap: float = 500_000_000_000
    echelon1_min_daily_turnover: float = 500_000_000
    echelon1_min_free_float: float = 10.0
    echelon2_min_daily_turnover: float = 25_000_000

    signal_threshold: float = Field(default=25.0, ge=0, le=100)
    atr_stop_multiplier: float = Field(default=1.5, gt=0)
    atr_take_multiplier: float = Field(default=3.0, gt=0)
    default_risk_per_trade_pct: float = Field(default=1.0, gt=0, le=10)

    @field_validator("default_timeframe")
    @classmethod
    def validate_default_timeframe(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in {"5m", "15m", "1h", "1d", "1w"}:
            raise ValueError("default_timeframe must be one of 5m, 15m, 1h, 1d, 1w")
        return value

    @property
    def timeframe_list(self) -> list[str]:
        allowed = {"5m", "15m", "1h", "1d", "1w"}
        result = list(dict.fromkeys(item.strip().lower() for item in self.timeframes.split(",")))
        invalid = set(result) - allowed
        if invalid:
            raise ValueError(f"Unsupported timeframes: {', '.join(sorted(invalid))}")
        return result

    @property
    def blue_chip_list(self) -> list[str]:
        return list(
            dict.fromkeys(
                item.strip().upper() for item in self.blue_chip_tickers.split(",") if item
            )
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
