from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


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


@dataclass(slots=True)
class RiskLevels:
    entry: float
    stop_loss: float
    take_profit: float
    risk_pct: float
    reward_risk_ratio: float


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


class MoexApiError(RuntimeError):
    """MOEX ISS request or response error."""


class UnknownTickerError(ValueError):
    """Ticker is absent from the configured universe."""


class InsufficientDataError(ValueError):
    """There are not enough candles to calculate a reliable signal."""

