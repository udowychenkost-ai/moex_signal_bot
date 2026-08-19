from __future__ import annotations

from datetime import timedelta

from app.domain import HorizonProfile, IdeaHorizon

HORIZON_PROFILES: dict[IdeaHorizon, HorizonProfile] = {
    IdeaHorizon.INTRADAY_1D: HorizonProfile(
        horizon=IdeaHorizon.INTRADAY_1D,
        timeframe_weights={"15m": 0.5, "1h": 0.35, "1d": 0.15},
        primary_timeframe="15m",
        technical_weight=0.9,
        fundamental_weight=0.08,
        news_weight=0.02,
        minimum_confidence=60.0,
        atr_stop_multiplier=1.2,
        atr_take_multiplier=2.4,
        entry_zone_atr=0.3,
        default_expiry=timedelta(days=1),
    ),
    IdeaHorizon.SWING_5D: HorizonProfile(
        horizon=IdeaHorizon.SWING_5D,
        timeframe_weights={"1h": 0.25, "4h": 0.35, "1d": 0.3, "1w": 0.1},
        primary_timeframe="4h",
        technical_weight=0.75,
        fundamental_weight=0.2,
        news_weight=0.05,
        minimum_confidence=65.0,
        atr_stop_multiplier=1.5,
        atr_take_multiplier=3.0,
        entry_zone_atr=0.4,
        default_expiry=timedelta(days=5),
    ),
    IdeaHorizon.POSITION_1M: HorizonProfile(
        horizon=IdeaHorizon.POSITION_1M,
        timeframe_weights={"4h": 0.1, "1d": 0.55, "1w": 0.35},
        primary_timeframe="1d",
        technical_weight=0.6,
        fundamental_weight=0.3,
        news_weight=0.1,
        minimum_confidence=70.0,
        atr_stop_multiplier=2.0,
        atr_take_multiplier=4.0,
        entry_zone_atr=0.5,
        default_expiry=timedelta(days=30),
    ),
}


def get_horizon_profile(horizon: IdeaHorizon | str) -> HorizonProfile:
    try:
        key = horizon if isinstance(horizon, IdeaHorizon) else IdeaHorizon(horizon)
    except ValueError as error:
        supported = ", ".join(item.value for item in IdeaHorizon)
        raise ValueError(f"Unsupported idea horizon {horizon!r}; use {supported}") from error
    return HORIZON_PROFILES[key]
