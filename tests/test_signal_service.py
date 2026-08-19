from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.config import Settings
from app.db import create_engine_and_session, init_db
from app.domain import CandleData, InstrumentData
from app.models import SignalRecord
from app.repositories import upsert_candles, upsert_instruments
from app.signals import SignalService, format_signal
from tests.test_extended_analysis import make_candles


@pytest.mark.asyncio
async def test_signal_service_generates_and_persists_weighted_signal() -> None:
    engine, factory = create_engine_and_session("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    source = make_candles(count=250, trend="up")
    candles = [
        CandleData(
            secid="SBER",
            board_id="TQBR",
            timeframe="1d",
            begin=item.begin,
            end=item.begin + timedelta(days=1),
            open=item.open,
            high=item.high,
            low=item.low,
            close=item.close,
            volume=item.volume,
            value=item.close * item.volume,
        )
        for item in source
    ]
    async with factory() as session, session.begin():
        await upsert_instruments(session, [InstrumentData("SBER", "TQBR", "Сбер")])
        await upsert_candles(session, candles)

    settings = Settings(
        _env_file=None,
        database_url="sqlite+aiosqlite:///:memory:",
        technical_scoring_model="weighted",
        risk_method="levels",
    )
    generated = await SignalService(settings, factory).generate("sber", "1d")
    duplicate = await SignalService(settings, factory).generate("sber", "1d")

    assert generated.secid == "SBER"
    assert generated.action in {"BUY", "SELL", "HOLD"}
    assert generated.risk_method in {"levels", "atr"}
    assert generated.rationale
    assert generated.record_id is not None
    assert duplicate.record_id == generated.record_id
    assert generated.atr is not None
    assert generated.stop_loss < generated.entry_price < generated.take_profit
    assert "Не является индивидуальной инвестиционной рекомендацией" in format_signal(generated)
    async with factory() as session:
        stored_count = await session.scalar(select(func.count()).select_from(SignalRecord))
    assert stored_count == 1
    await engine.dispose()
