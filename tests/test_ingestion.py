from app.config import Settings
from app.domain import InstrumentData
from app.ingestion import classify_echelon


def settings() -> Settings:
    return Settings(
        _env_file=None,
        blue_chip_tickers="SBER",
        echelon1_min_market_cap=1_000,
        echelon1_min_daily_turnover=500,
        echelon1_min_free_float=10,
        echelon2_min_daily_turnover=100,
    )


def test_allowlisted_blue_chip_is_first_echelon_without_missing_metrics() -> None:
    item = InstrumentData("SBER", "TQBR", "Сбербанк")
    assert classify_echelon(item, settings()) == 1


def test_threshold_classifier_and_liquidity_floor() -> None:
    liquid = InstrumentData(
        "TEST", "TQBR", "Test", market_cap=2_000, daily_turnover=800, free_float=20
    )
    second = InstrumentData("MID", "TQBR", "Mid", daily_turnover=200)
    illiquid = InstrumentData("LOW", "TQBR", "Low", daily_turnover=10)
    assert classify_echelon(liquid, settings()) == 1
    assert classify_echelon(second, settings()) == 2
    assert classify_echelon(illiquid, settings()) == 0
