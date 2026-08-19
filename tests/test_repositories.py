from datetime import UTC, datetime, timedelta

import pytest

from app.db import create_engine_and_session, init_db
from app.domain import CandleData, InstrumentData
from app.models import TelegramUser
from app.repositories import (
    add_watchlist_item,
    deactivate_instruments_except,
    ensure_user,
    get_candles,
    list_active_instruments,
    list_subscriptions,
    update_user_settings,
    upsert_candles,
    upsert_instruments,
)


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


@pytest.mark.asyncio
async def test_universe_sync_deactivates_stale_instruments() -> None:
    engine, factory = create_engine_and_session("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    async with factory() as session, session.begin():
        await upsert_instruments(
            session,
            [
                InstrumentData("SBER", "TQBR", "Сбер"),
                InstrumentData("GAZP", "TQBR", "Газпром"),
            ],
        )
        changed = await deactivate_instruments_except(session, ["sber"])
    async with factory() as session:
        active = await list_active_instruments(session)
    assert changed == 1
    assert [instrument.secid for instrument in active] == ["SBER"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_universe_deactivation_refuses_empty_selection() -> None:
    engine, factory = create_engine_and_session("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    async with factory() as session:
        with pytest.raises(ValueError, match="complete universe"):
            await deactivate_instruments_except(session, [])
    await engine.dispose()


@pytest.mark.asyncio
async def test_subscriptions_include_each_users_risk_setting() -> None:
    engine, factory = create_engine_and_session("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    async with factory() as session, session.begin():
        await upsert_instruments(session, [InstrumentData("SBER", "TQBR", "Сбер")])
        await ensure_user(session, 2**40, "investor", "1h", 0.75)
        await add_watchlist_item(session, 2**40, "SBER")
    async with factory() as session:
        subscriptions = await list_subscriptions(session)
    assert subscriptions == [(2**40, "SBER", "1h", 0.75)]

    async with factory() as session, session.begin():
        await upsert_instruments(session, [InstrumentData("GAZP", "TQBR", "Газпром")])
        await deactivate_instruments_except(session, ["GAZP"])
    async with factory() as session:
        subscriptions = await list_subscriptions(session)
    assert subscriptions == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_user_report_preferences_are_persisted_separately_from_strategy() -> None:
    engine, factory = create_engine_and_session("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    async with factory() as session, session.begin():
        await ensure_user(session, 42, "user", "15m", 1.0)
        await update_user_settings(
            session,
            42,
            report_frequency="3h",
            idea_horizon="SWING_5D",
            risk_pct=2.0,
            minimum_confidence=80,
        )
    async with factory() as session:
        user = await session.get(TelegramUser, 42)
    assert user is not None
    assert user.report_frequency == "3h"
    assert user.idea_horizon == "SWING_5D"
    assert user.risk_per_trade_pct == 2.0
    assert user.minimum_confidence == 80
    await engine.dispose()
