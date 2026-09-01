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


class CapturingIntradayMoex:
    def __init__(self) -> None:
        self.security_calls: list[tuple[str, str]] = []
        self.market_calls: list[tuple[str, str]] = []

    async def fetch_candles(
        self,
        secid: str,
        timeframe: str,
        date_from: datetime,
        *,
        board_id: str,
    ) -> list[CandleData]:
        self.security_calls.append((secid, timeframe))
        return []

    async def fetch_market_candles(
        self,
        symbol: str,
        timeframe: str,
        date_from: datetime,
    ) -> list[CandleData]:
        self.market_calls.append((symbol, timeframe))
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


@pytest.mark.asyncio
async def test_intraday_v24_sync_uses_isolated_timeframes_and_echelon_filter() -> None:
    engine, factory = create_engine_and_session("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    async with factory() as session, session.begin():
        await upsert_instruments(
            session,
            [
                InstrumentData("SBER", "TQBR", "Сбербанк", echelon=1),
                InstrumentData("MTSS", "TQBR", "МТС", echelon=2),
                InstrumentData("LOW", "TQBR", "Неликвид", echelon=0),
            ],
        )
    moex = CapturingIntradayMoex()
    config = settings().model_copy(
        update={
            "intraday_v24_enabled": True,
            "intraday_v24_execution_1m_enabled": False,
        }
    )
    service = IngestionService(config, factory, moex)  # type: ignore[arg-type]

    counters = await service.sync_intraday_v24()

    expected_timeframes = {"1d", "1h", "15m", "5m"}
    assert {ticker for ticker, _ in moex.security_calls} == {"SBER", "MTSS"}
    assert all(
        {timeframe for ticker, timeframe in moex.security_calls if ticker == expected_ticker}
        == expected_timeframes
        for expected_ticker in ("SBER", "MTSS")
    )
    assert set(moex.market_calls) == {
        (config.market_benchmark, timeframe) for timeframe in expected_timeframes
    }
    assert counters == {"candles": 0, "market_candles": 0, "errors": 0}
    await engine.dispose()


@pytest.mark.asyncio
async def test_intraday_v24_sync_includes_one_minute_only_when_enabled() -> None:
    engine, factory = create_engine_and_session("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    async with factory() as session, session.begin():
        await upsert_instruments(
            session,
            [InstrumentData("SBER", "TQBR", "Сбербанк", echelon=1)],
        )
    moex = CapturingIntradayMoex()
    config = settings().model_copy(
        update={
            "intraday_v24_enabled": True,
            "intraday_v24_execution_1m_enabled": True,
        }
    )
    service = IngestionService(config, factory, moex)  # type: ignore[arg-type]

    await service.sync_intraday_v24()

    assert ("SBER", "1m") in moex.security_calls
    assert (config.market_benchmark, "1m") in moex.market_calls
    await engine.dispose()
