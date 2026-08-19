from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.config import Settings
from app.db import create_engine_and_session, init_db
from app.domain import IdeaHorizon, IdeaStatus, InstrumentData
from app.idea_repository import create_or_update_idea
from app.ideas import build_trading_idea, idea_material_hash
from app.models import IdeaNotification, TelegramUser, TradingIdea
from app.reporting import (
    ReportingService,
    format_best_ideas,
    format_idea_details,
    format_trading_idea,
    report_is_due,
)
from app.repositories import ensure_user, upsert_instruments
from tests.test_ideas import NOW, signal


class StubBot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        self.messages.append((chat_id, text))


def test_report_frequency_due_logic() -> None:
    user = TelegramUser(
        telegram_id=1,
        report_frequency="3h",
        last_report_at=NOW,
    )
    assert not report_is_due(user, NOW + timedelta(hours=2, minutes=59))
    assert report_is_due(user, NOW + timedelta(hours=3))
    user.report_frequency = "strong"
    assert report_is_due(user, NOW)
    user.report_frequency = "off"
    assert not report_is_due(user, NOW + timedelta(days=1))


@pytest.mark.asyncio
async def test_reporting_filters_and_deduplicates_idea_versions() -> None:
    engine, factory = create_engine_and_session("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    async with factory() as session, session.begin():
        await upsert_instruments(session, [InstrumentData("SBER", "TQBR", "Сбербанк")])
        await ensure_user(session, 1001, "user", "15m", 1.0, "hourly", "all", 70)
    data = build_trading_idea(
        Settings(_env_file=None),
        instrument_name="Сбербанк",
        horizon=IdeaHorizon.INTRADAY_1D,
        signals=[signal("15m", 60), signal("1h", 60), signal("1d", 60)],
        now=NOW,
    )
    assert data is not None
    async with factory() as session, session.begin():
        created = await create_or_update_idea(
            session,
            data,
            material_hash=idea_material_hash(data),
            confidence_delta=7.5,
        )
        idea_id = created.idea.id

    bot = StubBot()
    reporting = ReportingService(factory)
    first = await reporting.dispatch_due(bot, now=NOW)
    second = await reporting.dispatch_due(bot, now=NOW + timedelta(hours=1))

    assert first["reports_sent"] == 1
    assert first["ideas_sent"] == 1
    assert second["reports_sent"] == 0
    assert len(bot.messages) == 1

    async with factory() as session, session.begin():
        idea = await session.get(TradingIdea, idea_id)
        assert idea is not None
        idea.version += 1
        idea.status = IdeaStatus.ACTIVE.value
        idea.updated_at = NOW + timedelta(hours=2)
    third = await reporting.dispatch_due(bot, now=NOW + timedelta(hours=2))
    assert third["reports_sent"] == 1
    assert len(bot.messages) == 2
    async with factory() as session:
        notification_count = await session.scalar(
            select(func.count()).select_from(IdeaNotification)
        )
    assert notification_count == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_user_filters_apply_to_manual_best_ideas() -> None:
    engine, factory = create_engine_and_session("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    async with factory() as session, session.begin():
        await upsert_instruments(session, [InstrumentData("SBER", "TQBR", "Сбербанк")])
        user = await ensure_user(
            session,
            1002,
            "swing",
            "1d",
            1.0,
            "daily",
            IdeaHorizon.SWING_5D.value,
            80,
        )
    service = ReportingService(factory)
    assert await service.best_for_user(user) == []
    await engine.dispose()


def test_primary_idea_message_is_not_overloaded_with_raw_indicators() -> None:
    idea = TradingIdea(
        id=1,
        ticker="SBER",
        instrument_name="Сбербанк",
        direction="BUY",
        horizon=IdeaHorizon.SWING_5D.value,
        primary_timeframe="4h",
        entry_price_from=250,
        entry_price_to=265,
        current_price=258,
        take_profit=300,
        stop_loss=230,
        confidence=78,
        expected_return_pct=13.2,
        risk_pct=13.2,
        risk_reward_ratio=1.0,
        rationale="4h: RSI подтверждает импульс\n1d: MACD выше сигнальной линии",
        invalidation_reason="Идея отменяется ниже 230 ₽",
        status=IdeaStatus.ACTIVE.value,
        source_signal_id=None,
        source_timeframes="4h,1d",
        source_candle_begin=NOW,
        last_evaluated_at=NOW,
        material_hash="hash",
        version=1,
        created_at=NOW,
        updated_at=NOW,
        activated_at=NOW,
        expires_at=NOW + timedelta(days=5),
    )
    primary = format_trading_idea(idea)
    details = format_idea_details(idea)
    summary = format_best_ideas([idea])
    assert "RSI" not in primary
    assert "MACD" not in primary
    assert "RSI" in details
    assert "BUY SBER" in summary
    assert "250.00–265.00" in primary
