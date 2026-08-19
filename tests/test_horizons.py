from __future__ import annotations

from datetime import timedelta

import pytest

from app.domain import HorizonProfile, IdeaHorizon
from app.horizons import HORIZON_PROFILES, get_horizon_profile


def test_all_supported_horizons_have_extensible_profiles() -> None:
    assert set(HORIZON_PROFILES) == set(IdeaHorizon)
    for horizon, profile in HORIZON_PROFILES.items():
        assert profile.horizon == horizon
        assert profile.primary_timeframe in profile.timeframe_weights
        assert sum(profile.normalized_timeframe_weights.values()) == pytest.approx(1.0)
        assert profile.default_expiry > timedelta(0)


def test_position_profile_does_not_use_minute_timeframes() -> None:
    profile = get_horizon_profile(IdeaHorizon.POSITION_1M)
    assert not {"5m", "15m"}.intersection(profile.timeframe_weights)
    assert profile.timeframe_weights["1d"] > profile.timeframe_weights["4h"]


def test_intraday_profile_combines_execution_and_context_timeframes() -> None:
    profile = get_horizon_profile(IdeaHorizon.INTRADAY_1D)

    assert {"5m", "15m", "1h"}.issubset(profile.timeframe_weights)
    assert {"4h", "1d"}.issubset(profile.timeframe_weights)
    assert profile.timeframe_weights["15m"] > profile.timeframe_weights["1d"]


def test_invalid_profile_rejects_inconsistent_configuration() -> None:
    with pytest.raises(ValueError, match="primary_timeframe"):
        HorizonProfile(
            horizon=IdeaHorizon.SWING_5D,
            timeframe_weights={"1d": 1.0},
            primary_timeframe="4h",
            technical_weight=1.0,
            fundamental_weight=0.0,
            news_weight=0.0,
            minimum_confidence=60,
            atr_stop_multiplier=1.5,
            atr_take_multiplier=3,
            entry_zone_atr=0.4,
            default_expiry=timedelta(days=5),
        )


def test_unknown_horizon_has_actionable_error() -> None:
    with pytest.raises(ValueError, match="Unsupported idea horizon"):
        get_horizon_profile("WEEK_2")
