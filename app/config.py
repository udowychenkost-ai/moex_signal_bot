from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.domain import IdeaHorizon


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
    scanning_interval_minutes: int = Field(default=15, ge=1, le=59)
    lifecycle_interval_minutes: int = Field(default=5, ge=1, le=59)
    reporting_interval_minutes: int = Field(default=1, ge=1, le=59)
    daily_summary_hour: int = Field(default=19, ge=0, le=23)
    daily_summary_minute: int = Field(default=15, ge=0, le=59)
    scheduler_timezone: str = "Europe/Moscow"
    data_freshness_limits_minutes: str = "5m:30,15m:60,1h:240,4h:1440,1d:5760,1w:14400"
    small_sample_threshold: int = Field(default=30, ge=1, le=10_000)
    telegram_admin_chat_ids: str = ""
    app_version: str = "0.2.0"
    git_commit: str = "unknown"
    intraday_observation_mode: Literal["RESEARCH", "PAPER"] = "RESEARCH"
    swing_observation_mode: Literal["RESEARCH", "PAPER"] = "RESEARCH"
    position_observation_mode: Literal["RESEARCH", "PAPER"] = "PAPER"

    blue_chip_tickers: str = (
        "SBER,GAZP,LKOH,YDEX,NVTK,GMKN,TATN,ROSN,PLZL,MOEX,"
        "MTSS,MGNT,CHMF,NLMK,ALRS,VTBR,SIBN,PHOR,IRAO,SNGS"
    )
    echelon1_min_market_cap: float = 500_000_000_000
    echelon1_min_daily_turnover: float = 500_000_000
    echelon1_min_free_float: float = 10.0
    echelon2_min_daily_turnover: float = 25_000_000

    signal_threshold: float = Field(default=25.0, ge=0, le=100)
    technical_scoring_model: Literal["weighted", "legacy"] = "legacy"
    score_weight_trend: float = Field(default=25.0, ge=0)
    score_weight_momentum: float = Field(default=25.0, ge=0)
    score_weight_macd: float = Field(default=20.0, ge=0)
    score_weight_bollinger: float = Field(default=15.0, ge=0)
    score_weight_volume: float = Field(default=15.0, ge=0)
    atr_stop_multiplier: float = Field(default=1.5, gt=0)
    atr_take_multiplier: float = Field(default=3.0, gt=0)
    risk_method: Literal["atr", "levels"] = "atr"
    level_buffer_pct: float = Field(default=0.3, ge=0, le=10)
    minimum_reward_risk_ratio: float = Field(default=2.0, ge=1)
    default_risk_per_trade_pct: float = Field(default=1.0, gt=0, le=10)
    idea_minimum_confidence: float = Field(default=60.0, ge=50, le=95)
    idea_material_confidence_delta: float = Field(default=7.5, ge=1, le=50)
    default_report_frequency: Literal["hourly", "3h", "daily", "strong", "off"] = "hourly"
    default_idea_horizon: Literal["INTRADAY_1D", "SWING_5D", "POSITION_1M", "all"] = "all"
    default_minimum_confidence: float = Field(default=70.0, ge=50, le=95)
    paper_account_size: float = Field(default=1_000_000.0, gt=0)
    paper_commission_pct: float = Field(default=0.05, ge=0, le=5)
    paper_buy_slippage_bps: float = Field(default=5.0, ge=0, le=500)
    paper_sell_slippage_bps: float = Field(default=5.0, ge=0, le=500)
    backtest_commission_pct: float = Field(default=0.05, ge=0, le=5)
    backtest_buy_slippage_bps: float = Field(default=5.0, ge=0, le=500)
    backtest_sell_slippage_bps: float = Field(default=5.0, ge=0, le=500)
    research_database_url: str = "sqlite+aiosqlite:///./data/research.db"
    research_config_path: str = "research.toml"
    research_output_dir: str = "reports/backtests"
    research_workers: int = Field(default=4, ge=1, le=16)

    @field_validator("default_timeframe")
    @classmethod
    def validate_default_timeframe(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in {"5m", "15m", "1h", "4h", "1d", "1w"}:
            raise ValueError("default_timeframe must be one of 5m, 15m, 1h, 4h, 1d, 1w")
        return value

    @property
    def timeframe_list(self) -> list[str]:
        allowed = {"5m", "15m", "1h", "4h", "1d", "1w"}
        result = list(dict.fromkeys(item.strip().lower() for item in self.timeframes.split(",")))
        invalid = set(result) - allowed
        if invalid:
            raise ValueError(f"Unsupported timeframes: {', '.join(sorted(invalid))}")
        return result

    @property
    def analysis_timeframe_list(self) -> list[str]:
        required = ["5m", "15m", "1h", "4h", "1d", "1w"]
        return list(dict.fromkeys([*required, *self.timeframe_list]))

    @property
    def blue_chip_list(self) -> list[str]:
        return list(
            dict.fromkeys(
                item.strip().upper() for item in self.blue_chip_tickers.split(",") if item
            )
        )

    @property
    def technical_score_weights(self) -> dict[str, float]:
        weights = {
            "trend": self.score_weight_trend,
            "momentum": self.score_weight_momentum,
            "macd": self.score_weight_macd,
            "bollinger": self.score_weight_bollinger,
            "volume": self.score_weight_volume,
        }
        total = sum(weights.values())
        if total <= 0:
            raise ValueError("At least one technical score weight must be positive")
        return {name: weight / total * 100 for name, weight in weights.items()}

    @property
    def freshness_limits(self) -> dict[str, int]:
        allowed = {"5m", "15m", "1h", "4h", "1d", "1w"}
        values: dict[str, int] = {}
        for raw_item in self.data_freshness_limits_minutes.split(","):
            timeframe, separator, raw_minutes = raw_item.strip().partition(":")
            if not separator or timeframe not in allowed:
                raise ValueError(
                    "DATA_FRESHNESS_LIMITS_MINUTES must contain timeframe:minutes pairs"
                )
            minutes = int(raw_minutes)
            if minutes <= 0:
                raise ValueError("Freshness limits must be positive")
            values[timeframe] = minutes
        missing = allowed - values.keys()
        if missing:
            raise ValueError(f"Missing freshness limits: {', '.join(sorted(missing))}")
        return values

    @property
    def admin_chat_ids(self) -> list[int]:
        return list(
            dict.fromkeys(
                int(item.strip())
                for item in self.telegram_admin_chat_ids.split(",")
                if item.strip()
            )
        )

    def observation_mode(self, horizon: IdeaHorizon | str) -> str:
        selected = horizon if isinstance(horizon, IdeaHorizon) else IdeaHorizon(horizon)
        return {
            IdeaHorizon.INTRADAY_1D: self.intraday_observation_mode,
            IdeaHorizon.SWING_5D: self.swing_observation_mode,
            IdeaHorizon.POSITION_1M: self.position_observation_mode,
        }[selected]


@lru_cache
def get_settings() -> Settings:
    return Settings()
