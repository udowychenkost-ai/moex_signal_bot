from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from sqlalchemy import select

from app.actual_trades import (
    ActualActionConfirmation,
    ActualTradeAlreadyExists,
    ActualTradeClosed,
    ActualTradeConfirmation,
    ActualTradeError,
    ActualTradeService,
)
from app.bot import (
    BotServices,
    create_router,
    parse_actual_execution_time,
    parse_actual_number,
    parse_actual_position,
)
from app.config import Settings
from app.db import create_engine_and_session, init_db
from app.journal import create_idea_journal, create_model_trade
from app.models import ActualTradeJournal, TradeEventJournal
from app.telegram_ui import v24_actual_position_keyboard, v24_entry_confirmation_keyboard
from app.v24_domain import ActualTradeAction, ActualTradeStatus, FillStatus, TradeEventType

SIGNAL_TIME = datetime(2026, 9, 1, 8, 30, tzinfo=UTC)


class NoopService:
    pass


class RecordingBot(Bot):
    def __init__(self) -> None:
        super().__init__("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefgh")
        self.methods: list[object] = []
        self.next_message_id = 500

    async def __call__(self, method, request_timeout=None):
        del request_timeout
        self.methods.append(method)
        if isinstance(method, SendMessage):
            self.next_message_id += 1
            return Message(
                message_id=self.next_message_id,
                date=datetime.now(UTC),
                chat=Chat(id=int(method.chat_id), type="private"),
                text=method.text,
            ).as_(self)
        return True


def callback_update(data: str, *, update_id: int) -> Update:
    user = User(id=101, is_bot=False, first_name="Tester")
    return Update(
        update_id=update_id,
        callback_query=CallbackQuery(
            id=f"callback-{update_id}",
            from_user=user,
            chat_instance="instance",
            message=Message(
                message_id=update_id,
                date=datetime.now(UTC),
                chat=Chat(id=101, type="private"),
                from_user=user,
                text="v2.4 idea",
            ),
            data=data,
        ),
    )


def message_update(text: str, *, update_id: int) -> Update:
    user = User(id=101, is_bot=False, first_name="Tester")
    return Update(
        update_id=update_id,
        message=Message(
            message_id=update_id,
            date=datetime.now(UTC),
            chat=Chat(id=101, type="private"),
            from_user=user,
            text=text,
        ),
    )


async def seeded_service():
    engine, factory = create_engine_and_session("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    async with factory() as session, session.begin():
        idea, _ = await create_idea_journal(
            session,
            signal_datetime=SIGNAL_TIME,
            ticker="SBER",
            direction="LONG",
            journal_values={
                "initial_stop": 95.0,
                "tp1": 110.0,
                "tp2": 115.0,
            },
        )
        await create_model_trade(
            session,
            trade_id=idea.trade_id,
            fill_status=FillStatus.FILLED,
            values={"model_entry": 99.0},
        )
    return engine, factory, ActualTradeService(factory), idea.trade_id


def test_actual_input_parsers_require_explicit_units_and_timezone() -> None:
    assert parse_actual_number("268,45") == 268.45
    assert parse_actual_position("100 шт") == (100.0, None)
    assert parse_actual_position("25 000 руб") == (None, 25_000.0)
    with pytest.raises(ValueError, match="единицу"):
        parse_actual_position("100")
    with pytest.raises(ValueError):
        parse_actual_number("nan")

    parsed = parse_actual_execution_time(
        "14:35",
        now=datetime(2026, 9, 1, 10, 0, tzinfo=UTC),
    )
    assert parsed.isoformat() == "2026-09-01T14:35:00+03:00"


@pytest.mark.asyncio
async def test_confirmed_entry_is_idempotent_separate_and_auditable() -> None:
    engine, factory, service, trade_id = await seeded_service()
    confirmation = ActualTradeConfirmation(
        trade_id=trade_id,
        telegram_id=101,
        confirmation_key="telegram-entry-1",
        entry_time=SIGNAL_TIME,
        entry_price=100.0,
        position_shares=10.0,
        commission_rub=5.0,
    )
    try:
        first = await service.confirm_entry(confirmation)
        duplicate = await service.confirm_entry(confirmation)

        assert first.created
        assert not duplicate.created
        assert first.actual.actual_position_rub == 1_000.0
        assert first.actual.entry_slippage_bps == pytest.approx(101.0101)
        assert first.actual.initial_risk_rub == 50.0
        async with factory() as session:
            events = list(
                (
                    await session.execute(
                        select(TradeEventJournal).where(
                            TradeEventJournal.trade_id == trade_id
                        )
                    )
                ).scalars()
            )
        assert len(events) == 1
        assert events[0].event_type == TradeEventType.ENTRY.value
        assert events[0].confirmed_by_telegram_id == 101
        assert events[0].confirmation_key == "telegram-entry-1"
        assert events[0].position_after == 10.0

        with pytest.raises(ActualTradeAlreadyExists):
            await service.confirm_entry(replace(confirmation, confirmation_key="telegram-entry-2"))
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_actual_actions_are_append_only_and_never_mutate_initial_record() -> None:
    engine, factory, service, trade_id = await seeded_service()
    try:
        entry = await service.confirm_entry(
            ActualTradeConfirmation(
                trade_id=trade_id,
                telegram_id=101,
                confirmation_key="entry",
                entry_time=SIGNAL_TIME,
                entry_price=100.0,
                position_shares=10.0,
            )
        )
        actual_id = entry.actual.actual_trade_id
        break_even = ActualActionConfirmation(
            actual_trade_id=actual_id,
            telegram_id=101,
            confirmation_key="event-break-even",
            action=ActualTradeAction.BREAK_EVEN,
            event_time=SIGNAL_TIME,
        )
        first_event = await service.record_action(break_even)
        repeated_event = await service.record_action(break_even)
        assert first_event.created
        assert not repeated_event.created

        await service.record_action(
            ActualActionConfirmation(
                actual_trade_id=actual_id,
                telegram_id=101,
                confirmation_key="event-partial",
                action=ActualTradeAction.PARTIAL_CLOSE,
                event_time=SIGNAL_TIME,
                current_price=105.0,
                position_after=4.0,
            )
        )
        interim = await service.get_position(actual_id, telegram_id=101)
        assert interim.status is ActualTradeStatus.OPEN
        assert interim.current_stop == 100.0
        assert interim.position_value == 4.0

        await service.record_action(
            ActualActionConfirmation(
                actual_trade_id=actual_id,
                telegram_id=101,
                confirmation_key="event-close",
                action=ActualTradeAction.FULL_CLOSE,
                event_time=SIGNAL_TIME,
                current_price=108.0,
            )
        )
        closed = await service.get_position(actual_id, telegram_id=101)
        assert closed.status is ActualTradeStatus.CLOSED
        assert closed.position_value == 0.0
        with pytest.raises(ActualTradeClosed):
            await service.record_action(
                ActualActionConfirmation(
                    actual_trade_id=actual_id,
                    telegram_id=101,
                    confirmation_key="event-after-close",
                    action=ActualTradeAction.HOLD,
                    event_time=SIGNAL_TIME,
                )
            )
        async with factory() as session:
            persisted = await session.get(ActualTradeJournal, actual_id)
            assert persisted is not None
            assert persisted.final_exit_time is None
            assert persisted.actual_exit is None
            assert persisted.actual_position_shares == 10.0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_actual_events_require_owner_and_real_values() -> None:
    engine, _, service, trade_id = await seeded_service()
    try:
        entry = await service.confirm_entry(
            ActualTradeConfirmation(
                trade_id=trade_id,
                telegram_id=101,
                confirmation_key="entry-owner",
                entry_time=SIGNAL_TIME,
                entry_price=100.0,
                position_rub=10_000.0,
            )
        )
        with pytest.raises(ActualTradeError):
            await service.record_action(
                ActualActionConfirmation(
                    actual_trade_id=entry.actual.actual_trade_id,
                    telegram_id=202,
                    confirmation_key="wrong-owner",
                    action=ActualTradeAction.HOLD,
                    event_time=SIGNAL_TIME,
                )
            )
        with pytest.raises(ActualTradeError, match="цена и остаток"):
            await service.record_action(
                ActualActionConfirmation(
                    actual_trade_id=entry.actual.actual_trade_id,
                    telegram_id=101,
                    confirmation_key="missing-fill",
                    action=ActualTradeAction.REDUCE,
                    event_time=SIGNAL_TIME,
                )
            )
    finally:
        await engine.dispose()


def test_actual_trade_keyboards_expose_only_manual_journal_actions() -> None:
    trade_id = "20260901-SBER-LONG-01"
    entry = v24_entry_confirmation_keyboard(trade_id)
    entry_buttons = [button for row in entry.inline_keyboard for button in row]
    assert any(button.text == "✅ Я вошёл в сделку" for button in entry_buttons)
    assert any(button.callback_data == f"actual_enter:{trade_id}" for button in entry_buttons)

    active = v24_actual_position_keyboard(f"A-{trade_id}")
    callbacks = [
        button.callback_data
        for row in active.inline_keyboard
        for button in row
        if button.callback_data
    ]
    assert all(len(value.encode()) <= 64 for value in callbacks)
    assert {
        f"actual_action:{action.value}:A-{trade_id}" for action in ActualTradeAction
    }.issubset(callbacks)


@pytest.mark.asyncio
async def test_telegram_entry_wizard_persists_only_after_all_user_confirmations() -> None:
    engine, factory, service, trade_id = await seeded_service()
    dispatcher = Dispatcher()
    dispatcher.include_router(
        create_router(
            BotServices(
                settings=Settings(_env_file=None),
                session_factory=factory,
                ingestion=NoopService(),  # type: ignore[arg-type]
                signals=NoopService(),  # type: ignore[arg-type]
                actual_trades=service,
            )
        )
    )
    bot = RecordingBot()
    try:
        await dispatcher.feed_update(bot, callback_update(f"actual_enter:{trade_id}", update_id=1))
        async with factory() as session:
            assert (await session.scalar(select(ActualTradeJournal))) is None

        for update_id, value in enumerate(
            ("100,25", "10 шт", "01.09.2026 12:30", "5,50"),
            start=2,
        ):
            await dispatcher.feed_update(bot, message_update(value, update_id=update_id))

        async with factory() as session:
            actual = await session.scalar(select(ActualTradeJournal))
            events = list((await session.execute(select(TradeEventJournal))).scalars())
        assert actual is not None
        assert actual.actual_entry == 100.25
        assert actual.actual_position_shares == 10.0
        assert actual.actual_entry_costs == 5.5
        assert len(events) == 1
        assert events[0].reason == "USER_CONFIRMED_ENTRY"
        sent = [item for item in bot.methods if isinstance(item, SendMessage)]
        assert any("Фактический вход записан" in item.text for item in sent)
    finally:
        await bot.session.close()
        await engine.dispose()
