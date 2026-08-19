from __future__ import annotations

import math
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime

from app.backtest import (
    BacktestResult,
    build_breakdowns,
    calculate_backtest_metrics,
)
from app.config import Settings
from app.domain import HorizonProfile


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class EvaluationSplit:
    name: str
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if _utc(self.start) >= _utc(self.end):
            raise ValueError("evaluation split start must precede end")


@dataclass(frozen=True, slots=True)
class DatasetSplits:
    train: EvaluationSplit
    validation: EvaluationSplit
    test: EvaluationSplit

    def as_dict(self) -> dict[str, dict[str, str]]:
        return {
            split.name: {"start": split.start.isoformat(), "end": split.end.isoformat()}
            for split in (self.train, self.validation, self.test)
        }


def chronological_splits(
    start: datetime,
    end: datetime,
    *,
    train_fraction: float,
    validation_fraction: float,
    test_fraction: float,
) -> DatasetSplits:
    if abs(train_fraction + validation_fraction + test_fraction - 1.0) > 1e-9:
        raise ValueError("split fractions must sum to 1")
    if min(train_fraction, validation_fraction, test_fraction) <= 0:
        raise ValueError("split fractions must be positive")
    start_utc, end_utc = _utc(start), _utc(end)
    if start_utc >= end_utc:
        raise ValueError("dataset start must precede end")
    duration = end_utc - start_utc
    train_end = start_utc + duration * train_fraction
    validation_end = train_end + duration * validation_fraction
    return DatasetSplits(
        train=EvaluationSplit("train", start_utc, train_end),
        validation=EvaluationSplit("validation", train_end, validation_end),
        test=EvaluationSplit("test", validation_end, end_utc),
    )


def walk_forward_splits(
    start: datetime,
    end: datetime,
    *,
    folds: int,
    initial_train_fraction: float = 0.4,
) -> list[tuple[EvaluationSplit, EvaluationSplit]]:
    if folds < 1:
        raise ValueError("folds must be positive")
    if not 0 < initial_train_fraction < 1:
        raise ValueError("initial train fraction must be between 0 and 1")
    start_utc, end_utc = _utc(start), _utc(end)
    duration = end_utc - start_utc
    initial_end = start_utc + duration * initial_train_fraction
    step = (end_utc - initial_end) / folds
    result: list[tuple[EvaluationSplit, EvaluationSplit]] = []
    for index in range(folds):
        test_start = initial_end + step * index
        test_end = initial_end + step * (index + 1)
        result.append(
            (
                EvaluationSplit(f"wf{index + 1}_train", start_utc, test_start),
                EvaluationSplit(f"wf{index + 1}_test", test_start, test_end),
            )
        )
    return result


@dataclass(frozen=True, slots=True)
class StrategyConfiguration:
    name: str
    scoring_model: str
    signal_threshold: float
    minimum_confidence: float
    minimum_reward_risk_ratio: float = 2.0
    atr_scale: float = 1.0
    entry_zone_scale: float = 1.0
    timeframe_variant: str = "default"
    trend_weight: float = 25.0
    momentum_weight: float = 25.0
    macd_weight: float = 20.0
    bollinger_weight: float = 15.0
    volume_weight: float = 15.0

    def __post_init__(self) -> None:
        if self.scoring_model not in {"legacy", "weighted"}:
            raise ValueError("scoring model must be legacy or weighted")
        if self.timeframe_variant not in {"default", "primary_focus", "context_focus"}:
            raise ValueError("unsupported timeframe variant")
        if (
            min(
                self.signal_threshold,
                self.minimum_confidence,
                self.minimum_reward_risk_ratio,
                self.atr_scale,
                self.entry_zone_scale,
            )
            <= 0
        ):
            raise ValueError("strategy parameters must be positive")

    def settings(self, base: Settings) -> Settings:
        return base.model_copy(
            update={
                "technical_scoring_model": self.scoring_model,
                "signal_threshold": self.signal_threshold,
                "idea_minimum_confidence": self.minimum_confidence,
                "minimum_reward_risk_ratio": self.minimum_reward_risk_ratio,
                "score_weight_trend": self.trend_weight,
                "score_weight_momentum": self.momentum_weight,
                "score_weight_macd": self.macd_weight,
                "score_weight_bollinger": self.bollinger_weight,
                "score_weight_volume": self.volume_weight,
            }
        )

    def profile(self, base: HorizonProfile) -> HorizonProfile:
        weights = dict(base.timeframe_weights)
        if self.timeframe_variant == "primary_focus":
            weights[base.primary_timeframe] *= 1.35
        elif self.timeframe_variant == "context_focus":
            for timeframe in weights:
                if timeframe in {"4h", "1d", "1w"}:
                    weights[timeframe] *= 1.25
        return replace(
            base,
            timeframe_weights=weights,
            minimum_confidence=self.minimum_confidence,
            atr_stop_multiplier=base.atr_stop_multiplier * self.atr_scale,
            atr_take_multiplier=base.atr_take_multiplier * self.atr_scale,
            entry_zone_atr=base.entry_zone_atr * self.entry_zone_scale,
        )

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def calibration_candidates() -> list[StrategyConfiguration]:
    """Small, declared sweep: no TEST-driven or thousand-combination search."""
    return [
        StrategyConfiguration("legacy_default", "legacy", 25, 60),
        StrategyConfiguration(
            "legacy_selective",
            "legacy",
            35,
            70,
            atr_scale=0.85,
            entry_zone_scale=0.8,
        ),
        StrategyConfiguration(
            "legacy_broad",
            "legacy",
            20,
            60,
            atr_scale=1.15,
            entry_zone_scale=1.2,
        ),
        StrategyConfiguration("weighted_default", "weighted", 25, 60),
        StrategyConfiguration(
            "weighted_trend_context",
            "weighted",
            25,
            65,
            timeframe_variant="context_focus",
            trend_weight=35,
            momentum_weight=20,
            volume_weight=10,
        ),
        StrategyConfiguration(
            "weighted_momentum_primary",
            "weighted",
            25,
            60,
            timeframe_variant="primary_focus",
            trend_weight=20,
            momentum_weight=35,
            bollinger_weight=10,
        ),
    ]


def objective_score(result: BacktestResult, *, minimum_activated: int = 10) -> float:
    metrics = result.metrics
    if metrics.activated_count == 0:
        return -1_000.0
    profit_factor = metrics.profit_factor
    bounded_pf = 3.0 if math.isinf(profit_factor) else min(3.0, profit_factor)
    scarcity_penalty = max(0, minimum_activated - metrics.activated_count) * 0.25
    return (
        metrics.expectancy_r * 2.0
        + (bounded_pf - 1.0)
        - metrics.maximum_drawdown_pct * 0.05
        - scarcity_penalty
    )


def stability_score(train: BacktestResult, validation: BacktestResult) -> float:
    base = objective_score(validation)
    expectancy_gap = abs(train.metrics.expectancy_r - validation.metrics.expectancy_r)
    drawdown_gap = abs(train.metrics.maximum_drawdown_pct - validation.metrics.maximum_drawdown_pct)
    return base - expectancy_gap - drawdown_gap * 0.02


def aggregate_results(
    results: list[BacktestResult],
    *,
    initial_equity: float,
) -> BacktestResult:
    trades = [trade for result in results for trade in result.trades]
    closed = sorted(
        (trade for trade in trades if trade.closed_at is not None),
        key=lambda trade: _utc(trade.closed_at),
    )
    equity_curve = [initial_equity]
    for trade in closed:
        equity_curve.append(equity_curve[-1] + trade.net_pnl)
    return BacktestResult(
        metrics=calculate_backtest_metrics(
            trades,
            initial_equity=initial_equity,
            equity_curve=equity_curve,
        ),
        trades=trades,
        breakdowns=build_breakdowns(trades),
    )
