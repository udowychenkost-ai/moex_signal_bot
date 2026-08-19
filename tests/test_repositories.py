from datetime import UTC, datetime, timedelta

import pytest

from app.db import create_engine_and_session, init_db
from app.domain import CandleData, InstrumentData
from app.repositories import get_candles, upsert_candles, upsert_instruments


@pytest.mark.asyncio
async def test_candle_upsert_replaces_open_candle() -> None:
    engine, factory = create_engine_and_session("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    begin = datetime(2025, 1, 1, tzinfo=UTC)
    candle = CandleData(
        "SBER", "TQBR", "1d", begin, begin + timedelta(days=1), 100, 110, 90, 105, 10, 1000
    )
    async with factory() as session, session.begin():
        await upsert_instruments(session, [InstrumentData("SBER", "TQBR", "Сбер")])
        await upsert_candles(session, [candle])
    candle.close = 108
    async with factory() as session, session.begin():
        await upsert_candles(session, [candle])
    async with factory() as session:
        stored = await get_candles(session, "SBER", "1d")
    assert len(stored) == 1
    assert stored[0].close == 108
    await engine.dispose()

