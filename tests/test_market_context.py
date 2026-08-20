from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import numpy as np

from app.analysis import analyze_technical, volume_state
from app.market_context import analyze_market_context
from app.research_features import ResearchMarketContextSeries


def _candles(values: np.ndarray, *, volume: float = 1_000) -> list[SimpleNamespace]:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    return [
        SimpleNamespace(
            begin=start + timedelta(days=index),
            end=start + timedelta(days=index + 1),
            open=float(value + 0.2),
            high=float(value + 1),
            low=float(value - 1),
            close=float(value),
            volume=volume,
        )
        for index, value in enumerate(values)
    ]


def test_market_regime_and_relative_strength_are_point_in_time() -> None:
    market = _candles(np.linspace(100, 80, 240))
    strong_stock = _candles(np.linspace(100, 110, 240))

    context = analyze_market_context(
        market,
        strong_stock,
        timeframe="1d",
        as_of=strong_stock[-1].end,
    )

    assert context.regime == "BEAR"
    assert context.regime_score < 0
    assert context.relative_strength_score > 0
    assert context.relative_strength_label == "выше рынка"


def test_crash_oversold_does_not_become_automatic_buy() -> None:
    # A broad crash and a falling stock create low oscillators, but there is no
    # stabilization/volume/level confirmation. The extreme factor must not turn
    # oversold into a mechanical BUY.
    market = _candles(np.geomspace(100, 55, 240))
    stock = _candles(np.geomspace(100, 45, 240))
    context = analyze_market_context(
        market,
        stock,
        timeframe="1d",
        as_of=stock[-1].end,
    )
    result = analyze_technical(
        stock,
        scoring_model="contextual",
        market_context=context,
    )

    assert result.rsi < 30
    assert context.regime == "BEAR"
    assert result.diagnostic_scores["momentum_extreme"] <= 10
    assert result.score < 25


def test_volume_state_boundaries() -> None:
    assert volume_state(1.0) == "NORMAL"
    assert volume_state(1.3) == "ELEVATED"
    assert volume_state(2.0) == "HIGH"
    assert volume_state(3.0) == "EXTREME"


def test_vectorized_research_context_matches_canonical_formula() -> None:
    market = _candles(100 + np.sin(np.arange(260) / 12) * 4 + np.arange(260) * 0.08)
    stock = _candles(90 + np.sin(np.arange(260) / 10) * 3 + np.arange(260) * 0.12)
    canonical = analyze_market_context(
        market,
        stock,
        timeframe="1d",
        as_of=stock[-1].end,
    )
    vectorized = ResearchMarketContextSeries(market, stock, "1d").at_end(stock[-1].end)

    assert vectorized == canonical
