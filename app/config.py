from __future__ import annotations

import math
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.domain import IdeaHorizon
from app.v24_domain import SourceClass


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
    # Environment: ENABLE_ORDERBOOK, ORDERBOOK_INTERVAL_MINUTES,
    # ORDERBOOK_REQUEST_CONCURRENCY.
    enable_orderbook: bool = False
    orderbook_interval_minutes: int = Field(default=2, ge=1, le=5)
    orderbook_request_concurrency: int = Field(default=5, ge=1, le=10)

    # V2.4 Data SLA is deliberately fail-closed until the operator supplies all
    # limits and required source classes. None means NOT_CONFIGURED, not zero.
    data_sla_version: str = "v2_4_unconfigured"
    max_latency_enter_now: float | None = Field(default=None, gt=0)
    max_latency_position_management: float | None = Field(default=None, gt=0)
    max_latency_intraday_analysis: float | None = Field(default=None, gt=0)
    required_quote_source_class: str | None = None
    required_volume_source_class: str | None = None
    required_orderbook_source_class: str | None = None
    microstructure_max_spread_pct: float | None = Field(default=None, gt=0, le=1)
    microstructure_orderbook_max_age_seconds: float | None = Field(default=None, gt=0)

    # V2.4 production liquidity/risk values have no pseudo-user defaults.
    liquidity_v2_adv_participation_rate: float | None = Field(default=None, gt=0, le=1)
    liquidity_v2_session_participation_rate: float | None = Field(default=None, gt=0, le=1)
    liquidity_v2_orderbook_participation_rate: float | None = Field(default=None, gt=0, le=1)
    liquidity_v2_normal_exit_participation_rate: float | None = Field(default=None, gt=0, le=1)
    liquidity_v2_fast_exit_participation_rate: float | None = Field(default=None, gt=0, le=1)
    liquidity_v2_stress_exit_participation_rate: float | None = Field(default=None, gt=0, le=1)
    liquidity_v2_max_expected_slippage_bps: float | None = Field(default=None, ge=0)
    liquidity_v2_max_market_impact_bps: float | None = Field(default=None, ge=0)

    # V2.1.5 execution-liquidity UX. These heuristics never affect strategy sizing.
    liquidity_adv_days: int = Field(default=20, ge=5, le=100)
    liquidity_turnover_participation: float = Field(default=0.0025, gt=0, le=0.05)
    liquidity_book_participation: float = Field(default=0.10, gt=0, le=0.50)
    liquidity_book_band_narrow: float = Field(default=0.0025, gt=0, le=0.05)
    liquidity_book_band_primary: float = Field(default=0.005, gt=0, le=0.05)
    liquidity_book_band_wide: float = Field(default=0.01, gt=0, le=0.10)
    liquidity_orderbook_freshness_seconds: int = Field(default=300, ge=10, le=3_600)
    liquidity_turnover_freshness_seconds: int = Field(default=1_800, ge=60, le=86_400)
    liquidity_spread_tight_threshold: float = Field(default=0.001, ge=0, le=0.05)
    liquidity_spread_normal_threshold: float = Field(default=0.0025, ge=0, le=0.05)
    liquidity_spread_wide_threshold: float = Field(default=0.005, ge=0, le=0.10)
    liquidity_spread_tight_modifier: float = Field(default=1.00, gt=0, le=1)
    liquidity_spread_normal_modifier: float = Field(default=0.85, gt=0, le=1)
    liquidity_spread_wide_modifier: float = Field(default=0.60, gt=0, le=1)
    liquidity_spread_very_wide_modifier: float = Field(default=0.35, gt=0, le=1)
    liquidity_unknown_spread_modifier: float = Field(default=0.85, gt=0, le=1)
    liquidity_low_volatility_modifier: float = Field(default=1.00, gt=0, le=1)
    liquidity_normal_volatility_modifier: float = Field(default=0.90, gt=0, le=1)
    liquidity_high_volatility_modifier: float = Field(default=0.70, gt=0, le=1)
    liquidity_extreme_volatility_modifier: float = Field(default=0.45, gt=0, le=1)
    liquidity_unknown_volatility_modifier: float = Field(default=0.75, gt=0, le=1)
    liquidity_high_adv_threshold: float = Field(default=1_000_000_000, gt=0)
    liquidity_medium_adv_threshold: float = Field(default=100_000_000, gt=0)
    liquidity_high_relative_turnover: float = Field(default=0.75, ge=0)
    liquidity_medium_relative_turnover: float = Field(default=0.35, ge=0)
    liquidity_high_depth_threshold: float = Field(default=10_000_000, gt=0)
    liquidity_medium_depth_threshold: float = Field(default=2_000_000, gt=0)
    liquidity_rating_adv_weight: float = Field(default=0.30, ge=0, le=1)
    liquidity_rating_relative_turnover_weight: float = Field(default=0.15, ge=0, le=1)
    liquidity_rating_spread_weight: float = Field(default=0.20, ge=0, le=1)
    liquidity_rating_depth_weight: float = Field(default=0.25, ge=0, le=1)
    liquidity_rating_freshness_weight: float = Field(default=0.10, ge=0, le=1)
    liquidity_min_known_rating_weight: float = Field(default=0.50, gt=0, le=1)
    liquidity_high_rating_score: float = Field(default=0.75, ge=0, le=1)
    liquidity_medium_rating_score: float = Field(default=0.45, ge=0, le=1)

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
    app_version: str = "0.5.6"
    git_commit: str = "unknown"
    intraday_observation_mode: Literal["RESEARCH", "PAPER"] = "RESEARCH"
    swing_observation_mode: Literal["RESEARCH", "PAPER"] = "RESEARCH"
    position_observation_mode: Literal["RESEARCH", "PAPER"] = "PAPER"

    # Isolated MASTER PROMPT v2.4 intraday mode. It never replaces historical
    # INTRADAY_1D cohorts and remains disabled until its production gates are configured.
    intraday_v24_enabled: bool = False
    intraday_v24_strategy_version: str = "intraday_v2_4"
    intraday_v24_execution_1m_enabled: bool = False
    intraday_v24_max_holding_trading_days: int = Field(default=2, ge=1, le=2)
    intraday_v24_leverage_enabled: bool = False
    intraday_v24_structural_atr_buffer_multiplier: float | None = Field(default=None, gt=0)
    intraday_v24_market_summary_hour: int = Field(default=11, ge=0, le=23)

    blue_chip_tickers: str = (
        "SBER,GAZP,LKOH,YDEX,NVTK,GMKN,TATN,ROSN,PLZL,MOEX,"
        "MTSS,MGNT,CHMF,NLMK,ALRS,VTBR,SIBN,PHOR,IRAO,SNGS"
    )
    echelon1_min_market_cap: float = 500_000_000_000
    echelon1_min_daily_turnover: float = 500_000_000
    echelon1_min_free_float: float = 10.0
    echelon2_min_daily_turnover: float = 25_000_000

    signal_threshold: float = Field(default=25.0, ge=0, le=100)
    technical_scoring_model: Literal["weighted", "legacy", "contextual"] = "legacy"
    intraday_technical_scoring_model: Literal["weighted", "legacy", "contextual"] = "legacy"
    swing_technical_scoring_model: Literal["weighted", "legacy", "contextual"] = "legacy"
    position_technical_scoring_model: Literal["weighted", "legacy", "contextual"] = "legacy"
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
    default_report_frequency: Literal["hourly", "3h", "daily", "strong", "off"] = "strong"
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
    market_benchmark: str = "IMOEX"
    market_context_symbols: str = "IMOEX,RTSI,RGBITR,RVI"
    market_context_enabled: bool = True
    fundamental_enabled: bool = True
    fundamental_json_path: str = "fundamentals/official.json"

    strategy_version: str = "v2_ai_quality_filter"
    quality_gate_enabled: bool = True
    quality_min_technical_score: float = Field(default=35.0, ge=0, le=100)
    quality_min_total_score: float = Field(default=35.0, ge=0, le=100)
    intraday_quality_min_confirmations: int = Field(default=4, ge=1, le=7)
    swing_quality_min_confirmations: int = Field(default=5, ge=1, le=7)
    position_quality_min_confirmations: int = Field(default=5, ge=1, le=7)
    quality_min_timeframe_confirmations: int = Field(default=2, ge=1, le=6)
    quality_confirmation_score: float = Field(default=10.0, ge=0, le=100)
    quality_conflict_score: float = Field(default=15.0, ge=0, le=100)
    quality_max_conflicts: int = Field(default=2, ge=0, le=7)
    quality_min_volume_ratio: float = Field(default=1.0, ge=0)
    quality_min_daily_turnover: float = Field(default=25_000_000.0, ge=0)
    quality_strong_regime_score: float = Field(default=50.0, ge=0, le=100)
    quality_regime_override_score: float = Field(default=40.0, ge=0, le=100)
    quality_ai_candidates_per_horizon: int = Field(default=5, ge=1, le=50)
    intraday_max_new_ideas_per_scan: int = Field(default=0, ge=0, le=100)
    swing_max_new_ideas_per_scan: int = Field(default=3, ge=0, le=100)
    position_max_new_ideas_per_scan: int = Field(default=3, ge=0, le=100)
    intraday_max_new_ideas_per_day: int = Field(default=0, ge=0, le=500)
    swing_max_new_ideas_per_day: int = Field(default=5, ge=0, le=500)
    position_max_new_ideas_per_day: int = Field(default=5, ge=0, le=500)
    intraday_cooldown_hours: int = Field(default=24, ge=0, le=24 * 365)
    swing_cooldown_hours: int = Field(default=72, ge=0, le=24 * 365)
    position_cooldown_hours: int = Field(default=168, ge=0, le=24 * 365)

    ai_filter_enabled: bool = True
    ai_allow_unreviewed_fallback: bool = False
    ai_provider: Literal["gemini", "openai"] = "gemini"
    gemini_api_key: str = ""
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    ai_model: str = "gemini-3.6-flash"
    ai_fallback_model: str = "gemini-flash-lite-latest"
    ai_request_timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    ai_max_output_tokens: int = Field(default=4_096, ge=1_024, le=8_192)
    ai_input_cost_per_million: float = Field(default=0.30, ge=0)
    ai_output_cost_per_million: float = Field(default=2.50, ge=0)
    ai_fallback_input_cost_per_million: float = Field(default=0.18, ge=0)
    ai_fallback_output_cost_per_million: float = Field(default=0.72, ge=0)

    @field_validator("default_timeframe")
    @classmethod
    def validate_default_timeframe(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in {"5m", "15m", "1h", "4h", "1d", "1w"}:
            raise ValueError("default_timeframe must be one of 5m, 15m, 1h, 4h, 1d, 1w")
        return value

    @field_validator(
        "required_quote_source_class",
        "required_volume_source_class",
        "required_orderbook_source_class",
    )
    @classmethod
    def validate_v24_source_class(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return SourceClass(value.strip().upper()).value

    @model_validator(mode="after")
    def validate_liquidity_settings(self) -> Settings:
        if not (
            self.liquidity_book_band_narrow
            <= self.liquidity_book_band_primary
            <= self.liquidity_book_band_wide
        ):
            raise ValueError("Liquidity book bands must be ordered narrow <= primary <= wide")
        if not (
            self.liquidity_spread_tight_threshold
            <= self.liquidity_spread_normal_threshold
            <= self.liquidity_spread_wide_threshold
        ):
            raise ValueError("Liquidity spread thresholds must be ordered tight <= normal <= wide")
        if self.liquidity_medium_adv_threshold > self.liquidity_high_adv_threshold:
            raise ValueError("Liquidity ADV thresholds must be ordered medium <= high")
        if self.liquidity_medium_depth_threshold > self.liquidity_high_depth_threshold:
            raise ValueError("Liquidity depth thresholds must be ordered medium <= high")
        if self.liquidity_medium_relative_turnover > self.liquidity_high_relative_turnover:
            raise ValueError(
                "Liquidity relative-turnover thresholds must be ordered medium <= high"
            )
        rating_weight = sum(
            (
                self.liquidity_rating_adv_weight,
                self.liquidity_rating_relative_turnover_weight,
                self.liquidity_rating_spread_weight,
                self.liquidity_rating_depth_weight,
                self.liquidity_rating_freshness_weight,
            )
        )
        if not math.isclose(rating_weight, 1.0):
            raise ValueError("Liquidity rating weights must sum to 1")
        if self.liquidity_medium_rating_score > self.liquidity_high_rating_score:
            raise ValueError("Liquidity rating scores must be ordered medium <= high")
        if (
            self.liquidity_v2_normal_exit_participation_rate is not None
            and self.liquidity_v2_fast_exit_participation_rate is not None
            and self.liquidity_v2_fast_exit_participation_rate
            > self.liquidity_v2_normal_exit_participation_rate
        ):
            raise ValueError("V2 fast-exit participation cannot exceed normal-exit participation")
        if (
            self.liquidity_v2_fast_exit_participation_rate is not None
            and self.liquidity_v2_stress_exit_participation_rate is not None
            and self.liquidity_v2_stress_exit_participation_rate
            > self.liquidity_v2_fast_exit_participation_rate
        ):
            raise ValueError("V2 stress-exit participation cannot exceed fast-exit participation")
        return self

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
    def intraday_v24_timeframe_list(self) -> list[str]:
        required = ["1d", "1h", "15m", "5m"]
        if self.intraday_v24_execution_1m_enabled:
            required.append("1m")
        return required

    @property
    def liquidity_book_bands(self) -> tuple[float, ...]:
        return tuple(
            sorted(
                {
                    self.liquidity_book_band_narrow,
                    self.liquidity_book_band_primary,
                    self.liquidity_book_band_wide,
                }
            )
        )

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

    def horizon_scoring_model(self, horizon: IdeaHorizon | str) -> str:
        selected = horizon if isinstance(horizon, IdeaHorizon) else IdeaHorizon(horizon)
        return {
            IdeaHorizon.INTRADAY_1D: self.intraday_technical_scoring_model,
            IdeaHorizon.SWING_5D: self.swing_technical_scoring_model,
            IdeaHorizon.POSITION_1M: self.position_technical_scoring_model,
        }[selected]

    @property
    def market_context_symbol_list(self) -> list[str]:
        symbols = [
            item.strip().upper() for item in self.market_context_symbols.split(",") if item.strip()
        ]
        if self.market_benchmark.upper() not in symbols:
            symbols.insert(0, self.market_benchmark.upper())
        return list(dict.fromkeys(symbols))

    def max_new_ideas_per_scan(self, horizon: IdeaHorizon | str) -> int:
        selected = horizon if isinstance(horizon, IdeaHorizon) else IdeaHorizon(horizon)
        return {
            IdeaHorizon.INTRADAY_1D: self.intraday_max_new_ideas_per_scan,
            IdeaHorizon.SWING_5D: self.swing_max_new_ideas_per_scan,
            IdeaHorizon.POSITION_1M: self.position_max_new_ideas_per_scan,
        }[selected]

    def minimum_confirmations(self, horizon: IdeaHorizon | str) -> int:
        selected = horizon if isinstance(horizon, IdeaHorizon) else IdeaHorizon(horizon)
        return {
            IdeaHorizon.INTRADAY_1D: self.intraday_quality_min_confirmations,
            IdeaHorizon.SWING_5D: self.swing_quality_min_confirmations,
            IdeaHorizon.POSITION_1M: self.position_quality_min_confirmations,
        }[selected]

    def max_new_ideas_per_day(self, horizon: IdeaHorizon | str) -> int:
        selected = horizon if isinstance(horizon, IdeaHorizon) else IdeaHorizon(horizon)
        return {
            IdeaHorizon.INTRADAY_1D: self.intraday_max_new_ideas_per_day,
            IdeaHorizon.SWING_5D: self.swing_max_new_ideas_per_day,
            IdeaHorizon.POSITION_1M: self.position_max_new_ideas_per_day,
        }[selected]

    def idea_cooldown_hours(self, horizon: IdeaHorizon | str) -> int:
        selected = horizon if isinstance(horizon, IdeaHorizon) else IdeaHorizon(horizon)
        return {
            IdeaHorizon.INTRADAY_1D: self.intraday_cooldown_hours,
            IdeaHorizon.SWING_5D: self.swing_cooldown_hours,
            IdeaHorizon.POSITION_1M: self.position_cooldown_hours,
        }[selected]


@lru_cache
def get_settings() -> Settings:
    return Settings()
