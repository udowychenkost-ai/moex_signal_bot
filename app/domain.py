from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum


class IdeaDirection(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class IdeaHorizon(StrEnum):
    INTRADAY_1D = "INTRADAY_1D"
    SWING_5D = "SWING_5D"
    POSITION_1M = "POSITION_1M"


class IdeaStatus(StrEnum):
    PENDING_ENTRY = "PENDING_ENTRY"
    ACTIVE = "ACTIVE"
    TP_HIT = "TP_HIT"
    SL_HIT = "SL_HIT"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    INVALIDATED = "INVALIDATED"


class EntryState(StrEnum):
    WAITING = "WAITING"
    ENTRY_AVAILABLE = "ENTRY_AVAILABLE"
    MISSED_INVALID = "MISSED_INVALID"


class ReportFrequency(StrEnum):
    HOURLY = "hourly"
    THREE_HOURS = "3h"
    DAILY = "daily"
    STRONG_ONLY = "strong"
    OFF = "off"


@dataclass(frozen=True, slots=True)
class HorizonProfile:
    horizon: IdeaHorizon
    timeframe_weights: dict[str, float]
    primary_timeframe: str
    technical_weight: float
    fundamental_weight: float
    news_weight: float
    minimum_confidence: float
    atr_stop_multiplier: float
    atr_take_multiplier: float
    entry_zone_atr: float
    default_expiry: timedelta
    technical_component_weights: dict[str, float] = field(
        default_factory=lambda: {
            "trend": 20.0,
            "momentum": 12.0,
            "momentum_extreme": 8.0,
            "volume": 12.0,
            "levels": 10.0,
            "volatility": 8.0,
            "relative_strength": 14.0,
            "market_regime": 16.0,
        }
    )

    def __post_init__(self) -> None:
        if not self.timeframe_weights or self.primary_timeframe not in self.timeframe_weights:
            raise ValueError("primary_timeframe must be included in timeframe_weights")
        if any(weight < 0 for weight in self.timeframe_weights.values()):
            raise ValueError("timeframe weights must be non-negative")
        if sum(self.timeframe_weights.values()) <= 0:
            raise ValueError("at least one timeframe weight must be positive")
        required_components = {
            "trend",
            "momentum",
            "momentum_extreme",
            "volume",
            "levels",
            "volatility",
            "relative_strength",
            "market_regime",
        }
        if set(self.technical_component_weights) != required_components:
            raise ValueError("technical_component_weights must define all contextual components")
        if any(weight < 0 for weight in self.technical_component_weights.values()):
            raise ValueError("technical component weights must be non-negative")
        if sum(self.technical_component_weights.values()) <= 0:
            raise ValueError("at least one technical component weight must be positive")
        factor_total = self.technical_weight + self.fundamental_weight + self.news_weight
        if abs(factor_total - 1.0) > 1e-9:
            raise ValueError("technical, fundamental and news weights must sum to 1")
        if not 0 <= self.minimum_confidence <= 100:
            raise ValueError("minimum_confidence must be between 0 and 100")
        if min(self.atr_stop_multiplier, self.atr_take_multiplier, self.entry_zone_atr) <= 0:
            raise ValueError("ATR parameters must be positive")
        if self.default_expiry <= timedelta(0):
            raise ValueError("default_expiry must be positive")

    @property
    def normalized_timeframe_weights(self) -> dict[str, float]:
        total = sum(self.timeframe_weights.values())
        return {timeframe: weight / total for timeframe, weight in self.timeframe_weights.items()}

    @property
    def normalized_technical_component_weights(self) -> dict[str, float]:
        total = sum(self.technical_component_weights.values())
        return {
            component: weight / total
            for component, weight in self.technical_component_weights.items()
        }


@dataclass(slots=True)
class InstrumentData:
    secid: str
    board_id: str
    short_name: str
    full_name: str | None = None
    isin: str | None = None
    lot_size: int | None = None
    last_price: float | None = None
    market_cap: float | None = None
    daily_turnover: float | None = None
    free_float: float | None = None
    echelon: int = 2
    sector: str = "Unknown"


DEFAULT_SECTORS: dict[str, str] = {
    "SBER": "Financials",
    "VTBR": "Financials",
    "MOEX": "Financials",
    "GAZP": "Energy",
    "LKOH": "Energy",
    "ROSN": "Energy",
    "NVTK": "Energy",
    "TATN": "Energy",
    "SIBN": "Energy",
    "SNGS": "Energy",
    "YDEX": "Information Technology",
    "GMKN": "Materials",
    "PLZL": "Materials",
    "CHMF": "Materials",
    "NLMK": "Materials",
    "ALRS": "Materials",
    "PHOR": "Materials",
    "MTSS": "Communication Services",
    "MGNT": "Consumer Staples",
    "IRAO": "Utilities",
}


@dataclass(slots=True)
class CandleData:
    secid: str
    board_id: str
    timeframe: str
    begin: datetime
    end: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    value: float


@dataclass(slots=True)
class MarketCandleData:
    symbol: str
    timeframe: str
    begin: datetime
    end: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    value: float


@dataclass(slots=True)
class OrderBookLevelData:
    secid: str
    board_id: str
    snapshot_at: datetime
    side: str
    level: int
    price: float
    quantity: float


@dataclass(slots=True)
class TechnicalResult:
    score: float
    rsi: float
    macd: float
    macd_signal: float
    macd_histogram: float
    atr: float
    ema20: float
    ema50: float
    support: float | None
    resistance: float | None
    volume_ratio: float
    explanations: list[str] = field(default_factory=list)
    sma20: float | None = None
    sma50: float | None = None
    sma200: float | None = None
    adx: float | None = None
    stochastic_k: float | None = None
    stochastic_d: float | None = None
    cci: float | None = None
    bb_high: float | None = None
    bb_low: float | None = None
    bb_percent: float | None = None
    obv: float | None = None
    support_levels: list[float] = field(default_factory=list)
    resistance_levels: list[float] = field(default_factory=list)
    component_scores: dict[str, float] = field(default_factory=dict)
    diagnostic_scores: dict[str, float] = field(default_factory=dict)


@dataclass(slots=True)
class RiskLevels:
    entry: float
    stop_loss: float
    take_profit: float
    risk_pct: float
    reward_risk_ratio: float
    method: str = "atr"


@dataclass(slots=True)
class PositionSize:
    units: int
    lots: int
    risk_budget: float
    actual_risk: float
    position_value: float
    capped_by_cash: bool


@dataclass(frozen=True, slots=True)
class TradePnL:
    gross_pnl: float
    commission: float
    slippage: float
    net_pnl: float
    r_multiple: float
    entry_fill_price: float
    exit_fill_price: float


@dataclass(slots=True)
class GeneratedSignal:
    secid: str
    timeframe: str
    horizon: str
    action: str
    technical_score: float
    total_score: float
    confidence: float
    entry_price: float
    stop_loss: float
    take_profit: float
    risk_pct: float
    reward_risk_ratio: float
    rationale: list[str]
    candle_begin: datetime
    risk_method: str = "atr"
    record_id: int | None = None
    atr: float | None = None
    support_levels: list[float] = field(default_factory=list)
    resistance_levels: list[float] = field(default_factory=list)
    factor_scores: dict[str, float] = field(default_factory=dict)
    relevant_indicators: dict[str, float | list[float] | None] = field(default_factory=dict)
    raw_component_scores: dict[str, float] = field(default_factory=dict)
    market_regime: str | None = None
    market_volatility: str | None = None
    market_regime_score: float = 0.0
    relative_strength_score: float = 0.0
    relative_strength_label: str = "недоступно"
    volume_score: float = 0.0
    volume_state: str = "UNKNOWN"
    momentum_extreme_score: float = 0.0


@dataclass(slots=True)
class TradingIdeaData:
    ticker: str
    instrument_name: str
    direction: IdeaDirection
    horizon: IdeaHorizon
    primary_timeframe: str
    entry_price_from: float
    entry_price_to: float
    current_price: float
    take_profit: float
    stop_loss: float
    confidence: float
    expected_return_pct: float
    risk_pct: float
    risk_reward_ratio: float
    rationale: list[str]
    invalidation_reason: str
    status: IdeaStatus
    created_at: datetime
    expires_at: datetime
    source_candle_begin: datetime
    source_signal_id: int | None = None
    source_timeframes: list[str] = field(default_factory=list)
    activated_at: datetime | None = None
    activation_price: float | None = None
    closed_at: datetime | None = None
    close_reason: str | None = None
    close_price: float | None = None
    last_evaluated_at: datetime | None = None
    id: int | None = None
    version: int = 1
    technical_score: float = 0.0
    fundamental_score: float = 0.0
    news_score: float = 0.0
    total_score: float = 0.0
    observation_mode: str = "RESEARCH"
    atr: float | None = None
    factor_scores: dict[str, object] = field(default_factory=dict)
    relevant_indicators: dict[str, object] = field(default_factory=dict)
    regime: str | None = None
    market_volatility: str | None = None
    market_regime_score: float = 0.0
    relative_strength_score: float = 0.0
    relative_strength_label: str = "недоступно"
    volume_score: float = 0.0
    volume_state: str = "UNKNOWN"
    momentum_extreme_score: float = 0.0
    fundamental_components: dict[str, float] = field(default_factory=dict)
    fundamental_publications: list[dict[str, object]] = field(default_factory=list)
    fundamental_label: str = "нет данных"

    @property
    def entry_state(self) -> EntryState:
        if self.status == IdeaStatus.PENDING_ENTRY:
            return EntryState.WAITING
        if self.status == IdeaStatus.ACTIVE:
            return EntryState.ENTRY_AVAILABLE
        return EntryState.MISSED_INVALID


@dataclass(frozen=True, slots=True)
class MarketContextData:
    benchmark: str
    regime: str
    volatility: str
    regime_score: float
    relative_strength_score: float
    relative_strength_label: str
    benchmark_return_pct: float
    instrument_return_pct: float
    drawdown_pct: float
    realized_volatility_pct: float
    atr_pct: float
    as_of: datetime


@dataclass(frozen=True, slots=True)
class FundamentalScoreData:
    score: float
    components: dict[str, float]
    metrics: dict[str, float | None]
    sector: str
    publications: list[dict[str, object]]
    label: str
    as_of: datetime


@dataclass(frozen=True, slots=True)
class IdeaTransition:
    to_status: IdeaStatus
    event_type: str
    price: float
    reason: str
    occurred_at: datetime


class MoexApiError(RuntimeError):
    """MOEX ISS request or response error."""


class UnknownTickerError(ValueError):
    """Ticker is absent from the configured universe."""


class InsufficientDataError(ValueError):
    """There are not enough candles to calculate a reliable signal."""


class StaleMarketDataError(InsufficientDataError):
    """Required decision-time candles are absent or older than the configured guard."""
