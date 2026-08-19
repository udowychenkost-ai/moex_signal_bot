from datetime import UTC, datetime, timedelta

import pytest

from app.config import Settings
from app.db import create_engine_and_session, init_db
from app.domain import CandleData, InstrumentData
from app.ingestion import IngestionService, classify_echelon
from app.repositories import upsert_candles, upsert_instruments


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


class CapturingMoex:
    def __init__(self) -> None:
        self.date_from: datetime | None = None

    async def fetch_candles(
        self,
        secid: str,
        timeframe: str,
        date_from: datetime,
        *,
        board_id: str,
    ) -> list[CandleData]:
        self.date_from = date_from
        return []


@pytest.mark.asyncio
async def test_historical_ingestion_requests_only_incremental_overlap() -> None:
    engine, factory = create_engine_and_session("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    latest = datetime(2026, 8, 18, tzinfo=UTC)
    async with factory() as session, session.begin():
        await upsert_instruments(session, [InstrumentData("SBER", "TQBR", "Сбербанк")])
        await upsert_candles(
            session,
            [
                CandleData(
                    "SBER",
                    "TQBR",
                    "1d",
                    latest,
                    latest + timedelta(days=1),
                    100,
                    101,
                    99,
                    100,
                    1000,
                    100_000,
                )
            ],
        )
    moex = CapturingMoex()
    service = IngestionService(settings(), factory, moex)

    await service.sync_candles("SBER", "1d")

    assert moex.date_from == latest - timedelta(days=7)
    await engine.dispose()
