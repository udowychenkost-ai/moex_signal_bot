from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageReplyMarkup,
    EditMessageText,
    SendMessage,
)
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from sqlalchemy import func, select

from app.bot import BotServices, create_router
from app.config import Settings
from app.db import create_engine_and_session, init_db
from app.domain import InstrumentData
from app.idea_repository import create_or_update_idea
from app.ideas import idea_material_hash
from app.models import IdeaFollow, TradingIdea, WatchlistItem
from app.repositories import ensure_user, upsert_instruments
from app.telegram_context import (
    ContextActionExpired,
    ContextObjectNotFound,
    TelegramContextService,
)
from app.telegram_ui import (
    idea_context_keyboard,
    instrument_analysis_keyboard,
    instrument_context_keyboard,
    lifecycle_context_keyboard,
    market_context_keyboard,
    results_keyboard,
    signal_history_keyboard,
    statistics_context_keyboard,
    status_context_keyboard,
    top_ideas_keyboard,
    watchlist_keyboard,
)
from tests.test_ideas import NOW
from tests.test_v2_quality_ai import candidate


class NoopIngestion:
    pass


class NoopSignals:
    pass


class RecordingBot(Bot):
    def __init__(self) -> None:
        super().__init__("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefgh")
        self.methods: list[object] = []

    async def __call__(self, method, request_timeout=None):
        del request_timeout
        self.methods.append(method)
        return True


def callback_values(markup) -> list[str]:
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data is not None
    ]


async def seeded_context(*, tickers: tuple[str, ...] = ("SBER",)):
    engine, factory = create_engine_and_session("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    async with factory() as session, session.begin():
        await upsert_instruments(
            session,
            [InstrumentData(ticker, "TQBR", ticker, daily_turnover=1e9) for ticker in tickers],
        )
        await ensure_user(session, 101, "first", "15m", 1.0)
        await ensure_user(session, 202, "second", "15m", 1.0)
        created = await create_or_update_idea(
            session,
            candidate(),
            material_hash=idea_material_hash(candidate()),
            confidence_delta=7.5,
        )
        idea_id = created.idea.id
    return engine, factory, idea_id


@pytest.mark.asyncio
async def test_context_keyboards_cover_actions_state_navigation_and_callback_limit() -> None:
    engine, factory, idea_id = await seeded_context()
    async with factory() as session:
        idea = await session.get(TradingIdea, idea_id)
        assert idea is not None
        markups = [
            idea_context_keyboard(idea, watched=False, followed=False),
            idea_context_keyboard(idea, watched=True, followed=True),
            lifecycle_context_keyboard(idea, closed=False),
            lifecycle_context_keyboard(idea, closed=True),
            instrument_context_keyboard("SBER", watched=False, idea_id=idea.id),
            instrument_analysis_keyboard("SBER", watched=True),
            top_ideas_keyboard([idea]),
            watchlist_keyboard(("SBER",), page=0, total_pages=1),
            signal_history_keyboard("SBER", watched=True, page=0, total_pages=2),
            results_keyboard("win", page=0, total_pages=3),
            statistics_context_keyboard("30", "1m"),
            market_context_keyboard(),
            status_context_keyboard(),
        ]

    all_labels = [
        button.text for markup in markups for row in markup.inline_keyboard for button in row
    ]
    assert "👁 Отслеживать акцию" in all_labels
    assert "✅ Акция отслеживается" in all_labels
    assert "⭐ Следить за идеей" in all_labels
    assert "✅ Слежу за идеей" in all_labels
    assert "🏠 Главное меню" in all_labels
    assert "⬅️ Назад" in all_labels or "⬅️ К идеям" in all_labels
    assert "🧠 Gemini vs Quant" in all_labels
    for markup in markups:
        assert all(len(value.encode()) <= 64 for value in callback_values(markup))
    await engine.dispose()


@pytest.mark.asyncio
async def test_watch_and_follow_are_idempotent_owned_and_survive_service_restart() -> None:
    engine, factory, idea_id = await seeded_context()
    first = TelegramContextService(factory)

    await first.set_watch(101, "SBER", enabled=True)
    await first.set_watch(101, "SBER", enabled=True)
    await first.set_idea_follow(101, idea_id, enabled=True)
    await first.set_idea_follow(101, idea_id, enabled=True)
    await first.set_idea_follow(202, idea_id, enabled=False)

    async with factory() as session:
        watch_count = await session.scalar(select(func.count()).select_from(WatchlistItem))
        follow_count = await session.scalar(select(func.count()).select_from(IdeaFollow))
    assert watch_count == 1
    assert follow_count == 1
    assert (await first.idea_context(101, idea_id)).followed
    assert not (await first.idea_context(202, idea_id)).followed

    after_restart = TelegramContextService(factory)
    restored = await after_restart.idea_context(101, idea_id)
    assert restored.watched
    assert restored.followed

    await after_restart.set_watch(202, "SBER", enabled=True)
    await after_restart.set_idea_follow(202, idea_id, enabled=True)
    await after_restart.set_watch(202, "SBER", enabled=False)
    await after_restart.set_idea_follow(202, idea_id, enabled=False)
    assert (await after_restart.idea_context(101, idea_id)).watched
    assert (await after_restart.idea_context(101, idea_id)).followed
    second_user = await after_restart.idea_context(202, idea_id)
    assert not second_user.watched
    assert not second_user.followed
    await engine.dispose()


@pytest.mark.asyncio
async def test_invalid_ticker_missing_idea_and_deleted_idea_are_rejected() -> None:
    engine, factory, idea_id = await seeded_context()
    context = TelegramContextService(factory)

    with pytest.raises(ContextObjectNotFound):
        await context.set_watch(101, "SBER;DROP", enabled=True)
    with pytest.raises(ContextObjectNotFound):
        await context.idea_context(101, 999_999)
    with pytest.raises(ContextObjectNotFound):
        await context.idea_context(101, 10**40)
    async with factory() as session, session.begin():
        idea = await session.get(TradingIdea, idea_id)
        assert idea is not None
        await session.delete(idea)
    with pytest.raises(ContextObjectNotFound):
        await TelegramContextService(factory).idea_context(101, idea_id)
    await engine.dispose()


@pytest.mark.asyncio
async def test_follow_action_rechecks_current_lifecycle_status() -> None:
    engine, factory, idea_id = await seeded_context()
    context = TelegramContextService(factory)
    await context.set_idea_follow(101, idea_id, enabled=True)
    async with factory() as session, session.begin():
        idea = await session.get(TradingIdea, idea_id)
        assert idea is not None
        idea.status = "TP_HIT"

    with pytest.raises(ContextActionExpired):
        await context.set_idea_follow(202, idea_id, enabled=True)
    unfollowed = await context.set_idea_follow(101, idea_id, enabled=False)
    assert not unfollowed.followed
    await engine.dispose()


@pytest.mark.asyncio
async def test_watchlist_and_results_pagination_are_bounded() -> None:
    tickers = ("SBER", "GAZP", "LKOH", "PLZL", "MOEX", "NVTK", "ROSN")
    engine, factory, _ = await seeded_context(tickers=tickers)
    context = TelegramContextService(factory)
    for ticker in tickers:
        await context.set_watch(101, ticker, enabled=True)
    first = await context.watchlist_page(101, 0)
    second = await context.watchlist_page(101, 1)
    overflow = await context.watchlist_page(101, 99)

    assert len(first.items) == 5
    assert len(second.items) == 2
    assert second.page == overflow.page == 1
    assert second.total_pages == 2

    base = candidate()
    async with factory() as session, session.begin():
        for index, ticker in enumerate(tickers[1:], start=1):
            data = replace(
                base,
                ticker=ticker,
                instrument_name=ticker,
                source_candle_begin=NOW + timedelta(minutes=index),
                created_at=NOW + timedelta(minutes=index),
            )
            created = await create_or_update_idea(
                session,
                data,
                material_hash=idea_material_hash(data),
                confidence_delta=7.5,
            )
            created.idea.status = "TP_HIT"
            created.idea.activated_at = NOW
            created.idea.activation_price = created.idea.entry_price_from
            created.idea.close_price = created.idea.take_profit
            created.idea.closed_at = NOW + timedelta(hours=1)
    results_first = await context.results_page(101, "win", 0)
    results_second = await context.results_page(101, "win", 1)
    assert len(results_first.items) == 5
    assert len(results_second.items) == 1
    assert results_second.total_pages == 2
    await engine.dispose()


def callback_update(data: str, *, markup=None) -> Update:
    user = User(id=101, is_bot=False, first_name="Tester", username="first")
    message = Message(
        message_id=10,
        date=datetime.now(UTC),
        chat=Chat(id=101, type="private"),
        from_user=user,
        text="context card",
        reply_markup=markup,
    )
    return Update(
        update_id=1,
        callback_query=CallbackQuery(
            id="callback-1",
            from_user=user,
            chat_instance="instance",
            message=message,
            data=data,
        ),
    )


@pytest.mark.asyncio
async def test_callback_routing_follow_uses_edit_in_place_and_home_remains_available() -> None:
    engine, factory, idea_id = await seeded_context()
    async with factory() as session:
        idea = await session.get(TradingIdea, idea_id)
        assert idea is not None
        markup = idea_context_keyboard(idea, watched=False, followed=False)
    settings = Settings(_env_file=None)
    services = BotServices(
        settings=settings,
        session_factory=factory,
        ingestion=NoopIngestion(),
        signals=NoopSignals(),
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(create_router(services))
    bot = RecordingBot()

    await dispatcher.feed_update(bot, callback_update(f"idea_follow:{idea_id}", markup=markup))
    await dispatcher.feed_update(bot, callback_update(f"idea_follow:{idea_id}", markup=markup))
    assert any(isinstance(method, EditMessageReplyMarkup) for method in bot.methods)
    assert not any(isinstance(method, SendMessage) for method in bot.methods)
    assert (await TelegramContextService(factory).idea_context(101, idea_id)).followed
    async with factory() as session:
        follow_count = await session.scalar(select(func.count()).select_from(IdeaFollow))
    assert follow_count == 1

    bot.methods.clear()
    await dispatcher.feed_update(bot, callback_update("ideas:0"))
    assert any(isinstance(method, EditMessageText) for method in bot.methods)
    assert not any(isinstance(method, SendMessage) for method in bot.methods)

    bot.methods.clear()
    await dispatcher.feed_update(bot, callback_update("home"))
    assert any(isinstance(method, SendMessage) for method in bot.methods)
    assert any(isinstance(method, AnswerCallbackQuery) for method in bot.methods)
    await bot.session.close()
    await engine.dispose()


@pytest.mark.asyncio
async def test_missing_idea_callback_returns_alert_without_editing_message() -> None:
    engine, factory, _ = await seeded_context()
    services = BotServices(
        settings=Settings(_env_file=None),
        session_factory=factory,
        ingestion=NoopIngestion(),
        signals=NoopSignals(),
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(create_router(services))
    bot = RecordingBot()

    await dispatcher.feed_update(bot, callback_update("idea:999999"))

    alerts = [method for method in bot.methods if isinstance(method, AnswerCallbackQuery)]
    assert alerts and alerts[-1].show_alert
    assert not any(isinstance(method, EditMessageReplyMarkup) for method in bot.methods)
    await bot.session.close()
    await engine.dispose()
