from datetime import date

from app.research_data import load_research_config


def test_research_config_is_separate_and_declares_survivorship_limit() -> None:
    config = load_research_config("research.toml")

    assert len(config.tickers) == 20
    assert {
        "SBER",
        "GAZP",
        "LKOH",
        "YDEX",
        "GMKN",
        "ROSN",
        "NVTK",
        "TATN",
        "MTSS",
        "MOEX",
    }.issubset(config.tickers)
    assert config.start_dates["5m"] == date(2026, 1, 1)
    assert config.selection_method == "fixed_current_liquid_universe"
    assert config.prices_adjusted_for_corporate_actions is False
    assert config.evaluation.train_fraction == 0.6
