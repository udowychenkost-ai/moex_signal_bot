from app.config import Settings


def test_analysis_timeframes_include_every_horizon_dependency() -> None:
    settings = Settings(_env_file=None, timeframes="15m")

    assert settings.timeframe_list == ["15m"]
    assert settings.analysis_timeframe_list == ["15m", "1h", "4h", "1d", "1w"]
