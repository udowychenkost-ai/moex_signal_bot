from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.analysis import prepare_technical_features, score_technical_features
from app.research_features import ResearchTechnicalSeries
from tests.test_backtest import _history


@pytest.mark.parametrize("index", [99, 499, 699])
def test_batch_research_features_match_canonical_window(index: int) -> None:
    candles = _history(700)
    series = ResearchTechnicalSeries(candles)
    canonical = prepare_technical_features(candles[max(0, index - 499) : index + 1])
    prepared = series.at_end(candles[index].end)

    assert prepared.support_levels == canonical.support_levels
    assert prepared.resistance_levels == canonical.resistance_levels
    assert prepared.legacy_support == canonical.legacy_support
    assert prepared.legacy_resistance == canonical.legacy_resistance
    for field in ("rsi", "macd_histogram", "atr", "ema20", "ema50", "adx"):
        assert getattr(prepared, field) == pytest.approx(
            getattr(canonical, field),
            rel=1e-6,
            abs=1e-8,
        )
    for scoring_model in ("legacy", "weighted"):
        assert (
            score_technical_features(
                prepared,
                scoring_model=scoring_model,
            ).score
            == score_technical_features(
                canonical,
                scoring_model=scoring_model,
            ).score
        )


def test_batch_features_at_decision_ignore_appended_future_candle() -> None:
    candles = _history(200)
    decision = candles[-1].end
    future = SimpleNamespace(
        begin=decision,
        end=decision + timedelta(hours=1),
        open=10_000.0,
        high=20_000.0,
        low=1.0,
        close=15_000.0,
        volume=10**12,
    )

    prefix = ResearchTechnicalSeries(candles).at_end(decision)
    with_future = ResearchTechnicalSeries([*candles, future]).at_end(decision)

    assert with_future == prefix
