from __future__ import annotations

from datetime import UTC, datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain import AnalysisMode, IdeaDirection, IdeaHorizon, IdeaStatus, ReportFrequency
from app.idea_repository import (
    get_idea,
    list_open_ideas,
    list_unnotified_ideas,
    mark_ideas_notified,
)
from app.models import TelegramUser, TradingIdea
from app.repositories import list_report_users, mark_report_sent
from app.telegram_ui import top_ideas_keyboard

HORIZON_LABELS = {
    IdeaHorizon.INTRADAY_1D.value: "1 день",
    IdeaHorizon.SWING_5D.value: "5 дней",
    IdeaHorizon.POSITION_1M.value: "1 месяц",
}
STATUS_LABELS = {
    IdeaStatus.PENDING_ENTRY.value: "ожидает входа",
    IdeaStatus.ACTIVE.value: "активна",
    IdeaStatus.TP_HIT.value: "достигнут TP",
    IdeaStatus.SL_HIT.value: "достигнут SL",
    IdeaStatus.EXPIRED.value: "срок истёк",
    IdeaStatus.CANCELLED.value: "отменена",
    IdeaStatus.INVALIDATED.value: "вход упущен",
}


def _aware_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def report_is_due(user: TelegramUser, now: datetime) -> bool:
    frequency = ReportFrequency(user.report_frequency)
    if frequency == ReportFrequency.OFF:
        return False
    if frequency == ReportFrequency.STRONG_ONLY:
        return True
    if user.last_report_at is None:
        return True
    elapsed = _aware_utc(now) - _aware_utc(user.last_report_at)
    thresholds = {
        ReportFrequency.HOURLY: timedelta(hours=1),
        ReportFrequency.THREE_HOURS: timedelta(hours=3),
        ReportFrequency.DAILY: timedelta(days=1),
    }
    return elapsed >= thresholds[frequency]


def format_trading_idea(idea: TradingIdea, *, timezone: str = "Europe/Moscow") -> str:
    direction = IdeaDirection(idea.direction)
    icon = "📈" if direction == IdeaDirection.BUY else "📉"
    action = "ПОКУПКА" if direction == IdeaDirection.BUY else "ПРОДАЖА"
    zone_label = "покупки" if direction == IdeaDirection.BUY else "продажи"
    formed = _aware_utc(idea.created_at).astimezone(ZoneInfo(timezone))
    regime_icon = {"BULL": "🟢", "BEAR": "🔴", "SIDEWAYS": "🟡"}.get(idea.market_regime or "", "⚪")
    return (
        f"🔵 <b>Классический</b>\n{icon} <b>{action} — {escape(idea.instrument_name)}</b>\n\n"
        f"<b>{escape(idea.ticker)}</b>\n\n"
        f"💰 Текущая цена: <b>{idea.current_price:.2f} ₽</b>\n"
        f"🎯 Зона {zone_label}: <b>{idea.entry_price_from:.2f}–{idea.entry_price_to:.2f} ₽</b>\n"
        f"✅ Take Profit: <b>{idea.take_profit:.2f} ₽</b>\n"
        f"🛑 Stop Loss: <b>{idea.stop_loss:.2f} ₽</b>\n\n"
        f"⏱ Срок идеи: <b>{HORIZON_LABELS[idea.horizon]}</b>\n"
        f"Статус: <b>{STATUS_LABELS[idea.status]}</b>\n\n"
        f"Потенциал: <b>+{idea.expected_return_pct:.1f}%</b>\n"
        f"Риск: <b>−{idea.risk_pct:.1f}%</b>\n"
        f"Risk/Reward: <b>1:{idea.risk_reward_ratio:.1f}</b>\n\n"
        f"🔥 Уверенность: <b>{idea.confidence:.0f}%</b>\n\n"
        f"{regime_icon} IMOEX: <b>{idea.market_regime or 'нет данных'}</b> · "
        f"vol {idea.market_volatility or 'n/a'}\n"
        f"Относительная сила: <b>{escape(idea.relative_strength_label or 'недоступно')}</b>\n"
        f"Объём: <b>{idea.volume_state or 'UNKNOWN'}</b> "
        f"({float(idea.volume_score or 0):+.0f}/100)\n"
        f"Фундаментал: <b>{escape(idea.fundamental_label or 'нет данных')}</b>\n\n"
        f"⚠️ {escape(idea.invalidation_reason)}\n\n"
        f"Сформировано: {formed:%d.%m.%Y %H:%M} МСК\n\n"
        "Не является индивидуальной инвестиционной рекомендацией."
    )


def format_idea_details(idea: TradingIdea) -> str:
    reasons = [line for line in idea.rationale.splitlines() if line]
    explanation = "\n".join(f"• {escape(line)}" for line in reasons[:8])
    return f"{format_trading_idea(idea)}\n\n<b>Почему:</b>\n{explanation}"


def format_best_ideas(ideas: list[TradingIdea]) -> str:
    if not ideas:
        return "Сейчас идей, соответствующих выбранным критериям, нет."
    sections = ["🔥 <b>Лучшие идеи сейчас</b>"]
    for horizon in IdeaHorizon:
        selected = [idea for idea in ideas if idea.horizon == horizon.value][:5]
        if not selected:
            continue
        sections.append(f"\n<b>{HORIZON_LABELS[horizon.value]}</b>")
        for index, idea in enumerate(selected, start=1):
            action = "BUY" if idea.direction == IdeaDirection.BUY.value else "SELL"
            strength = idea.final_quality_score or idea.confidence
            ai = f" · AI {idea.ai_score:.0f}/100" if idea.ai_score is not None else ""
            sections.append(
                f"{index}. <b>{action} {escape(idea.ticker)}</b> — {strength:.0f}/100{ai} · "
                f"{STATUS_LABELS[idea.status]} · {idea.observation_mode}"
            )
    return "\n".join(sections)


class ReportingService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        timezone: str = "Europe/Moscow",
        strategy_version: str | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.timezone = timezone
        self.strategy_version = strategy_version

    async def best_for_user(
        self,
        user: TelegramUser,
        *,
        limit: int = 15,
        horizon: str | None = None,
        created_after: datetime | None = None,
    ) -> list[TradingIdea]:
        if not AnalysisMode(user.analysis_mode).includes_legacy:
            return []
        async with self.session_factory() as session:
            return await list_open_ideas(
                session,
                horizon=horizon or user.idea_horizon,
                minimum_confidence=user.minimum_confidence,
                limit=limit,
                strategy_version=self.strategy_version,
                created_after=created_after,
                ai_approved_only=user.ai_filter_enabled and self.strategy_version is not None,
            )

    async def idea_details(self, idea_id: int) -> TradingIdea | None:
        async with self.session_factory() as session:
            return await get_idea(session, idea_id)

    async def dispatch_due(self, bot: Bot, *, now: datetime | None = None) -> dict[str, int]:
        sent_at = now or datetime.now(UTC)
        async with self.session_factory() as session:
            users = await list_report_users(session)
        counters = {"users_checked": len(users), "reports_sent": 0, "ideas_sent": 0}
        for user in users:
            if not AnalysisMode(user.analysis_mode).includes_legacy:
                continue
            if not report_is_due(user, sent_at):
                continue
            async with self.session_factory() as session:
                ideas = await list_unnotified_ideas(
                    session,
                    telegram_id=user.telegram_id,
                    horizon=user.idea_horizon,
                    minimum_confidence=user.minimum_confidence,
                    strategy_version=self.strategy_version,
                    ai_approved_only=(user.ai_filter_enabled and self.strategy_version is not None),
                )
            try:
                if ideas:
                    try:
                        await bot.send_message(
                            user.telegram_id,
                            format_best_ideas(ideas),
                            reply_markup=top_ideas_keyboard(ideas),
                        )
                    except TypeError as error:
                        if "reply_markup" not in str(error):
                            raise
                        await bot.send_message(user.telegram_id, format_best_ideas(ideas))
                async with self.session_factory() as session, session.begin():
                    if ideas:
                        await mark_ideas_notified(
                            session,
                            user.telegram_id,
                            ideas,
                            sent_at=sent_at,
                        )
                    await mark_report_sent(session, user.telegram_id, sent_at)
                if ideas:
                    counters["reports_sent"] += 1
                    counters["ideas_sent"] += len(ideas)
            except TelegramForbiddenError:
                async with self.session_factory() as session, session.begin():
                    await session.execute(
                        update(TelegramUser)
                        .where(TelegramUser.telegram_id == user.telegram_id)
                        .values(is_active=False)
                    )
        return counters
