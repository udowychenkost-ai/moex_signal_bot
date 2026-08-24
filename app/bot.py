from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai_ux import (
    format_current_ai_analysis,
    format_current_ai_unavailable,
    has_historical_ai_review,
)
from app.config import Settings
from app.domain import (
    InsufficientDataError,
    MoexApiError,
    StaleMarketDataError,
    UnknownTickerError,
)
from app.forward import (
    format_ai_analysis,
    format_application_status,
    format_fundamental_analysis,
    format_idea_history,
    format_idea_market_analysis,
    format_lifecycle_history,
    format_new_idea,
    format_open_ideas,
    format_statistics,
    format_technical_analysis,
)
from app.ingestion import IngestionService
from app.market_overview import MarketOverviewService, format_market_overview
from app.models import CandidateExperiment, SignalRecord, TelegramUser, TradingIdea
from app.on_demand_ai import OnDemandAIService
from app.operations import OperationalService, realized_r
from app.paper import PaperTradingService, format_paper_summary
from app.reporting import ReportingService, format_best_ideas, format_idea_details
from app.repositories import (
    add_watchlist_item,
    ensure_user,
    get_active_instrument,
    remove_watchlist_item,
    update_user_settings,
)
from app.signals import SignalService, format_signal
from app.telegram_context import (
    ContextAccessError,
    ContextActionExpired,
    ContextObjectNotFound,
    ContextPage,
    InstrumentContext,
    ResultItem,
    TelegramContextService,
    normalize_ticker,
)
from app.telegram_ui import (
    idea_context_keyboard,
    ideas_page_keyboard,
    instrument_analysis_keyboard,
    instrument_context_keyboard,
    market_context_keyboard,
    results_keyboard,
    signal_history_keyboard,
    statistics_context_keyboard,
    status_context_keyboard,
    top_ideas_keyboard,
    watchlist_keyboard,
)

ALLOWED_TIMEFRAMES = {"5m", "15m", "1h", "4h", "1d", "1w"}
logger = logging.getLogger(__name__)


@dataclass(slots=True)
class BotServices:
    settings: Settings
    session_factory: async_sessionmaker[AsyncSession]
    ingestion: IngestionService
    signals: SignalService
    reporting: ReportingService | None = None
    paper: PaperTradingService | None = None
    operations: OperationalService | None = None
    market_overview: MarketOverviewService | None = None
    on_demand_ai: OnDemandAIService | None = None


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔥 Лучшие идеи"), KeyboardButton(text="👁 Отслеживаемые")],
            [
                KeyboardButton(text="📊 Активные идеи"),
                KeyboardButton(text="📒 Результаты сигналов"),
            ],
            [KeyboardButton(text="🌍 Рынок сейчас"), KeyboardButton(text="🔎 Проверить акцию")],
            [KeyboardButton(text="📈 Статистика"), KeyboardButton(text="⚙️ Настройки")],
            [KeyboardButton(text="🩺 Система")],
        ],
        resize_keyboard=True,
    )


def idea_details_keyboard(ideas: list[object]) -> InlineKeyboardMarkup | None:
    typed = [idea for idea in ideas if isinstance(idea, TradingIdea)]
    return top_ideas_keyboard(typed) if typed else None


def idea_sections_keyboard(idea_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🧠 AI-анализ", callback_data=f"idea_ai:{idea_id}"),
                InlineKeyboardButton(text="📊 Теханализ", callback_data=f"idea_tech:{idea_id}"),
            ],
            [
                InlineKeyboardButton(text="🏢 Фундаментал", callback_data=f"idea_fund:{idea_id}"),
                InlineKeyboardButton(text="🌍 Рынок", callback_data=f"idea_market:{idea_id}"),
            ],
            [InlineKeyboardButton(text="📜 История идеи", callback_data=f"idea_history:{idea_id}")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="best:menu")],
        ]
    )


def best_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Сегодня", callback_data="best:today")],
            [
                InlineKeyboardButton(text="1 день", callback_data="best:INTRADAY_1D"),
                InlineKeyboardButton(text="5 дней", callback_data="best:SWING_5D"),
                InlineKeyboardButton(text="1 месяц", callback_data="best:POSITION_1M"),
            ],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="home")],
        ]
    )


def statistics_menu_keyboard() -> InlineKeyboardMarkup:
    return statistics_context_keyboard()


def settings_menu_keyboard(user: TelegramUser | None = None) -> InlineKeyboardMarkup:
    frequency = (
        {
            "strong": "Только сильные",
            "hourly": "Каждый час",
            "3h": "Каждые 3 часа",
            "daily": "Раз в день",
            "off": "Выкл.",
        }[user.report_frequency]
        if user is not None
        else "—"
    )
    horizon = (
        {
            "INTRADAY_1D": "1D",
            "SWING_5D": "5D",
            "POSITION_1M": "1M",
            "all": "Все",
        }[user.idea_horizon]
        if user is not None
        else "—"
    )
    strength = f"{user.minimum_confidence:.0f}+" if user is not None else "—"
    risk = f"{user.risk_per_trade_pct:g}%" if user is not None else "—"
    ai_state = "ON ✅" if user is not None and user.ai_filter_enabled else "OFF"
    watch_state = "ON ✅" if user is not None and user.notify_watchlist else "OFF"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"📨 Отчёты: {frequency} ✅", callback_data="settings:frequency"
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"⏱ Горизонт: {horizon} ✅", callback_data="settings:horizon"
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"🎯 Min strength: {strength} ✅", callback_data="settings:strength"
                )
            ],
            [InlineKeyboardButton(text=f"💰 Риск: {risk} ✅", callback_data="settings:risk")],
            [InlineKeyboardButton(text="🔔 Уведомления", callback_data="settings:notifications")],
            [InlineKeyboardButton(text=f"🧠 AI filter: {ai_state}", callback_data="settings:ai")],
            [
                InlineKeyboardButton(
                    text=f"🔔 Watch notifications: {watch_state}",
                    callback_data="settings:notifications",
                )
            ],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="home")],
        ]
    )


def _selected(text: str, selected: bool) -> str:
    return f"✅ {text}" if selected else text


def settings_values_keyboard(section: str, user: TelegramUser) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if section == "frequency":
        choices = (
            ("Только сильные новые идеи", "strong"),
            ("Каждый час", "hourly"),
            ("Каждые 3 часа", "3h"),
            ("Раз в день", "daily"),
            ("Выкл.", "off"),
        )
        rows = [
            [
                InlineKeyboardButton(
                    text=_selected(label, user.report_frequency == value),
                    callback_data=f"set_frequency:{value}",
                )
            ]
            for label, value in choices
        ]
    elif section == "horizon":
        choices = (
            ("1 день · RESEARCH", "INTRADAY_1D"),
            ("5 дней · RESEARCH", "SWING_5D"),
            ("1 месяц · PAPER", "POSITION_1M"),
            ("Все", "all"),
        )
        rows = [
            [
                InlineKeyboardButton(
                    text=_selected(label, user.idea_horizon == value),
                    callback_data=f"set_horizon:{value}",
                )
            ]
            for label, value in choices
        ]
    elif section == "strength":
        rows = [
            [
                InlineKeyboardButton(
                    text=_selected(f"{value}+", user.minimum_confidence == value),
                    callback_data=f"set_strength:{value}",
                )
                for value in values
            ]
            for values in ((70, 75, 80), (85, 90))
        ]
    elif section == "risk":
        rows = [
            [
                InlineKeyboardButton(
                    text=_selected(f"{value}%", user.risk_per_trade_pct == value),
                    callback_data=f"set_risk:{value}",
                )
                for value in (0.5, 1.0, 2.0)
            ]
        ]
    elif section == "notifications":
        choices = (
            ("Новая идея", "new", user.notify_new_idea),
            ("Activation", "activation", user.notify_activation),
            ("TP", "tp", user.notify_tp),
            ("SL", "sl", user.notify_sl),
            ("Expiry", "expiry", user.notify_expiry),
            ("Daily summary", "daily", user.notify_daily_summary),
            ("Watchlist", "watch", user.notify_watchlist),
        )
        rows = [
            [
                InlineKeyboardButton(
                    text=f"{label} {'ON ✅' if enabled else 'OFF'}",
                    callback_data=f"toggle_notify:{key}",
                )
            ]
            for label, key, enabled in choices
        ]
    elif section == "ai":
        rows = [
            [
                InlineKeyboardButton(
                    text=_selected("ON", user.ai_filter_enabled), callback_data="set_ai:1"
                ),
                InlineKeyboardButton(
                    text=_selected("OFF", not user.ai_filter_enabled), callback_data="set_ai:0"
                ),
            ]
        ]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="settings:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
        f"AI-фильтр ленты: <b>{'ON' if user.ai_filter_enabled else 'OFF'}</b>\n\n"
        f"Watchlist-уведомления: <b>{'ON' if user.notify_watchlist else 'OFF'}</b>\n\n"
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


async def _answer_long(
    message: Message,
    text: str,
    *,
    limit: int = 3_500,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
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
    for index, chunk in enumerate(chunks):
        if reply_markup is not None and index == len(chunks) - 1:
            await message.answer(chunk, reply_markup=reply_markup)
        else:
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


async def _ensure_callback_user(callback: CallbackQuery, services: BotServices) -> TelegramUser:
    async with services.session_factory() as session, session.begin():
        return await ensure_user(
            session,
            callback.from_user.id,
            callback.from_user.username,
            services.settings.default_timeframe,
            services.settings.default_risk_per_trade_pct,
            services.settings.default_report_frequency,
            services.settings.default_idea_horizon,
            services.settings.default_minimum_confidence,
        )


async def _edit_context(
    callback: CallbackQuery,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    if callback.message is None:
        return
    if len(text) > 3_500 and isinstance(callback.message, Message):
        await _answer_long(callback.message, text, reply_markup=reply_markup)
        return
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as error:
        if "message is not modified" not in str(error).lower():
            raise
        await callback.message.edit_reply_markup(reply_markup=reply_markup)


async def _edit_markup(
    callback: CallbackQuery,
    reply_markup: InlineKeyboardMarkup,
) -> None:
    if callback.message is None:
        return
    try:
        await callback.message.edit_reply_markup(reply_markup=reply_markup)
    except TelegramBadRequest as error:
        if "message is not modified" not in str(error).lower():
            raise


def _instrument_text(context: InstrumentContext) -> str:
    idea = context.latest_idea
    signal = context.latest_signal
    idea_line = (
        f"{idea.direction} · {idea.horizon} · {idea.status} · сила "
        f"{(idea.final_quality_score or idea.confidence):.0f}/100"
        if idea is not None
        else "нет сохранённой TradingIdea"
    )
    signal_line = (
        f"{signal.action} · {signal.timeframe} · {signal.confidence:.0f}/100"
        if signal is not None
        else "ещё нет рассчитанного сигнала"
    )
    return (
        f"👁 <b>{escape(context.instrument.secid)} "
        f"({escape(context.instrument.short_name)})</b>\n\n"
        f"Watchlist: <b>{'да' if context.watched else 'нет'}</b>\n"
        f"Последний сигнал: <b>{escape(signal_line)}</b>\n"
        f"Текущая идея: <b>{escape(idea_line)}</b>"
    )


def _results_text(result_filter: str, page: ContextPage[ResultItem]) -> str:
    labels = {
        "win": "✅ Плюсовые",
        "loss": "❌ Минусовые",
        "active": "⏳ Активные",
        "expired": "⌛ Истекшие",
        "missed": "🚫 Неактивированные",
        "airej": "🧠 AI отклонённые",
    }
    items = page.items
    lines = [f"📒 <b>{labels[result_filter]}</b>"]
    if not items:
        lines.append("\nЗаписей пока нет.")
    for item in items:
        status_label = {
            "AI_REJECT": "Отклонено AI",
        }.get(item.status, item.status)
        lines.append(
            f"\n• <b>{escape(item.ticker)}</b> · {escape(item.direction)} · "
            f"{escape(item.horizon)}\n  {escape(status_label)} · {item.score:.0f}/100"
        )
    lines.append(f"\nСтраница {page.page + 1}/{page.total_pages} · всего {page.total_items}")
    return "\n".join(lines)


def _signal_history_text(ticker: str, page: ContextPage[SignalRecord]) -> str:
    lines = [f"📜 <b>История сигналов {escape(ticker)}</b>"]
    items = page.items
    if not items:
        lines.append("\nСигналов пока нет.")
    for item in items:
        lines.append(
            f"\n• {item.created_at:%d.%m.%Y %H:%M} · <b>{item.action}</b> · "
            f"{item.timeframe} · {item.confidence:.0f}/100"
        )
    lines.append(f"\nСтраница {page.page + 1}/{page.total_pages}")
    return "\n".join(lines)


def create_router(services: BotServices) -> Router:
    router = Router(name="moex-signal-bot")
    context_service = TelegramContextService(services.session_factory)

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
            "Кнопка «🔎 Проверить акцию» запросит тикер без slash-команды.\n\n"
            "⚠️ Не является индивидуальной инвестиционной рекомендацией.",
            reply_markup=main_menu(),
        )

    @router.callback_query(F.data == "noop")
    async def noop(callback: CallbackQuery) -> None:
        await _ensure_callback_user(callback, services)
        await callback.answer()

    @router.callback_query(F.data == "home")
    async def home(callback: CallbackQuery) -> None:
        await _ensure_callback_user(callback, services)
        if callback.message is not None:
            await callback.message.answer("🏠 <b>Главное меню</b>", reply_markup=main_menu())
        await callback.answer()

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
        await _ensure_message_user(message, services)
        await message.answer(
            "🔥 <b>Лучшие идеи</b>\nВыберите период:", reply_markup=best_menu_keyboard()
        )

    @router.callback_query(F.data == "best:menu")
    async def best_menu(callback: CallbackQuery) -> None:
        await _ensure_callback_user(callback, services)
        if callback.message is not None:
            await _edit_context(
                callback,
                "🔥 <b>Лучшие идеи</b>\nВыберите период:",
                reply_markup=best_menu_keyboard(),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("best:"))
    async def best_filtered(callback: CallbackQuery) -> None:
        user = await _ensure_callback_user(callback, services)
        if callback.message is None or callback.data is None or services.reporting is None:
            await callback.answer("Сервис идей недоступен", show_alert=True)
            return
        value = callback.data.partition(":")[2]
        if value not in {"today", "INTRADAY_1D", "SWING_5D", "POSITION_1M"}:
            await callback.answer("Некорректный фильтр", show_alert=True)
            return
        created_after = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        ideas = await services.reporting.best_for_user(
            user,
            horizon=None if value == "today" else value,
            created_after=created_after if value == "today" else None,
        )
        await _edit_context(
            callback,
            format_best_ideas(ideas),
            reply_markup=top_ideas_keyboard(ideas, filter_key=value),
        )
        await callback.answer()

    @router.message(Command("ideas"))
    @router.message(F.text == "📊 Активные идеи")
    async def my_ideas(message: Message) -> None:
        user = await _ensure_message_user(message, services)
        page = await context_service.open_ideas_page(user.telegram_id, 0)
        await message.answer(
            format_open_ideas(list(page.items)),
            reply_markup=ideas_page_keyboard(
                page.items,
                page=page.page,
                total_pages=page.total_pages,
            ),
        )

    @router.callback_query(F.data.regexp(r"^ideas:[0-9]+$"))
    async def ideas_page(callback: CallbackQuery) -> None:
        user = await _ensure_callback_user(callback, services)
        if callback.data is None or callback.message is None:
            await callback.answer()
            return
        page = await context_service.open_ideas_page(
            user.telegram_id, int(callback.data.partition(":")[2])
        )
        await _edit_context(
            callback,
            format_open_ideas(list(page.items)),
            ideas_page_keyboard(page.items, page=page.page, total_pages=page.total_pages),
        )
        await callback.answer()

    @router.message(Command("idea"))
    async def idea_command(message: Message) -> None:
        user = await _ensure_message_user(message, services)
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
        context = await context_service.idea_context(user.telegram_id, history.idea.id)
        await _answer_long(
            message,
            format_idea_history(history, timezone=services.settings.scheduler_timezone),
            reply_markup=idea_context_keyboard(
                history.idea,
                watched=context.watched,
                followed=context.followed,
            ),
        )

    @router.message(Command("status"))
    @router.message(F.text == "🩺 Система")
    @router.message(F.text == "ℹ️ Статус системы")
    async def application_status(message: Message) -> None:
        await _ensure_message_user(message, services)
        if services.operations is None:
            await message.answer("Диагностический сервис недоступен.")
            return
        status = await services.operations.status()
        await message.answer(
            format_application_status(status, timezone=services.settings.scheduler_timezone),
            reply_markup=status_context_keyboard(),
        )

    @router.message(Command("stats"))
    @router.message(F.text == "📈 Статистика")
    async def forward_stats(message: Message) -> None:
        await _ensure_message_user(message, services)
        if services.operations is None:
            await message.answer("Диагностический сервис недоступен.")
            return
        await message.answer(
            "📈 <b>Статистика</b>\nВыберите период:",
            reply_markup=statistics_menu_keyboard(),
        )

    @router.callback_query(F.data.startswith("stats:"))
    async def forward_stats_period(callback: CallbackQuery) -> None:
        await _ensure_callback_user(callback, services)
        if callback.message is None or callback.data is None or services.operations is None:
            await callback.answer("Статистика недоступна", show_alert=True)
            return
        selected = callback.data.partition(":")[2]
        labels = {"7": "7 дней", "30": "30 дней", "all": "всё время"}
        periods = await services.operations.statistics()
        period = next((item for item in periods if item.label == labels.get(selected)), None)
        if period is None:
            await callback.answer("Период не найден", show_alert=True)
            return
        await _edit_context(
            callback,
            format_statistics((period,)),
            statistics_context_keyboard(selected, "all"),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("stats_h:"))
    async def forward_stats_horizon(callback: CallbackQuery) -> None:
        await _ensure_callback_user(callback, services)
        if callback.message is None or callback.data is None or services.operations is None:
            await callback.answer("Статистика недоступна", show_alert=True)
            return
        horizon = callback.data.partition(":")[2]
        if horizon not in {"INTRADAY_1D", "SWING_5D", "POSITION_1M"}:
            await callback.answer("Горизонт не найден", show_alert=True)
            return
        filtered = tuple(
            replace(
                period,
                horizons=tuple(row for row in period.horizons if row.horizon == horizon),
                experiment_cohorts=tuple(
                    row for row in period.experiment_cohorts if row.horizon == horizon
                ),
            )
            for period in await services.operations.statistics()
        )
        short_horizon = {
            "INTRADAY_1D": "1d",
            "SWING_5D": "5d",
            "POSITION_1M": "1m",
        }[horizon]
        await _edit_context(
            callback,
            format_statistics(filtered),
            statistics_context_keyboard("all", short_horizon),
        )
        await callback.answer()

    @router.callback_query(F.data.regexp(r"^stats_view:(7|30|all):(1d|5d|1m|all)$"))
    async def statistics_view(callback: CallbackQuery) -> None:
        await _ensure_callback_user(callback, services)
        if callback.message is None or callback.data is None or services.operations is None:
            await callback.answer("Статистика недоступна", show_alert=True)
            return
        _, period_key, horizon_key = callback.data.split(":")
        period_label = {"7": "7 дней", "30": "30 дней", "all": "всё время"}[period_key]
        horizon_value = {
            "1d": "INTRADAY_1D",
            "5d": "SWING_5D",
            "1m": "POSITION_1M",
            "all": None,
        }[horizon_key]
        period = next(
            item for item in await services.operations.statistics() if item.label == period_label
        )
        if horizon_value is not None:
            period = replace(
                period,
                horizons=tuple(row for row in period.horizons if row.horizon == horizon_value),
                experiment_cohorts=tuple(
                    row for row in period.experiment_cohorts if row.horizon == horizon_value
                ),
            )
        await _edit_context(
            callback,
            format_statistics((period,)),
            statistics_context_keyboard(period_key, horizon_key),
        )
        await callback.answer()

    @router.callback_query(
        F.data.regexp(r"^stats_break:(dir|best|worst|ai):(7|30|all):(1d|5d|1m|all)$")
    )
    async def statistics_breakdown(callback: CallbackQuery) -> None:
        await _ensure_callback_user(callback, services)
        if callback.message is None or callback.data is None or services.operations is None:
            await callback.answer("Статистика недоступна", show_alert=True)
            return
        _, mode, period_key, horizon_key = callback.data.split(":")
        if mode == "ai":
            cutoff = {
                "7": datetime.now(UTC) - timedelta(days=7),
                "30": datetime.now(UTC) - timedelta(days=30),
                "all": None,
            }[period_key]
            horizon_value = {
                "1d": "INTRADAY_1D",
                "5d": "SWING_5D",
                "1m": "POSITION_1M",
                "all": None,
            }[horizon_key]
            async with services.session_factory() as session:
                experiments = list(
                    await session.scalars(
                        select(CandidateExperiment).where(
                            CandidateExperiment.strategy_version
                            == services.settings.strategy_version
                        )
                    )
                )
            quant = [
                row
                for row in experiments
                if (
                    cutoff is None
                    or row.decision_at.replace(tzinfo=row.decision_at.tzinfo or UTC) >= cutoff
                )
                and (horizon_value is None or row.horizon == horizon_value)
            ]
            gemini = [
                row
                for row in quant
                if row.ai_provider == "gemini" and row.ai_verdict in {"STRONG_APPROVE", "APPROVE"}
            ]

            def cohort_line(name: str, rows: list[CandidateExperiment]) -> str:
                actual = [row.actual_r for row in rows if row.actual_r is not None]
                average = sum(actual) / len(actual) if actual else None
                return (
                    f"<b>{name}</b>\n"
                    f"Generated: {len(rows)} · Activated: "
                    f"{sum(row.activated_at is not None for row in rows)} · "
                    f"TP/SL: {sum(row.status == 'TP_HIT' for row in rows)}/"
                    f"{sum(row.status == 'SL_HIT' for row in rows)} · "
                    f"Avg R: {f'{average:+.2f}R' if average is not None else 'н/д'}"
                )

            text = "\n\n".join(
                (
                    "🧠 <b>Gemini vs Quant</b>",
                    cohort_line("ALL QUANT", quant),
                    cohort_line("GEMINI APPROVED", gemini),
                    "OpenAI rows не включены в Gemini cohort.",
                )
            )
        else:
            cutoff = {
                "7": datetime.now(UTC) - timedelta(days=7),
                "30": datetime.now(UTC) - timedelta(days=30),
                "all": None,
            }[period_key]
            horizon_value = {
                "1d": "INTRADAY_1D",
                "5d": "SWING_5D",
                "1m": "POSITION_1M",
                "all": None,
            }[horizon_key]
            async with services.session_factory() as session:
                ideas = list(
                    await session.scalars(
                        select(TradingIdea).where(
                            TradingIdea.strategy_version == services.settings.strategy_version
                        )
                    )
                )
            rows = [
                idea
                for idea in ideas
                if (
                    cutoff is None
                    or idea.created_at.replace(tzinfo=idea.created_at.tzinfo or UTC) >= cutoff
                )
                and (horizon_value is None or idea.horizon == horizon_value)
            ]
            groups: dict[str, list[float]] = {}
            for idea in rows:
                value = realized_r(idea)
                if value is not None:
                    key = idea.direction if mode == "dir" else idea.ticker
                    groups.setdefault(key, []).append(value)
            ranked = sorted(
                ((key, len(values), sum(values) / len(values)) for key, values in groups.items()),
                key=lambda row: row[2],
                reverse=mode != "worst",
            )[:10]
            title = {
                "dir": "📈 BUY vs SELL",
                "best": "🏆 Лучшие бумаги",
                "worst": "💩 Худшие бумаги",
            }[mode]
            lines = [f"{title}\n"]
            lines.extend(
                f"• <b>{escape(key)}</b>: {average:+.2f}R · n={count}"
                for key, count, average in ranked
            )
            if not ranked:
                lines.append("Недостаточно закрытых активированных идей.")
            text = "\n".join(lines)
        await _edit_context(
            callback,
            text,
            statistics_context_keyboard(period_key, horizon_key),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("idea:"))
    async def idea_details(callback: CallbackQuery) -> None:
        user = await _ensure_callback_user(callback, services)
        if callback.data is None:
            await callback.answer("Некорректная кнопка", show_alert=True)
            return
        try:
            idea_id = int(callback.data.partition(":")[2])
        except ValueError:
            await callback.answer("Некорректный идентификатор", show_alert=True)
            return
        if callback.message is None:
            await callback.answer("Идея не найдена", show_alert=True)
            return
        try:
            context = await context_service.idea_context(user.telegram_id, idea_id)
        except (ContextAccessError, ContextObjectNotFound) as error:
            await callback.answer(str(error), show_alert=True)
            return
        if services.operations is not None:
            history = await services.operations.idea_history(idea_id)
            if history is None:
                await callback.answer("Идея не найдена", show_alert=True)
                return
            await _edit_context(
                callback,
                format_new_idea(history.idea, timezone=services.settings.scheduler_timezone),
                reply_markup=idea_context_keyboard(
                    history.idea,
                    watched=context.watched,
                    followed=context.followed,
                ),
            )
        else:
            await _edit_context(
                callback,
                format_idea_details(context.idea),
                idea_context_keyboard(
                    context.idea,
                    watched=context.watched,
                    followed=context.followed,
                ),
            )
        await callback.answer()

    @router.callback_query(
        F.data.regexp(r"^idea_(ai|tech|fund|market|history|why|changes|result):[0-9]+$")
    )
    async def idea_section(callback: CallbackQuery) -> None:
        user = await _ensure_callback_user(callback, services)
        if callback.data is None or callback.message is None or services.operations is None:
            await callback.answer("Разбор недоступен", show_alert=True)
            return
        section, _, raw_id = callback.data.partition(":")
        if not raw_id.isdigit():
            await callback.answer("Некорректный идентификатор", show_alert=True)
            return
        idea_id = int(raw_id)
        try:
            context = await context_service.idea_context(user.telegram_id, idea_id)
        except (ContextAccessError, ContextObjectNotFound) as error:
            await callback.answer(str(error), show_alert=True)
            return
        history = await services.operations.idea_history(idea_id)
        if history is None:
            await callback.answer("Идея не найдена", show_alert=True)
            return
        formatters = {
            "idea_ai": lambda: format_ai_analysis(history),
            "idea_tech": lambda: format_technical_analysis(history),
            "idea_fund": lambda: format_fundamental_analysis(history),
            "idea_market": lambda: format_idea_market_analysis(history),
            "idea_history": lambda: format_lifecycle_history(
                history, timezone=services.settings.scheduler_timezone
            ),
            "idea_why": lambda: format_idea_details(history.idea),
            "idea_changes": lambda: format_lifecycle_history(
                history, timezone=services.settings.scheduler_timezone
            ),
            "idea_result": lambda: format_idea_history(
                history, timezone=services.settings.scheduler_timezone
            ),
        }
        await _edit_context(
            callback,
            formatters[section](),
            reply_markup=idea_context_keyboard(
                history.idea,
                watched=context.watched,
                followed=context.followed,
            ),
        )
        await callback.answer()

    @router.callback_query(F.data.regexp(r"^idea_ai_now:[0-9]+$"))
    async def idea_ai_now(callback: CallbackQuery) -> None:
        user = await _ensure_callback_user(callback, services)
        if callback.data is None or callback.message is None:
            await callback.answer()
            return
        if services.on_demand_ai is None:
            await callback.answer("Gemini analysis недоступен", show_alert=True)
            return
        idea_id = int(callback.data.partition(":")[2])
        try:
            context = await context_service.idea_context(user.telegram_id, idea_id)
        except (ContextAccessError, ContextObjectNotFound) as error:
            await callback.answer(str(error), show_alert=True)
            return
        if has_historical_ai_review(context.idea):
            await callback.answer("У идеи уже есть creation-time AI review", show_alert=True)
            return
        await callback.answer("Gemini анализирует текущее состояние…")
        await _edit_context(
            callback,
            f"⏳ <b>Gemini анализирует {escape(context.idea.ticker)}…</b>",
            idea_context_keyboard(
                context.idea,
                watched=context.watched,
                followed=context.followed,
            ),
        )
        try:
            result = await services.on_demand_ai.analyze(context.idea)
        except (
            InsufficientDataError,
            MoexApiError,
            StaleMarketDataError,
            UnknownTickerError,
        ) as error:
            text = format_current_ai_unavailable(str(error))
        except Exception:
            logger.exception("On-demand Gemini analysis failed for idea %s", idea_id)
            text = format_current_ai_unavailable(
                "Сервис текущего анализа временно недоступен. Попробуйте позже."
            )
        else:
            if result.candidate is None or result.review is None:
                text = format_current_ai_unavailable(result.reason)
            else:
                text = format_current_ai_analysis(result.review, result.candidate)
        fresh_context = await context_service.idea_context(user.telegram_id, idea_id)
        await _edit_context(
            callback,
            text,
            idea_context_keyboard(
                fresh_context.idea,
                watched=fresh_context.watched,
                followed=fresh_context.followed,
            ),
        )

    @router.callback_query(F.data.regexp(r"^idea_(follow|unfollow):[0-9]+$"))
    async def idea_follow(callback: CallbackQuery) -> None:
        user = await _ensure_callback_user(callback, services)
        if callback.data is None or callback.message is None:
            await callback.answer()
            return
        action, raw_id = callback.data.split(":", maxsplit=1)
        try:
            context = await context_service.set_idea_follow(
                user.telegram_id,
                int(raw_id),
                enabled=action == "idea_follow",
            )
        except (
            ValueError,
            ContextAccessError,
            ContextObjectNotFound,
            ContextActionExpired,
        ) as error:
            await callback.answer(str(error), show_alert=True)
            return
        await _edit_markup(
            callback,
            idea_context_keyboard(
                context.idea,
                watched=context.watched,
                followed=context.followed,
            ),
        )
        await callback.answer("Сохранено")

    @router.message(F.text == "🌍 Рынок сейчас")
    @router.message(F.text == "🧠 Анализ рынка")
    async def market_analysis(message: Message) -> None:
        await _ensure_message_user(message, services)
        if services.market_overview is None:
            await message.answer("Сервис анализа рынка недоступен.")
            return
        await message.answer(
            format_market_overview(await services.market_overview.current()),
            reply_markup=market_context_keyboard(),
        )

    @router.callback_query(
        F.data.regexp(r"^market:(refresh|leaders|laggards|oversold|overbought|volume)$")
    )
    async def market_context(callback: CallbackQuery) -> None:
        await _ensure_callback_user(callback, services)
        if callback.message is None or callback.data is None or services.market_overview is None:
            await callback.answer("Сервис рынка недоступен", show_alert=True)
            return
        section = callback.data.partition(":")[2]
        overview = await services.market_overview.current()
        if section == "refresh":
            text = format_market_overview(overview)
        else:
            title, rows, suffix = {
                "leaders": ("📈 Лидеры рынка", overview.relative_strength_leaders, "% vs IMOEX"),
                "laggards": ("📉 Слабейшие", overview.relative_strength_laggards, "% vs IMOEX"),
                "oversold": ("🟢 Перепроданные", overview.oversold_tickers, " RSI"),
                "overbought": ("🔴 Перекупленные", overview.overbought_tickers, " RSI"),
                "volume": ("📦 Аномальный объём", overview.anomalous_volume, "×"),
            }[section]
            lines = [f"{title}\n"]
            lines.extend(
                f"• <b>{escape(ticker)}</b>: {value:+.1f}{suffix}" for ticker, value in rows
            )
            if not rows:
                lines.append("Подходящих инструментов сейчас нет.")
            text = "\n".join(lines)
        await _edit_context(callback, text, market_context_keyboard())
        await callback.answer()

    @router.callback_query(F.data.regexp(r"^instrument_view:[A-Z0-9._-]{1,24}$"))
    async def instrument_view(callback: CallbackQuery) -> None:
        user = await _ensure_callback_user(callback, services)
        if callback.data is None or callback.message is None:
            await callback.answer()
            return
        ticker = callback.data.partition(":")[2]
        try:
            context = await context_service.instrument_context(user.telegram_id, ticker)
        except (ContextAccessError, ContextObjectNotFound) as error:
            await callback.answer(str(error), show_alert=True)
            return
        await _edit_context(
            callback,
            _instrument_text(context),
            instrument_context_keyboard(
                ticker,
                watched=context.watched,
                idea_id=context.latest_idea.id if context.latest_idea else None,
            ),
        )
        await callback.answer()

    @router.callback_query(F.data.regexp(r"^instrument_refresh:[A-Z0-9._-]{1,24}$"))
    async def instrument_refresh(callback: CallbackQuery) -> None:
        user = await _ensure_callback_user(callback, services)
        if callback.data is None or callback.message is None:
            await callback.answer()
            return
        ticker = callback.data.partition(":")[2]
        try:
            await context_service.instrument_context(user.telegram_id, ticker)
            await services.ingestion.refresh_ticker(ticker, user.default_timeframe)
            generated = await services.signals.generate(
                ticker,
                user.default_timeframe,
                risk_per_trade_pct=user.risk_per_trade_pct,
            )
            context = await context_service.instrument_context(user.telegram_id, ticker)
        except (
            ContextAccessError,
            ContextObjectNotFound,
            UnknownTickerError,
            InsufficientDataError,
        ) as error:
            await callback.answer(str(error), show_alert=True)
            return
        except MoexApiError:
            await callback.answer("MOEX ISS временно недоступен", show_alert=True)
            return
        await _edit_context(
            callback,
            format_signal(generated),
            instrument_analysis_keyboard(ticker, watched=context.watched),
        )
        await callback.answer("Обновлено")

    @router.callback_query(
        F.data.regexp(r"^instrument_(idea|ai|tech|fund|market|noidea):[A-Z0-9._-]{1,24}$")
    )
    async def instrument_section(callback: CallbackQuery) -> None:
        user = await _ensure_callback_user(callback, services)
        if callback.data is None or callback.message is None:
            await callback.answer()
            return
        action, ticker = callback.data.split(":", maxsplit=1)
        try:
            context = await context_service.instrument_context(user.telegram_id, ticker)
        except (ContextAccessError, ContextObjectNotFound) as error:
            await callback.answer(str(error), show_alert=True)
            return
        if context.latest_idea is None or services.operations is None:
            await callback.answer("Для бумаги сейчас нет TradingIdea", show_alert=True)
            return
        history = await services.operations.idea_history(context.latest_idea.id)
        if history is None:
            await callback.answer("Идея больше не существует", show_alert=True)
            return
        formatters = {
            "instrument_idea": lambda: format_new_idea(
                history.idea, timezone=services.settings.scheduler_timezone
            ),
            "instrument_noidea": lambda: "",
            "instrument_ai": lambda: format_ai_analysis(history),
            "instrument_tech": lambda: format_technical_analysis(history),
            "instrument_fund": lambda: format_fundamental_analysis(history),
            "instrument_market": lambda: format_idea_market_analysis(history),
        }
        await _edit_context(
            callback,
            formatters[action](),
            instrument_context_keyboard(
                ticker,
                watched=context.watched,
                idea_id=history.idea.id,
            ),
        )
        await callback.answer()

    @router.callback_query(F.data.regexp(r"^instrument_history:[A-Z0-9._-]{1,24}:[0-9]+$"))
    async def instrument_history(callback: CallbackQuery) -> None:
        user = await _ensure_callback_user(callback, services)
        if callback.data is None or callback.message is None:
            await callback.answer()
            return
        _, ticker, raw_page = callback.data.split(":")
        try:
            context = await context_service.instrument_context(user.telegram_id, ticker)
            page = await context_service.signal_history_page(
                user.telegram_id, ticker, int(raw_page)
            )
        except (ContextAccessError, ContextObjectNotFound, ValueError) as error:
            await callback.answer(str(error), show_alert=True)
            return
        await _edit_context(
            callback,
            _signal_history_text(ticker, page),
            signal_history_keyboard(
                ticker,
                watched=context.watched,
                page=page.page,
                total_pages=page.total_pages,
            ),
        )
        await callback.answer()

    @router.callback_query(
        F.data.regexp(r"^instrument_(watch|unwatch):[A-Z0-9._-]{1,24}(:[0-9]+)?$")
    )
    async def instrument_watch(callback: CallbackQuery) -> None:
        user = await _ensure_callback_user(callback, services)
        if callback.data is None or callback.message is None:
            await callback.answer()
            return
        parts = callback.data.split(":")
        action, ticker = parts[0], parts[1]
        try:
            await context_service.set_watch(
                user.telegram_id,
                ticker,
                enabled=action == "instrument_watch",
            )
            if len(parts) == 3:
                idea_context = await context_service.idea_context(user.telegram_id, int(parts[2]))
                keyboard = idea_context_keyboard(
                    idea_context.idea,
                    watched=idea_context.watched,
                    followed=idea_context.followed,
                )
            else:
                context = await context_service.instrument_context(user.telegram_id, ticker)
                keyboard = instrument_context_keyboard(
                    ticker,
                    watched=context.watched,
                    idea_id=context.latest_idea.id if context.latest_idea else None,
                )
        except (ContextAccessError, ContextObjectNotFound, ValueError) as error:
            await callback.answer(str(error), show_alert=True)
            return
        await _edit_markup(callback, keyboard)
        await callback.answer("Сохранено")

    @router.message(F.text == "🔎 Проверить акцию")
    async def request_ticker_check(message: Message) -> None:
        await _ensure_message_user(message, services)
        await message.answer(
            "🔎 <b>Проверить акцию</b>\n\nВведите тикер MOEX, например <code>SBER</code>.",
            reply_markup=ForceReply(
                selective=True,
                input_field_placeholder="SBER",
            ),
        )

    async def send_ticker_check(message: Message, secid: str, timeframe: str) -> None:
        user = await _ensure_message_user(message, services)
        try:
            secid = normalize_ticker(secid)
        except ContextObjectNotFound as error:
            await message.answer(str(error))
            return
        if timeframe not in ALLOWED_TIMEFRAMES:
            await message.answer("Интервал: 5m, 15m, 1h, 4h, 1d или 1w")
            return
        status = await message.answer(f"Обновляю {escape(secid)} · {timeframe}…")
        try:
            await services.ingestion.refresh_ticker(secid, timeframe)
            generated = await services.signals.generate(
                secid,
                timeframe,
                risk_per_trade_pct=user.risk_per_trade_pct,
            )
            context = await context_service.instrument_context(user.telegram_id, secid)
        except (ContextObjectNotFound, UnknownTickerError, InsufficientDataError) as error:
            await status.edit_text(escape(str(error)))
        except MoexApiError:
            await status.edit_text("MOEX ISS временно недоступен. Попробуйте чуть позже.")
        else:
            await status.edit_text(
                format_signal(generated),
                reply_markup=instrument_analysis_keyboard(secid, watched=context.watched),
            )

    @router.message(Command("signal"))
    async def signal(message: Message) -> None:
        tokens = _tokens(message)
        if len(tokens) < 2:
            await message.answer("Формат: <code>/signal SBER [15m]</code>")
            return
        secid = tokens[1].upper()
        user = await _ensure_message_user(message, services)
        timeframe = tokens[2].lower() if len(tokens) > 2 else user.default_timeframe
        await send_ticker_check(message, secid, timeframe)

    @router.message(Command("watchlist"))
    @router.message(F.text == "👁 Отслеживаемые")
    async def watchlist(message: Message) -> None:
        user = await _ensure_message_user(message, services)
        tokens = _tokens(message)
        if message.text == "👁 Отслеживаемые":
            tokens = ["/watchlist"]
        if len(tokens) == 1:
            page = await context_service.watchlist_page(user.telegram_id, 0)
            tickers = tuple(item.secid for item in page.items)
            text = "\n".join(f"• <b>{item.secid}</b> — {item.short_name}" for item in page.items)
            await message.answer(
                f"👁 <b>Watchlist</b>\n\n{text or 'Список пока пуст'}",
                reply_markup=watchlist_keyboard(
                    tickers,
                    page=page.page,
                    total_pages=page.total_pages,
                ),
            )
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

    @router.callback_query(F.data.regexp(r"^watchlist:[0-9]+$"))
    async def watchlist_page(callback: CallbackQuery) -> None:
        user = await _ensure_callback_user(callback, services)
        if callback.data is None or callback.message is None:
            await callback.answer()
            return
        page = await context_service.watchlist_page(
            user.telegram_id, int(callback.data.partition(":")[2])
        )
        tickers = tuple(item.secid for item in page.items)
        text = "\n".join(f"• <b>{item.secid}</b> — {item.short_name}" for item in page.items)
        await _edit_context(
            callback,
            f"👁 <b>Watchlist</b>\n\n{text or 'Список пока пуст'}",
            watchlist_keyboard(tickers, page=page.page, total_pages=page.total_pages),
        )
        await callback.answer()

    @router.message(Command("settings"))
    @router.message(F.text == "⚙️ Настройки")
    async def settings(message: Message) -> None:
        user = await _ensure_message_user(message, services)
        tokens = _tokens(message)
        if message.text == "⚙️ Настройки":
            tokens = ["/settings"]
        if len(tokens) == 1:
            await message.answer(
                settings_text(user, services), reply_markup=settings_menu_keyboard(user)
            )
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

    @router.message(F.text == "📒 Результаты сигналов")
    async def results_from_menu(message: Message) -> None:
        await _ensure_message_user(message, services)
        await message.answer(
            "📒 <b>Результаты сигналов</b>\nВыберите раздел:",
            reply_markup=results_keyboard(),
        )

    @router.callback_query(F.data == "results:menu")
    async def results_menu(callback: CallbackQuery) -> None:
        await _ensure_callback_user(callback, services)
        if callback.message is not None:
            await _edit_context(
                callback,
                "📒 <b>Результаты сигналов</b>\nВыберите раздел:",
                results_keyboard(),
            )
        await callback.answer()

    @router.callback_query(
        F.data.regexp(r"^results:(win|loss|active|expired|missed|airej):[0-9]+$")
    )
    async def results_page(callback: CallbackQuery) -> None:
        user = await _ensure_callback_user(callback, services)
        if callback.data is None or callback.message is None:
            await callback.answer()
            return
        _, result_filter, raw_page = callback.data.split(":")
        try:
            page = await context_service.results_page(
                user.telegram_id,
                result_filter,
                int(raw_page),
            )
        except (ContextAccessError, ContextObjectNotFound, ValueError) as error:
            await callback.answer(str(error), show_alert=True)
            return
        await _edit_context(
            callback,
            _results_text(result_filter, page),
            results_keyboard(
                result_filter,
                page=page.page,
                total_pages=page.total_pages,
            ),
        )
        await callback.answer()

    @router.callback_query(F.data.regexp(r"^status:(refresh|moex|gemini|scheduler|database|scan)$"))
    async def status_section(callback: CallbackQuery) -> None:
        await _ensure_callback_user(callback, services)
        if callback.message is None or callback.data is None or services.operations is None:
            await callback.answer("Диагностика недоступна", show_alert=True)
            return
        section = callback.data.partition(":")[2]
        status = await services.operations.status()
        if section == "refresh":
            text = format_application_status(
                status,
                timezone=services.settings.scheduler_timezone,
            )
        elif section == "moex":
            freshness = status.freshness
            examples = ", ".join(
                f"{item.ticker}/{item.timeframe}" for item in freshness.stale_examples[:10]
            )
            text = (
                "📡 <b>MOEX DATA</b>\n\n"
                f"Fresh: <b>{freshness.fresh}/{freshness.checked}</b>\n"
                f"Latest update: <b>{freshness.latest_moex_update or 'нет'}</b>\n"
                f"Stale: <b>{escape(examples or 'нет')}</b>"
            )
        elif section == "gemini":
            api_key_configured = bool(
                services.settings.gemini_api_key
                if services.settings.ai_provider == "gemini"
                else services.settings.openai_api_key
            )
            text = (
                "🧠 <b>AI PROVIDER</b>\n\n"
                f"Provider: <b>{escape(services.settings.ai_provider)}</b>\n"
                f"Primary: <b>{escape(services.settings.ai_model)}</b>\n"
                f"Fallback: <b>{escape(services.settings.ai_fallback_model)}</b>\n"
                "API key: <b>"
                f"{'configured' if api_key_configured else 'missing'}</b>\n"
                "Значение ключа никогда не показывается."
            )
        elif section == "scheduler":
            jobs = "\n".join(
                f"• {escape(item.job_name)}: "
                f"{'OK' if item.success else 'ERROR' if item.success is False else 'WAIT'}"
                for item in status.job_states
            )
            text = (
                f"⏱ <b>Scheduler: {'RUNNING' if status.scheduler_running else 'STOPPED'}</b>\n\n"
                f"{jobs or 'Запуски ещё не записаны.'}"
            )
        elif section == "database":
            text = (
                "📊 <b>DATABASE</b>\n\n"
                f"Connection + migration head: <b>{'OK' if status.database_ok else 'ERROR'}</b>\n"
                "DSN и credentials не выводятся."
            )
        else:
            scan = next(
                (item for item in status.job_states if item.job_name == "idea_scanning"), None
            )
            text = (
                "📈 <b>ПОСЛЕДНИЙ SCAN</b>\n\n"
                f"Finished: <b>{status.latest_scan_time or 'нет'}</b>\n"
                f"Next: <b>{status.next_scan_time or 'нет'}</b>\n"
                f"Result: <b>{'OK' if scan and scan.success else 'ERROR/WAIT'}</b>\n"
                f"Details: <code>{escape((scan.details if scan else '')[:1000])}</code>"
            )
        await _edit_context(callback, text, status_context_keyboard())
        await callback.answer()

    @router.callback_query(F.data == "settings:menu")
    async def settings_menu(callback: CallbackQuery) -> None:
        user = await _ensure_callback_user(callback, services)
        if callback.message is not None:
            await _edit_context(
                callback,
                settings_text(user, services),
                settings_menu_keyboard(user),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("settings:"))
    async def settings_section(callback: CallbackQuery) -> None:
        user = await _ensure_callback_user(callback, services)
        if callback.message is None or callback.data is None:
            await callback.answer()
            return
        section = callback.data.partition(":")[2]
        titles = {
            "frequency": "Отчёты",
            "horizon": "Горизонт",
            "strength": "Минимальная сила",
            "risk": "Риск",
            "notifications": "Уведомления",
            "ai": "AI filter",
        }
        if section not in titles:
            await callback.answer("Некорректный раздел", show_alert=True)
            return
        await _edit_context(
            callback,
            f"⚙️ <b>{titles[section]}</b>",
            reply_markup=settings_values_keyboard(section, user),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("set_frequency:"))
    async def set_frequency(callback: CallbackQuery) -> None:
        await _ensure_callback_user(callback, services)
        value = (callback.data or "").partition(":")[2]
        if value not in {"strong", "hourly", "3h", "daily", "off"}:
            await callback.answer("Некорректное значение", show_alert=True)
            return
        async with services.session_factory() as session, session.begin():
            await update_user_settings(session, callback.from_user.id, report_frequency=value)
        user = await _ensure_callback_user(callback, services)
        if callback.message is not None:
            await _edit_context(
                callback,
                "Частота сохранена.",
                settings_values_keyboard("frequency", user),
            )
        await callback.answer("Сохранено")

    @router.callback_query(F.data.startswith("set_horizon:"))
    async def set_horizon(callback: CallbackQuery) -> None:
        await _ensure_callback_user(callback, services)
        value = (callback.data or "").partition(":")[2]
        if value not in {"INTRADAY_1D", "SWING_5D", "POSITION_1M", "all"}:
            await callback.answer("Некорректное значение", show_alert=True)
            return
        async with services.session_factory() as session, session.begin():
            await update_user_settings(session, callback.from_user.id, idea_horizon=value)
        user = await _ensure_callback_user(callback, services)
        if callback.message is not None:
            await _edit_context(
                callback,
                "Срок идей сохранён.",
                settings_values_keyboard("horizon", user),
            )
        await callback.answer("Сохранено")

    @router.callback_query(F.data.startswith("set_strength:"))
    async def set_strength(callback: CallbackQuery) -> None:
        await _ensure_callback_user(callback, services)
        try:
            value = float((callback.data or "").partition(":")[2])
        except ValueError:
            await callback.answer("Некорректное значение", show_alert=True)
            return
        if value not in {70, 75, 80, 85, 90}:
            await callback.answer("Некорректное значение", show_alert=True)
            return
        async with services.session_factory() as session, session.begin():
            await update_user_settings(session, callback.from_user.id, minimum_confidence=value)
        user = await _ensure_callback_user(callback, services)
        if callback.message is not None:
            await _edit_context(
                callback,
                "Минимальная сила сохранена.",
                settings_values_keyboard("strength", user),
            )
        await callback.answer("Сохранено")

    @router.callback_query(F.data.startswith("set_risk:"))
    async def set_risk(callback: CallbackQuery) -> None:
        await _ensure_callback_user(callback, services)
        try:
            value = float((callback.data or "").partition(":")[2])
        except ValueError:
            await callback.answer("Некорректное значение", show_alert=True)
            return
        if value not in {0.5, 1.0, 2.0}:
            await callback.answer("Некорректное значение", show_alert=True)
            return
        async with services.session_factory() as session, session.begin():
            await update_user_settings(session, callback.from_user.id, risk_pct=value)
        user = await _ensure_callback_user(callback, services)
        if callback.message is not None:
            await _edit_context(
                callback,
                "Риск сохранён.",
                settings_values_keyboard("risk", user),
            )
        await callback.answer("Сохранено")

    @router.callback_query(F.data.startswith("set_ai:"))
    async def set_ai_filter(callback: CallbackQuery) -> None:
        await _ensure_callback_user(callback, services)
        value = (callback.data or "").partition(":")[2]
        if value not in {"0", "1"}:
            await callback.answer("Некорректное значение", show_alert=True)
            return
        enabled = value == "1"
        async with services.session_factory() as session, session.begin():
            await update_user_settings(session, callback.from_user.id, ai_filter_enabled=enabled)
        user = await _ensure_callback_user(callback, services)
        note = (
            "AI second opinion включён."
            if enabled
            else "AI-фильтр ленты выключен. Идеи без review будут явно помечены."
        )
        if callback.message is not None:
            await _edit_context(callback, note, settings_values_keyboard("ai", user))
        await callback.answer("Сохранено")

    @router.callback_query(F.data.startswith("toggle_notify:"))
    async def toggle_notification(callback: CallbackQuery) -> None:
        await _ensure_callback_user(callback, services)
        key = (callback.data or "").partition(":")[2]
        columns = {
            "new": "notify_new_idea",
            "activation": "notify_activation",
            "tp": "notify_tp",
            "sl": "notify_sl",
            "expiry": "notify_expiry",
            "daily": "notify_daily_summary",
            "watch": "notify_watchlist",
        }
        column = columns.get(key)
        if column is None:
            await callback.answer("Некорректная настройка", show_alert=True)
            return
        user = await _ensure_callback_user(callback, services)
        values = {column: not bool(getattr(user, column))}
        async with services.session_factory() as session, session.begin():
            await update_user_settings(session, callback.from_user.id, **values)
        user = await _ensure_callback_user(callback, services)
        if callback.message is not None:
            await _edit_context(
                callback,
                "Уведомления обновлены.",
                settings_values_keyboard("notifications", user),
            )
        await callback.answer("Сохранено")

    @router.message(Command("portfolio"))
    async def portfolio(message: Message) -> None:
        if services.paper is None:
            await message.answer("Paper trading сейчас недоступен.")
            return
        await message.answer(format_paper_summary(await services.paper.summary()))

    @router.message(F.reply_to_message.text.startswith("🔎 Проверить акцию"))
    async def ticker_check_reply(message: Message) -> None:
        user = await _ensure_message_user(message, services)
        tokens = _tokens(message)
        if len(tokens) != 1:
            await message.answer("Введите один тикер, например <code>SBER</code>.")
            return
        await send_ticker_check(message, tokens[0].upper(), user.default_timeframe)

    return router
