from __future__ import annotations

from dataclasses import dataclass

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.domain import InsufficientDataError, MoexApiError, UnknownTickerError
from app.forward import (
    format_application_status,
    format_idea_history,
    format_open_ideas,
    format_statistics,
)
from app.idea_repository import list_open_ideas
from app.ingestion import IngestionService
from app.models import TelegramUser
from app.operations import OperationalService
from app.paper import PaperTradingService, format_paper_summary
from app.reporting import ReportingService, format_best_ideas, format_idea_details
from app.repositories import (
    add_watchlist_item,
    ensure_user,
    get_active_instrument,
    get_watchlist,
    remove_watchlist_item,
    update_user_settings,
)
from app.signals import SignalService, format_signal

ALLOWED_TIMEFRAMES = {"5m", "15m", "1h", "4h", "1d", "1w"}


@dataclass(slots=True)
class BotServices:
    settings: Settings
    session_factory: async_sessionmaker[AsyncSession]
    ingestion: IngestionService
    signals: SignalService
    reporting: ReportingService | None = None
    paper: PaperTradingService | None = None
    operations: OperationalService | None = None


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔥 Лучшие идеи")],
            [KeyboardButton(text="📊 Мои идеи"), KeyboardButton(text="⚙️ Настройки")],
        ],
        resize_keyboard=True,
    )


def idea_details_keyboard(ideas: list[object]) -> InlineKeyboardMarkup | None:
    rows = [
        [
            InlineKeyboardButton(
                text=f"Подробнее {idea.ticker}",
                callback_data=f"idea:{idea.id}",
            )
        ]
        for idea in ideas[:8]
        if getattr(idea, "id", None) is not None
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def settings_text(user: TelegramUser, services: BotServices) -> str:
    frequency_labels = {
        "hourly": "каждый час",
        "3h": "каждые 3 часа",
        "daily": "раз в день",
        "strong": "только новые сильные идеи",
        "off": "выключено",
    }
    horizon_labels = {
        "INTRADAY_1D": "1 день",
        "SWING_5D": "5 дней",
        "POSITION_1M": "1 месяц",
        "all": "все",
    }
    return (
        "<b>Настройки идей</b>\n"
        f"Частота: <b>{frequency_labels[user.report_frequency]}</b>\n"
        f"Срок: <b>{horizon_labels[user.idea_horizon]}</b>\n"
        f"Риск: <b>{user.risk_per_trade_pct:.2f}%</b>\n"
        f"Минимальная уверенность: <b>{user.minimum_confidence:.0f}%</b>\n\n"
        "Изменить:\n"
        "<code>/settings frequency hourly|3h|daily|strong|off</code>\n"
        "<code>/settings horizon 1d|5d|1m|all</code>\n"
        "<code>/settings risk 0.5</code>\n"
        "<code>/settings confidence 70</code>\n\n"
        f"Системная модель: {services.settings.technical_scoring_model}; "
        f"TP/SL: {services.settings.risk_method}"
    )


def _tokens(message: Message) -> list[str]:
    return (message.text or "").strip().split()


async def _answer_long(message: Message, text: str, *, limit: int = 3_500) -> None:
    """Split line-oriented HTML without cutting an individual tag in half."""
    chunks: list[str] = []
    current: list[str] = []
    current_length = 0
    for line in text.splitlines():
        added = len(line) + (1 if current else 0)
        if current and current_length + added > limit:
            chunks.append("\n".join(current))
            current = []
            current_length = 0
        current.append(line)
        current_length += len(line) + (1 if current_length else 0)
    if current:
        chunks.append("\n".join(current))
    for chunk in chunks:
        await message.answer(chunk)


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
            services.settings.default_report_frequency,
            services.settings.default_idea_horizon,
            services.settings.default_minimum_confidence,
        )


def create_router(services: BotServices) -> Router:
    router = Router(name="moex-signal-bot")

    @router.message(Command("start"))
    async def start(message: Message) -> None:
        await _ensure_message_user(message, services)
        await message.answer(
            "Бот анализирует рынок независимо от частоты ваших отчётов.\n\n"
            "<b>Команды</b>\n"
            "/best — лучшие торговые идеи\n"
            "/ideas — активные и ожидающие идеи\n"
            "/idea ID — идея и полная lifecycle-история\n"
            "/status — состояние приложения, MOEX и scheduler\n"
            "/stats — forward-статистика за 7/30 дней и всё время\n"
            "/signal SBER [15m] — сигнал сейчас\n"
            "/watchlist add SBER — добавить тикер\n"
            "/watchlist remove SBER — удалить тикер\n"
            "/watchlist — показать список\n"
            "/settings — текущие настройки\n"
            "/settings timeframe 1h — сменить интервал\n"
            "/settings risk 0.5 — риск на сделку, %\n\n"
            "⚠️ Не является индивидуальной инвестиционной рекомендацией.",
            reply_markup=main_menu(),
        )

    async def send_best_ideas(message: Message) -> None:
        user = await _ensure_message_user(message, services)
        if services.reporting is None:
            await message.answer("Сервис торговых идей ещё не запущен.")
            return
        ideas = await services.reporting.best_for_user(user)
        await message.answer(
            format_best_ideas(ideas),
            reply_markup=idea_details_keyboard(ideas),
        )

    @router.message(Command("best"))
    @router.message(F.text == "🔥 Лучшие идеи")
    async def best_ideas(message: Message) -> None:
        await send_best_ideas(message)

    @router.message(Command("ideas"))
    @router.message(F.text == "📊 Мои идеи")
    async def my_ideas(message: Message) -> None:
        await _ensure_message_user(message, services)
        async with services.session_factory() as session:
            ideas = await list_open_ideas(session, minimum_confidence=0, limit=100)
        await message.answer(format_open_ideas(ideas))

    @router.message(Command("idea"))
    async def idea_command(message: Message) -> None:
        await _ensure_message_user(message, services)
        if services.operations is None:
            await message.answer("Диагностический сервис недоступен.")
            return
        tokens = _tokens(message)
        if len(tokens) != 2 or not tokens[1].isdigit():
            await message.answer("Формат: <code>/idea 123</code>")
            return
        history = await services.operations.idea_history(int(tokens[1]))
        if history is None:
            await message.answer("Идея не найдена.")
            return
        await _answer_long(
            message,
            format_idea_history(history, timezone=services.settings.scheduler_timezone),
        )

    @router.message(Command("status"))
    async def application_status(message: Message) -> None:
        await _ensure_message_user(message, services)
        if services.operations is None:
            await message.answer("Диагностический сервис недоступен.")
            return
        status = await services.operations.status()
        await message.answer(
            format_application_status(status, timezone=services.settings.scheduler_timezone)
        )

    @router.message(Command("stats"))
    async def forward_stats(message: Message) -> None:
        await _ensure_message_user(message, services)
        if services.operations is None:
            await message.answer("Диагностический сервис недоступен.")
            return
        await message.answer(format_statistics(await services.operations.statistics()))

    @router.callback_query(F.data.startswith("idea:"))
    async def idea_details(callback: CallbackQuery) -> None:
        if services.reporting is None or callback.data is None:
            await callback.answer("Сервис идей недоступен", show_alert=True)
            return
        try:
            idea_id = int(callback.data.partition(":")[2])
        except ValueError:
            await callback.answer("Некорректный идентификатор", show_alert=True)
            return
        if callback.message is None:
            await callback.answer("Идея не найдена", show_alert=True)
            return
        if services.operations is not None:
            history = await services.operations.idea_history(idea_id)
            if history is None:
                await callback.answer("Идея не найдена", show_alert=True)
                return
            await _answer_long(
                callback.message,
                format_idea_history(history, timezone=services.settings.scheduler_timezone),
            )
        else:
            idea = await services.reporting.idea_details(idea_id)
            if idea is None:
                await callback.answer("Идея не найдена", show_alert=True)
                return
            await callback.message.answer(format_idea_details(idea))
        await callback.answer()

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
            await message.answer("Интервал: 5m, 15m, 1h, 4h, 1d или 1w")
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
                if await get_active_instrument(session, secid) is None:
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
    @router.message(F.text == "⚙️ Настройки")
    async def settings(message: Message) -> None:
        user = await _ensure_message_user(message, services)
        tokens = _tokens(message)
        if message.text == "⚙️ Настройки":
            tokens = ["/settings"]
        if len(tokens) == 1:
            await message.answer(settings_text(user, services))
            return
        if len(tokens) != 3:
            await message.answer("Пример: <code>/settings timeframe 1h</code>")
            return
        key, value = tokens[1].lower(), tokens[2].lower()
        timeframe: str | None = None
        risk_pct: float | None = None
        report_frequency: str | None = None
        idea_horizon: str | None = None
        minimum_confidence: float | None = None
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
        elif key == "frequency" and value in {"hourly", "3h", "daily", "strong", "off"}:
            report_frequency = value
        elif key == "horizon":
            horizon_values = {
                "1d": "INTRADAY_1D",
                "5d": "SWING_5D",
                "1m": "POSITION_1M",
                "all": "all",
            }
            idea_horizon = horizon_values.get(value)
            if idea_horizon is None:
                await message.answer("Срок: 1d, 5d, 1m или all")
                return
        elif key == "confidence":
            try:
                minimum_confidence = float(value.replace(",", "."))
            except ValueError:
                minimum_confidence = None
            if minimum_confidence is None or not 50 <= minimum_confidence <= 95:
                await message.answer("Уверенность должна быть от 50% до 95%")
                return
        else:
            await message.answer("Поддерживаются frequency, horizon, risk и confidence")
            return
        async with services.session_factory() as session, session.begin():
            await update_user_settings(
                session,
                user.telegram_id,
                timeframe=timeframe,
                risk_pct=risk_pct,
                report_frequency=report_frequency,
                idea_horizon=idea_horizon,
                minimum_confidence=minimum_confidence,
            )
        await message.answer("Настройки сохранены")

    @router.message(Command("portfolio"))
    async def portfolio(message: Message) -> None:
        if services.paper is None:
            await message.answer("Paper trading сейчас недоступен.")
            return
        await message.answer(format_paper_summary(await services.paper.summary()))

    return router
