from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from app.analysis import add_indicators, analyze_technical, candle_frame, detect_support_resistance
from app.config import Settings


def make_candles(count: int = 250, trend: str = "up", seed: int = 42) -> list[object]:
    rng = np.random.default_rng(seed)
    if trend == "up":
        base = np.linspace(100, 180, count)
    elif trend == "down":
        base = np.linspace(180, 100, count)
    else:
        base = np.full(count, 140.0)
    close = base + rng.normal(0, 1.2, count)
    opened = close + rng.normal(0, 0.5, count)
    high = np.maximum(opened, close) + np.abs(rng.normal(0, 0.7, count))
    low = np.minimum(opened, close) - np.abs(rng.normal(0, 0.7, count))
    volume = rng.integers(100_000, 500_000, count)
    start = datetime(2025, 1, 1, tzinfo=UTC)
    return [
        SimpleNamespace(
            begin=start + timedelta(hours=index),
            open=float(opened[index]),
            high=float(high[index]),
            low=float(low[index]),
            close=float(close[index]),
            volume=float(volume[index]),
        )
        for index in range(count)
    ]


def test_extended_indicator_pipeline_has_expected_columns() -> None:
    frame = add_indicators(candle_frame(make_candles()))
    expected = {
        "sma_20",
        "sma_50",
        "sma_200",
        "ema_20",
        "ema_50",
        "macd",
        "macd_signal",
        "macd_histogram",
        "adx_14",
        "rsi_14",
        "stochastic_k",
        "stochastic_d",
        "cci_20",
        "bb_high",
        "bb_low",
        "bb_mid",
        "bb_percent",
        "atr_14",
        "obv",
        "volume_ratio",
    }
    assert expected.issubset(frame.columns)
    assert frame["sma_200"].iloc[-1] == pytest.approx(frame["sma_200"].iloc[-1])


def test_weighted_scoring_is_componentized_and_directional() -> None:
    up = analyze_technical(make_candles(trend="up"), scoring_model="weighted")
    down = analyze_technical(make_candles(trend="down"), scoring_model="weighted")
    assert up.score > down.score
    assert set(up.component_scores) == {
        "trend",
        "momentum",
        "macd",
        "bollinger",
        "volume",
    }
    assert sum(up.component_scores.values()) == pytest.approx(up.score)
    assert -100 <= up.score <= 100
    assert -100 <= down.score <= 100


def test_legacy_scoring_remains_available() -> None:
    result = analyze_technical(make_candles(), scoring_model="legacy")
    assert result.component_scores == {"legacy": result.score}


def test_legacy_scoring_is_the_backwards_compatible_default() -> None:
    explicit = analyze_technical(make_candles(), scoring_model="legacy")
    default = analyze_technical(make_candles())
    assert Settings(_env_file=None).technical_scoring_model == "legacy"
    assert default == explicit


def test_support_resistance_uses_extrema_and_clusters_close_levels() -> None:
    frame = pd.DataFrame(
        {
            "low": [100, 90, 100, 90.2, 100, 80, 100, 80.2, 100],
            "high": [100, 110, 100, 110.3, 100, 120, 100, 120.2, 100],
        }
    )
    levels = detect_support_resistance(frame, window=1, tolerance_pct=0.5)
    assert levels["support"] == pytest.approx([80.1, 90.1])
    assert levels["resistance"] == pytest.approx([110.15, 120.1])


def test_settings_normalize_technical_weights() -> None:
    settings = Settings(
        _env_file=None,
        score_weight_trend=1,
        score_weight_momentum=1,
        score_weight_macd=1,
        score_weight_bollinger=1,
        score_weight_volume=0,
    )
    weights = settings.technical_score_weights
    assert sum(weights.values()) == pytest.approx(100)
    assert weights["trend"] == pytest.approx(25)


def test_legacy_model_does_not_require_weight_configuration() -> None:
    settings = Settings(
        _env_file=None,
        technical_scoring_model="legacy",
        score_weight_trend=0,
        score_weight_momentum=0,
        score_weight_macd=0,
        score_weight_bollinger=0,
        score_weight_volume=0,
    )
    result = analyze_technical(make_candles(), scoring_model=settings.technical_scoring_model)
    assert result.component_scores == {"legacy": result.score}
