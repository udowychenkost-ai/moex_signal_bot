from __future__ import annotations

from dataclasses import dataclass

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.domain import InsufficientDataError, MoexApiError, UnknownTickerError
from app.ingestion import IngestionService
from app.models import TelegramUser
from app.repositories import (
    add_watchlist_item,
    ensure_user,
    get_instrument,
    get_watchlist,
    remove_watchlist_item,
    update_user_settings,
)
from app.signals import SignalService, format_signal

ALLOWED_TIMEFRAMES = {"5m", "15m", "1h", "1d", "1w"}


@dataclass(slots=True)
class BotServices:
    settings: Settings
    session_factory: async_sessionmaker[AsyncSession]
    ingestion: IngestionService
    signals: SignalService


def _tokens(message: Message) -> list[str]:
    return (message.text or "").strip().split()


async def _ensure_message_user(message: Message, services: BotServices) -> TelegramUser:
    if message.from_user is None:
        raise ValueError("Telegram user is missing")
    async with services.session_factory() as session, session.begin():
        return await ensure_user(
            session,
            message.from_user.id,
            message.from_user.username,
            services.settings.default_timeframe,
            services.settings.default_risk_per_trade_pct,
        )


def create_router(services: BotServices) -> Router:
    router = Router(name="moex-signal-bot")

    @router.message(Command("start"))
    async def start(message: Message) -> None:
        await _ensure_message_user(message, services)
        await message.answer(
            "Бот готов отслеживать ликвидные акции TQBR.\n\n"
            "<b>Команды</b>\n"
            "/signal SBER [15m] — сигнал сейчас\n"
            "/watchlist add SBER — добавить тикер\n"
            "/watchlist remove SBER — удалить тикер\n"
            "/watchlist — показать список\n"
            "/settings — текущие настройки\n"
            "/settings timeframe 1h — сменить интервал\n"
            "/settings risk 0.5 — риск на сделку, %\n\n"
            "⚠️ Не является индивидуальной инвестиционной рекомендацией."
        )

    @router.message(Command("signal"))
    async def signal(message: Message) -> None:
        user = await _ensure_message_user(message, services)
        tokens = _tokens(message)
        if len(tokens) < 2:
            await message.answer("Формат: <code>/signal SBER [15m]</code>")
            return
        secid = tokens[1].upper()
        timeframe = tokens[2].lower() if len(tokens) > 2 else user.default_timeframe
        if timeframe not in ALLOWED_TIMEFRAMES:
            await message.answer("Интервал: 5m, 15m, 1h, 1d или 1w")
            return
        status = await message.answer(f"Обновляю {secid} · {timeframe}…")
        try:
            await services.ingestion.refresh_ticker(secid, timeframe)
            generated = await services.signals.generate(
                secid, timeframe, risk_per_trade_pct=user.risk_per_trade_pct
            )
        except (UnknownTickerError, InsufficientDataError) as error:
            await status.edit_text(str(error))
        except MoexApiError:
            await status.edit_text("MOEX ISS временно недоступен. Попробуйте чуть позже.")
        else:
            await status.edit_text(format_signal(generated))

    @router.message(Command("watchlist"))
    async def watchlist(message: Message) -> None:
        user = await _ensure_message_user(message, services)
        tokens = _tokens(message)
        if len(tokens) == 1:
            async with services.session_factory() as session:
                items = await get_watchlist(session, user.telegram_id)
            text = ", ".join(items) if items else "Список пока пуст"
            await message.answer(f"<b>Watchlist:</b> {text}")
            return
        if len(tokens) != 3 or tokens[1].lower() not in {"add", "remove"}:
            await message.answer("Формат: <code>/watchlist add SBER</code> или remove")
            return
        action, secid = tokens[1].lower(), tokens[2].upper()
        async with services.session_factory() as session, session.begin():
            if action == "add":
                if await get_instrument(session, secid) is None:
                    await message.answer(f"{secid} не входит в текущую вселенную MVP")
                    return
                changed = await add_watchlist_item(session, user.telegram_id, secid)
            else:
                changed = await remove_watchlist_item(session, user.telegram_id, secid)
        if action == "add":
            await message.answer(f"{secid} добавлен" if changed else f"{secid} уже в watchlist")
        else:
            await message.answer(f"{secid} удалён" if changed else f"{secid} не найден")

    @router.message(Command("settings"))
    async def settings(message: Message) -> None:
        user = await _ensure_message_user(message, services)
        tokens = _tokens(message)
        if len(tokens) == 1:
            await message.answer(
                f"Таймфрейм: <b>{user.default_timeframe}</b>\n"
                f"Риск на сделку: <b>{user.risk_per_trade_pct:.2f}%</b>\n"
                f"Профиль: <b>{user.risk_profile}</b>"
            )
            return
        if len(tokens) != 3:
            await message.answer("Пример: <code>/settings timeframe 1h</code>")
            return
        key, value = tokens[1].lower(), tokens[2].lower()
        timeframe: str | None = None
        risk_pct: float | None = None
        if key == "timeframe" and value in ALLOWED_TIMEFRAMES:
            timeframe = value
        elif key == "risk":
            try:
                risk_pct = float(value.replace(",", "."))
            except ValueError:
                risk_pct = None
            if risk_pct is None or not 0.1 <= risk_pct <= 10:
                await message.answer("Риск должен быть от 0.1% до 10%")
                return
        else:
            await message.answer("Поддерживаются timeframe (5m…1w) и risk (0.1…10)")
            return
        async with services.session_factory() as session, session.begin():
            await update_user_settings(
                session, user.telegram_id, timeframe=timeframe, risk_pct=risk_pct
            )
        await message.answer("Настройки сохранены")

    @router.message(Command("portfolio"))
    async def portfolio(message: Message) -> None:
        await message.answer(
            "Paper trading будет добавлен на этапе трекинга позиций. "
            "MVP уже сохраняет все сигналы для последующего бэктеста."
        )

    return router

