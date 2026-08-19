from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import UTC, datetime

import pandas as pd

from app.analysis import (
    TechnicalFeatures,
    add_indicators,
    candle_frame,
    prepare_technical_features,
    score_technical_features,
)
from app.domain import InsufficientDataError


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _optional(value: object) -> float | None:
    number = float(value)
    return number if math.isfinite(number) else None


def _cluster(values: list[float], tolerance_pct: float = 0.5) -> list[float]:
    ordered = sorted(values)
    if not ordered:
        return []
    clusters: list[list[float]] = [[ordered[0]]]
    for value in ordered[1:]:
        center = sum(clusters[-1]) / len(clusters[-1])
        if center and abs(value - center) / center * 100 <= tolerance_pct:
            clusters[-1].append(value)
        else:
            clusters.append([value])
    return [round(sum(cluster) / len(cluster), 6) for cluster in clusters]


def _low_points(series: pd.Series, *, span: int) -> tuple[list[int], list[float]]:
    extrema = series.where(series == series.rolling(span, center=True, min_periods=span).min())
    return _non_null_points(extrema)


def _high_points(series: pd.Series, *, span: int) -> tuple[list[int], list[float]]:
    extrema = series.where(series == series.rolling(span, center=True, min_periods=span).max())
    return _non_null_points(extrema)


def _non_null_points(series: pd.Series) -> tuple[list[int], list[float]]:
    positions: list[int] = []
    values: list[float] = []
    for index, value in enumerate(series):
        if pd.notna(value):
            positions.append(index)
            values.append(float(value))
    return positions, values


def _values_between(
    points: tuple[list[int], list[float]],
    left: int,
    right: int,
) -> list[float]:
    positions, values = points
    start = bisect_left(positions, left)
    stop = bisect_right(positions, right)
    return values[start:stop]


@dataclass
class ResearchTechnicalSeries:
    """Causal batch indicators with exact rolling-window pivot selection.

    Fixed-window SMA/oscillator/level values match the canonical analyzer. EWM
    indicators use their full causal history; after the canonical 500-candle
    warm-up the numerical difference is expected to be negligible and is
    measured by ``compare_canonical``.
    """

    candles: list[object]

    def __post_init__(self) -> None:
        self.frame = add_indicators(candle_frame(self.candles))
        self._index_by_end = {_utc(candle.end): index for index, candle in enumerate(self.candles)}
        self._supports = _low_points(self.frame["low"], span=11)
        self._resistances = _high_points(self.frame["high"], span=11)
        self._legacy_supports = _low_points(self.frame["low"], span=5)
        self._legacy_resistances = _high_points(self.frame["high"], span=5)

    def at_end(self, end: datetime) -> TechnicalFeatures:
        index = self._index_by_end[_utc(end)]
        if index < 59:
            raise InsufficientDataError(
                f"Нужно минимум 60 свечей для сигнала, сейчас доступно {index + 1}"
            )
        start = max(0, index - 499)
        current = float(self.frame["close"].iloc[index])
        support_values = _values_between(
            self._supports,
            start + 5,
            index - 5,
        )
        resistance_values = _values_between(
            self._resistances,
            start + 5,
            index - 5,
        )
        support_levels = _cluster(support_values)
        resistance_levels = _cluster(resistance_values)
        supports_below = [value for value in support_levels if value < current]
        resistances_above = [value for value in resistance_levels if value > current]

        legacy_support_values = _values_between(
            self._legacy_supports,
            start + 2,
            index - 2,
        )
        legacy_resistance_values = _values_between(
            self._legacy_resistances,
            start + 2,
            index - 2,
        )
        legacy_supports_below = [value for value in legacy_support_values if value < current]
        legacy_resistances_above = [value for value in legacy_resistance_values if value > current]
        row = self.frame.iloc[index]
        return TechnicalFeatures(
            current_price=current,
            previous_close=float(self.frame["close"].iloc[index - 1]),
            rsi=float(row["rsi_14"]),
            macd=float(row["macd"]),
            macd_signal=float(row["macd_signal"]),
            macd_histogram=float(row["macd_histogram"]),
            previous_macd_histogram=float(self.frame["macd_histogram"].iloc[index - 1]),
            atr=float(row["atr_14"]),
            ema20=float(row["ema_20"]),
            ema50=float(row["ema_50"]),
            volume_ratio=_optional(row["volume_ratio"]),
            support=max(supports_below, default=None),
            resistance=min(resistances_above, default=None),
            legacy_support=(legacy_supports_below[-1] if legacy_supports_below else None),
            legacy_resistance=(legacy_resistances_above[-1] if legacy_resistances_above else None),
            sma20=_optional(row["sma_20"]),
            sma50=_optional(row["sma_50"]),
            sma200=_optional(row["sma_200"]),
            adx=_optional(row["adx_14"]),
            stochastic_k=_optional(row["stochastic_k"]),
            stochastic_d=_optional(row["stochastic_d"]),
            cci=_optional(row["cci_20"]),
            bb_high=_optional(row["bb_high"]),
            bb_low=_optional(row["bb_low"]),
            bb_percent=_optional(row["bb_percent"]),
            obv=_optional(row["obv"]),
            support_levels=support_levels,
            resistance_levels=resistance_levels,
        )

    def provider(self, _ticker: str, _timeframe: str, history: list[object]) -> TechnicalFeatures:
        return self.at_end(history[-1].end)

    def compare_canonical(self, *, samples: int = 3) -> dict[str, object]:
        if samples < 1:
            raise ValueError("samples must be positive")
        final_index = len(self.candles) - 1
        first_index = min(final_index, 499)
        if final_index < 59:
            return {"passed": False, "sample_count": 0, "reason": "fewer than 60 candles"}
        first_index = max(59, first_index)
        if samples == 1 or first_index == final_index:
            indices = [final_index]
        else:
            step = (final_index - first_index) / (samples - 1)
            indices = sorted({round(first_index + step * index) for index in range(samples)})
        max_relative_error = 0.0
        levels_match = True
        scores_match = True
        for index in indices:
            canonical = prepare_technical_features(self.candles[max(0, index - 499) : index + 1])
            prepared = self.at_end(self.candles[index].end)
            levels_match &= (
                prepared.support_levels == canonical.support_levels
                and prepared.resistance_levels == canonical.resistance_levels
                and prepared.legacy_support == canonical.legacy_support
                and prepared.legacy_resistance == canonical.legacy_resistance
            )
            for name in ("rsi", "macd_histogram", "atr", "ema20", "ema50"):
                expected = float(getattr(canonical, name))
                actual = float(getattr(prepared, name))
                max_relative_error = max(
                    max_relative_error,
                    abs(actual - expected) / max(abs(expected), 1e-12),
                )
            for scoring_model in ("legacy", "weighted"):
                scores_match &= (
                    score_technical_features(
                        prepared,
                        scoring_model=scoring_model,
                    ).score
                    == score_technical_features(
                        canonical,
                        scoring_model=scoring_model,
                    ).score
                )
        passed = levels_match and scores_match and max_relative_error <= 1e-5
        return {
            "passed": passed,
            "sample_count": len(indices),
            "levels_match": levels_match,
            "scores_match": scores_match,
            "maximum_critical_relative_error": max_relative_error,
        }
