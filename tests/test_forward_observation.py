from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.config import Settings
from app.db import create_engine_and_session, init_db
from app.domain import CandleData, IdeaHorizon, IdeaStatus, InstrumentData, StaleMarketDataError
from app.forward import ForwardReportingService
from app.idea_repository import add_idea_event, create_or_update_idea
from app.ideas import build_trading_idea, idea_material_hash
from app.models import ForwardNotification, TradingIdea, TradingIdeaSnapshot
from app.observation import TIMEFRAME_DURATIONS, DataFreshnessGuard, completed_candles
from app.operations import OperationalService
from app.repositories import ensure_user, upsert_candles, upsert_instruments
from tests.test_ideas import NOW, signal


class StubBot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        self.messages.append((chat_id, text))


def test_completed_candles_excludes_still_forming_bucket() -> None:
    duration = TIMEFRAME_DURATIONS["15m"]
    completed = CandleData(
        "SBER",
        "TQBR",
        "15m",
        NOW - duration,
        NOW,
        100,
        101,
        99,
        100,
        1,
        100,
    )
    forming = replace(completed, begin=NOW - timedelta(minutes=5), end=NOW)
    assert completed_candles([completed, forming], "15m", now=NOW) == [completed]


@pytest.mark.asyncio
async def test_freshness_guard_blocks_missing_or_stale_decision_data() -> None:
    engine, factory = create_engine_and_session("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    settings = Settings(_env_file=None)
    profile_timeframes = ["5m", "15m", "1h", "4h", "1d"]
    async with factory() as session, session.begin():
        await upsert_instruments(session, [InstrumentData("SBER", "TQBR", "Сбербанк")])
        candles = []
        for timeframe in profile_timeframes:
            duration = TIMEFRAME_DURATIONS[timeframe]
            candles.append(
                CandleData(
                    "SBER",
                    "TQBR",
                    timeframe,
                    NOW - duration - timedelta(minutes=1),
                    NOW - timedelta(minutes=1),
                    100,
                    101,
                    99,
                    100,
                    1,
                    100,
                )
            )
        await upsert_candles(session, candles)

    guard = DataFreshnessGuard(settings, factory)
    records = await guard.require_fresh("SBER", IdeaHorizon.INTRADAY_1D, now=NOW)
    assert all(record.is_fresh for record in records)
    with pytest.raises(StaleMarketDataError, match="Stale MOEX data blocked"):
        await guard.require_fresh(
            "SBER",
            IdeaHorizon.INTRADAY_1D,
            now=NOW + timedelta(days=14),
        )
    await engine.dispose()


@pytest.mark.asyncio
async def test_decision_snapshot_is_created_once_and_never_reassessed() -> None:
    engine, factory = create_engine_and_session("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    async with factory() as session, session.begin():
        await upsert_instruments(session, [InstrumentData("SBER", "TQBR", "Сбербанк")])
    candidate = build_trading_idea(
        Settings(_env_file=None),
        instrument_name="Сбербанк",
        horizon=IdeaHorizon.INTRADAY_1D,
        signals=[signal("5m", 60), signal("15m", 60), signal("1h", 60)],
        now=NOW,
    )
    assert candidate is not None
    candidate = replace(
        candidate,
        factor_scores={"15m": {"trend": 20.0}},
        relevant_indicators={"15m": {"rsi": 44.0}},
        atr=2.0,
    )
    async with factory() as session, session.begin():
        created = await create_or_update_idea(
            session,
            candidate,
            material_hash=idea_material_hash(candidate),
            confidence_delta=7.5,
        )
        idea_id = created.idea.id

    changed = replace(
        candidate,
        confidence=candidate.confidence + 10,
        technical_score=85,
        total_score=85,
        source_candle_begin=NOW + timedelta(minutes=15),
        created_at=NOW + timedelta(minutes=15),
        factor_scores={"15m": {"trend": 99.0}},
    )
    async with factory() as session, session.begin():
        result = await create_or_update_idea(
            session,
            changed,
            material_hash=idea_material_hash(changed),
            confidence_delta=7.5,
        )
        assert result.materially_changed

    async with factory() as session:
        snapshot = await session.get(TradingIdeaSnapshot, idea_id)
        count = await session.scalar(select(func.count()).select_from(TradingIdeaSnapshot))
    assert snapshot is not None
    assert snapshot.technical_score == candidate.technical_score
    assert '"trend": 20.0' in snapshot.factor_scores
    assert count == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_forward_notifications_send_creation_and_transition_once() -> None:
    engine, factory = create_engine_and_session("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    settings = Settings(_env_file=None)
    async with factory() as session, session.begin():
        await upsert_instruments(session, [InstrumentData("SBER", "TQBR", "Сбербанк")])
        user = await ensure_user(session, 1001, "owner", "15m", 1.0, "hourly", "all", 60)
        user.created_at = NOW - timedelta(minutes=1)
    candidate = build_trading_idea(
        settings,
        instrument_name="Сбербанк",
        horizon=IdeaHorizon.INTRADAY_1D,
        signals=[signal("5m", 60), signal("15m", 60), signal("1h", 60)],
        now=NOW,
    )
    assert candidate is not None
    async with factory() as session, session.begin():
        created = await create_or_update_idea(
            session,
            candidate,
            material_hash=idea_material_hash(candidate),
            confidence_delta=7.5,
        )
        idea_id = created.idea.id

    freshness = DataFreshnessGuard(settings, factory)
    operations = OperationalService(settings, factory, freshness)
    reporting = ForwardReportingService(settings, factory, operations)
    bot = StubBot()
    first = await reporting.dispatch_notifications(bot, now=NOW)
    duplicate = await reporting.dispatch_notifications(bot, now=NOW)
    assert first["sent"] == 1
    assert duplicate["sent"] == 0
    assert "НОВАЯ ИДЕЯ" in bot.messages[0][1]

    async with factory() as session, session.begin():
        idea = await session.get(TradingIdea, idea_id)
        assert idea is not None
        idea.status = IdeaStatus.ACTIVE.value
        idea.version += 1
        idea.activated_at = NOW + timedelta(minutes=15)
        idea.activation_price = idea.entry_price_to
        add_idea_event(
            session,
            idea,
            event_type="ACTIVATED",
            from_status=IdeaStatus.PENDING_ENTRY.value,
            to_status=IdeaStatus.ACTIVE.value,
            price=idea.activation_price,
            occurred_at=idea.activated_at,
        )
    transition = await reporting.dispatch_notifications(bot, now=NOW + timedelta(minutes=16))
    repeated = await reporting.dispatch_notifications(bot, now=NOW + timedelta(minutes=17))
    assert transition["sent"] == 1
    assert repeated["sent"] == 0
    assert "IDEA ACTIVATED" in bot.messages[-1][1]
    async with factory() as session:
        count = await session.scalar(select(func.count()).select_from(ForwardNotification))
    assert count == 2
    await engine.dispose()
