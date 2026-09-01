from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime

import pandas as pd

from app.analysis import candle_frame
from app.observation import aware_utc
from app.v24_domain import (
    EventStateV24,
    JournalDirection,
    MarketBiasV24,
    MarketTrendRegime,
    VolatilityStateV24,
)


@dataclass(frozen=True, slots=True)
class MarketRegimeConfigV24:
    version: str = "intraday_v2_4_regime_1"
    strong_ema_separation_pct: float = 1.0
    strong_return_20_pct: float = 4.0
    trend_return_20_pct: float = 0.5
    high_vol_percentile: float = 80.0
    low_vol_percentile: float = 20.0
    abnormal_return_sigma: float = 4.0

    def __post_init__(self) -> None:
        if (
            min(
                self.strong_ema_separation_pct,
                self.strong_return_20_pct,
                self.trend_return_20_pct,
                self.abnormal_return_sigma,
            )
            <= 0
        ):
            raise ValueError("Regime thresholds must be positive")
        if not 0 <= self.low_vol_percentile < self.high_vol_percentile <= 100:
            raise ValueError("Volatility percentiles must be ordered in [0, 100]")


@dataclass(frozen=True, slots=True)
class MarketRegimeAssessmentV24:
    regime: MarketTrendRegime
    volatility: VolatilityStateV24
    event_state: EventStateV24
    bias: MarketBiasV24
    as_of: datetime
    ema20: float
    ema50: float
    return_20_pct: float
    realized_volatility: float | None
    volatility_percentile: float | None
    reasons: tuple[str, ...]

    def allows_direction(self, direction: JournalDirection) -> bool:
        if self.event_state is EventStateV24.PANIC:
            return False
        if direction is JournalDirection.LONG:
            return self.regime in {
                MarketTrendRegime.UPTREND,
                MarketTrendRegime.STRONG_UPTREND,
            }
        return self.regime in {
            MarketTrendRegime.DOWNTREND,
            MarketTrendRegime.STRONG_DOWNTREND,
        }


def _frame(candles: list[object], as_of: datetime) -> pd.DataFrame:
    decision_time = aware_utc(as_of)
    selected = [item for item in candles if aware_utc(item.end) <= decision_time]
    if not selected:
        return pd.DataFrame()
    frame = candle_frame(selected).sort_index()
    return frame[~frame.index.duplicated(keep="last")]


def analyze_market_regime_v24(
    benchmark_candles: list[object],
    *,
    event_state: EventStateV24,
    as_of: datetime | None = None,
    config: MarketRegimeConfigV24 | None = None,
) -> MarketRegimeAssessmentV24:
    policy = config or MarketRegimeConfigV24()
    decision_at = aware_utc(
        as_of or (benchmark_candles[-1].end if benchmark_candles else datetime.now(UTC))
    )
    frame = _frame(benchmark_candles, decision_at)
    if len(frame) < 60:
        raise ValueError("At least 60 completed benchmark candles are required")
    close = frame["close"].astype(float)
    ema20_series = close.ewm(span=20, adjust=False).mean()
    ema50_series = close.ewm(span=50, adjust=False).mean()
    ema20 = float(ema20_series.iloc[-1])
    ema50 = float(ema50_series.iloc[-1])
    current = float(close.iloc[-1])
    return20 = (current / float(close.iloc[-21]) - 1) * 100
    separation = (ema20 / ema50 - 1) * 100
    ema50_slope = (ema50 / float(ema50_series.iloc[-6]) - 1) * 100
    reasons: list[str] = []
    if (
        current > ema20 > ema50
        and separation >= policy.strong_ema_separation_pct
        and return20 >= policy.strong_return_20_pct
        and ema50_slope > 0
    ):
        regime = MarketTrendRegime.STRONG_UPTREND
    elif current > ema20 > ema50 and return20 >= policy.trend_return_20_pct:
        regime = MarketTrendRegime.UPTREND
    elif (
        current < ema20 < ema50
        and separation <= -policy.strong_ema_separation_pct
        and return20 <= -policy.strong_return_20_pct
        and ema50_slope < 0
    ):
        regime = MarketTrendRegime.STRONG_DOWNTREND
    elif current < ema20 < ema50 and return20 <= -policy.trend_return_20_pct:
        regime = MarketTrendRegime.DOWNTREND
    else:
        regime = MarketTrendRegime.RANGE
    reasons.append(f"trend={regime.value} ema20={ema20:.4f} ema50={ema50:.4f}")

    returns = close.pct_change().dropna()
    rolling_vol = returns.rolling(20).std().dropna()
    latest_vol = float(rolling_vol.iloc[-1]) if not rolling_vol.empty else None
    percentile = (
        float((rolling_vol.tail(250) <= latest_vol).mean() * 100)
        if latest_vol is not None and math.isfinite(latest_vol)
        else None
    )
    latest_return = float(returns.iloc[-1]) if not returns.empty else 0.0
    if latest_vol and abs(latest_return) >= policy.abnormal_return_sigma * latest_vol:
        volatility = VolatilityStateV24.ABNORMAL
    elif percentile is not None and percentile >= policy.high_vol_percentile:
        volatility = VolatilityStateV24.HIGH_VOL
    elif percentile is not None and percentile <= policy.low_vol_percentile:
        volatility = VolatilityStateV24.LOW_VOL
    else:
        volatility = VolatilityStateV24.NORMAL_VOL
    reasons.append(f"volatility={volatility.value}")

    bias = {
        MarketTrendRegime.STRONG_UPTREND: MarketBiasV24.STRONG_LONG,
        MarketTrendRegime.UPTREND: MarketBiasV24.LONG,
        MarketTrendRegime.RANGE: MarketBiasV24.NEUTRAL,
        MarketTrendRegime.DOWNTREND: MarketBiasV24.SHORT,
        MarketTrendRegime.STRONG_DOWNTREND: MarketBiasV24.STRONG_SHORT,
    }[regime]
    if event_state is EventStateV24.PANIC:
        bias = MarketBiasV24.NEUTRAL
        volatility = VolatilityStateV24.ABNORMAL
        reasons.append("PANIC event state blocks trend-only entry")
    return MarketRegimeAssessmentV24(
        regime=regime,
        volatility=volatility,
        event_state=event_state,
        bias=bias,
        as_of=decision_at,
        ema20=ema20,
        ema50=ema50,
        return_20_pct=return20,
        realized_volatility=latest_vol,
        volatility_percentile=percentile,
        reasons=tuple(reasons),
    )
