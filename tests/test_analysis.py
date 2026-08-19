from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from app.analysis import analyze_technical, calculate_atr, calculate_macd, calculate_rsi
from app.domain import InsufficientDataError


def test_rsi_reaches_expected_extremes() -> None:
    rising = pd.Series(np.arange(1.0, 40.0))
    falling = pd.Series(np.arange(40.0, 1.0, -1))
    assert calculate_rsi(rising).iloc[-1] == pytest.approx(100)
    assert calculate_rsi(falling).iloc[-1] == pytest.approx(0)


def test_macd_and_atr_are_finite() -> None:
    close = pd.Series(np.linspace(100, 120, 80))
    macd, signal, histogram = calculate_macd(close)
    frame = pd.DataFrame({"high": close + 2, "low": close - 2, "close": close})
    atr = calculate_atr(frame)
    assert macd.iloc[-1] > 0
    assert signal.iloc[-1] > 0
    assert np.isfinite(histogram.iloc[-1])
    assert atr.iloc[-1] == pytest.approx(4.0)


def test_analysis_returns_explainable_bounded_score() -> None:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    close = 100 + np.sin(np.arange(100) / 6) * 3 + np.arange(100) * 0.15
    candles = [
        SimpleNamespace(
            begin=start + timedelta(days=index),
            open=value - 0.2,
            high=value + 1,
            low=value - 1,
            close=value,
            volume=1000 + index * 5,
        )
        for index, value in enumerate(close)
    ]
    result = analyze_technical(candles)
    assert -100 <= result.score <= 100
    assert result.atr > 0
    assert result.explanations


def test_analysis_rejects_short_history() -> None:
    with pytest.raises(InsufficientDataError, match="60"):
        analyze_technical([])
