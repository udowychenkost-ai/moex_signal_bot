from app.config import Settings
from app.domain import IdeaHorizon


def test_analysis_timeframes_include_every_horizon_dependency() -> None:
    settings = Settings(_env_file=None, timeframes="15m")

    assert settings.timeframe_list == ["15m"]
    assert settings.analysis_timeframe_list == ["5m", "15m", "1h", "4h", "1d", "1w"]


def test_horizon_scoring_selectors_are_independent() -> None:
    settings = Settings(
        _env_file=None,
        intraday_technical_scoring_model="legacy",
        swing_technical_scoring_model="weighted",
        position_technical_scoring_model="contextual",
    )

    assert settings.horizon_scoring_model(IdeaHorizon.INTRADAY_1D) == "legacy"
    assert settings.horizon_scoring_model(IdeaHorizon.SWING_5D) == "weighted"
    assert settings.horizon_scoring_model(IdeaHorizon.POSITION_1M) == "contextual"
