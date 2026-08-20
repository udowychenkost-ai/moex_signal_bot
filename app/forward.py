from __future__ import annotations

import logging
from datetime import UTC, datetime
from html import escape
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.domain import IdeaHorizon, IdeaStatus
from app.idea_repository import OPEN_IDEA_STATUSES
from app.models import (
    ForwardNotification,
    PaperTrade,
    TelegramUser,
    TradingIdea,
    TradingIdeaEvent,
)
from app.observation import aware_utc
from app.operations import (
    ApplicationStatus,
    IdeaHistory,
    OperationalService,
    PeriodStatistics,
    realized_r,
)
from app.reporting import HORIZON_LABELS

logger = logging.getLogger(__name__)

NOTIFIABLE_EVENT_TYPES = {
    "CREATED",
    "ACTIVATED",
    "TP_HIT",
    "SL_HIT",
    "EXPIRED",
    "ENTRY_MISSED",
    "DIRECTION_REVERSED",
    "CANCELLED",
    "INVALIDATED",
}

EVENT_TITLES = {
    IdeaStatus.ACTIVE.value: "IDEA ACTIVATED",
    IdeaStatus.TP_HIT.value: "TP HIT",
    IdeaStatus.SL_HIT.value: "SL HIT",
    IdeaStatus.EXPIRED.value: "EXPIRED",
    IdeaStatus.INVALIDATED.value: "INVALIDATED",
    IdeaStatus.CANCELLED.value: "CANCELLED",
}


def _format_time(value: datetime | None, timezone: str) -> str:
    if value is None:
        return "нет"
    return aware_utc(value).astimezone(ZoneInfo(timezone)).strftime("%d.%m.%Y %H:%M:%S %Z")


def format_new_idea(idea: TradingIdea, *, timezone: str) -> str:
    rationale = "\n".join(f"• {escape(line)}" for line in idea.rationale.splitlines()[:5] if line)
    return (
        "🆕 <b>НОВАЯ ИДЕЯ</b>\n\n"
        f"ID: <code>{idea.id}</code>\n"
        f"Direction: <b>{idea.direction}</b>\n"
        f"Ticker: <b>{escape(idea.ticker)}</b>\n"
        f"Инструмент: <b>{escape(idea.instrument_name)}</b>\n"
        f"Текущая цена: <b>{idea.current_price:.2f} ₽</b>\n"
        f"Entry zone: <b>{idea.entry_price_from:.2f}–{idea.entry_price_to:.2f} ₽</b>\n"
        f"TP: <b>{idea.take_profit:.2f} ₽</b>\n"
        f"SL: <b>{idea.stop_loss:.2f} ₽</b>\n"
        f"Horizon: <b>{HORIZON_LABELS[idea.horizon]}</b>\n"
        f"Режим: <b>{idea.observation_mode}</b>\n"
        f"Signal strength: <b>{idea.confidence:.0f}%</b>\n"
        f"R:R: <b>1:{idea.risk_reward_ratio:.2f}</b>\n"
        f"Potential return: <b>+{idea.expected_return_pct:.2f}%</b>\n"
        f"Potential risk: <b>−{idea.risk_pct:.2f}%</b>\n"
        f"Статус: <b>{idea.status}</b>\n\n"
        f"<b>Rationale</b>\n{rationale}\n\n"
        f"Created at: {_format_time(idea.created_at, timezone)}\n\n"
        "⚠️ Только наблюдение. Реальные сделки не выполняются."
    )


def _closed_result(idea: TradingIdea, paper: PaperTrade | None) -> str:
    if idea.status not in {
        IdeaStatus.TP_HIT.value,
        IdeaStatus.SL_HIT.value,
        IdeaStatus.EXPIRED.value,
        IdeaStatus.INVALIDATED.value,
        IdeaStatus.CANCELLED.value,
    }:
        return ""
    if paper is not None and paper.status == "CLOSED":
        return (
            f"\nФактический PAPER результат: <b>{paper.net_pnl:+,.2f} ₽</b>"
            f" · <b>{paper.r_multiple:+.2f}R</b>"
        )
    value = realized_r(idea)
    if value is not None:
        return f"\nФактический RESEARCH результат: <b>{value:+.2f}R</b> (без paper P&L)"
    return "\nФактический результат: <b>вход не был активирован</b>"


def format_lifecycle_event(
    idea: TradingIdea,
    event: TradingIdeaEvent,
    paper: PaperTrade | None,
    *,
    timezone: str,
) -> str:
    title = EVENT_TITLES.get(event.to_status, event.event_type)
    price = f"\nЦена события: <b>{event.price:.2f} ₽</b>" if event.price is not None else ""
    return (
        f"🔔 <b>{title}</b>\n\n"
        f"ID: <code>{idea.id}</code>\n"
        f"Ticker: <b>{escape(idea.ticker)}</b>\n"
        f"Horizon: <b>{HORIZON_LABELS[idea.horizon]}</b>\n"
        f"Текущий статус: <b>{event.to_status}</b>{price}\n"
        f"Причина: {escape(event.details or event.event_type)}"
        f"{_closed_result(idea, paper)}\n"
        f"Время: {_format_time(event.occurred_at, timezone)}\n\n"
        "⚠️ Только наблюдение. Торговое поручение не создавалось."
    )


def format_application_status(status: ApplicationStatus, *, timezone: str) -> str:
    freshness = status.freshness
    stale_note = ""
    if freshness.stale_examples:
        examples = ", ".join(
            f"{item.ticker}/{item.timeframe}" for item in freshness.stale_examples[:5]
        )
        stale_note = f"\nУстаревшие: <b>{escape(examples)}</b>"
    jobs = []
    for state in status.job_states:
        marker = "✅" if state.success else ("❌" if state.success is False else "⏳")
        jobs.append(f"{marker} {escape(state.job_name)}")
    jobs_text = ", ".join(jobs) if jobs else "ещё не запускались"
    return (
        "🩺 <b>LIVE OBSERVATION STATUS</b>\n\n"
        f"App version: <b>{escape(status.app_version)}</b>\n"
        f"Git commit: <code>{escape(status.git_commit)}</code>\n"
        f"Database: <b>{'OK' if status.database_ok else 'ERROR'}</b>\n"
        f"Latest MOEX update: <b>{_format_time(freshness.latest_moex_update, timezone)}</b>\n"
        f"Data freshness: <b>{freshness.fresh}/{freshness.checked} fresh</b>"
        f"{stale_note}\n"
        f"Latest scan: <b>{_format_time(status.latest_scan_time, timezone)}</b>\n"
        f"Next scan: <b>{_format_time(status.next_scan_time, timezone)}</b>\n"
        f"Active ideas: <b>{status.active_ideas}</b>\n"
        f"Pending ideas: <b>{status.pending_ideas}</b>\n"
        f"Ideas closed today: <b>{status.ideas_closed_today}</b>\n"
        f"Scheduler: <b>{'RUNNING' if status.scheduler_running else 'STOPPED'}</b>\n"
        f"Jobs: {jobs_text}"
    )


def _metric(value: float | None, *, suffix: str = "") -> str:
    if value is None:
        return "n/a"
    if value == float("inf"):
        return "∞"
    return f"{value:.2f}{suffix}"


def format_statistics(periods: tuple[PeriodStatistics, ...]) -> str:
    sections = ["📊 <b>FORWARD STATISTICS</b>"]
    for period in periods:
        sections.append(f"\n<b>{period.label}</b>")
        for row in period.horizons:
            sample = " ⚠️ малая выборка" if row.small_sample else ""
            paper_pnl = (
                f"{row.net_paper_pnl:+,.2f} ₽"
                if row.net_paper_pnl is not None
                else "n/a (RESEARCH)"
            )
            sections.append(
                f"\n<b>{HORIZON_LABELS[row.horizon]} · {row.mode}</b>{sample}\n"
                f"Generated: {row.generated} · Activated: {row.activated}\n"
                f"TP: {row.tp} · SL: {row.sl} · Expired: {row.expired}\n"
                f"Win rate: {_metric(row.win_rate, suffix='%')} · "
                f"PF: {_metric(row.profit_factor)}\n"
                f"Average R: {_metric(row.average_r)} · Net paper P&L: {paper_pnl}"
            )
    sections.append(
        "\n<i>Win rate = TP/(TP+SL). RESEARCH R не включает комиссии; "
        "PAPER P&L включает настроенные комиссии и проскальзывание.</i>"
    )
    return "\n".join(sections)


def format_open_ideas(ideas: list[TradingIdea]) -> str:
    if not ideas:
        return "Ожидающих и активных идей сейчас нет."
    lines = ["📋 <b>Текущие идеи</b>"]
    for idea in ideas:
        lines.append(
            f"<code>{idea.id}</code> · <b>{idea.direction} {escape(idea.ticker)}</b> · "
            f"{HORIZON_LABELS[idea.horizon]} · {idea.status} · {idea.confidence:.0f}%"
        )
    lines.append("\nПолная история: <code>/idea ID</code>")
    return "\n".join(lines)


def format_idea_history(history: IdeaHistory, *, timezone: str) -> str:
    idea = history.idea
    lines = [format_new_idea(idea, timezone=timezone), "\n<b>Lifecycle</b>"]
    for event in history.events:
        price = f" @ {event.price:.2f}" if event.price is not None else ""
        lines.append(
            f"• {_format_time(event.occurred_at, timezone)} · "
            f"{escape(event.event_type)} → <b>{event.to_status}</b>{price}"
        )
    lines.append(_closed_result(idea, history.paper_trade))
    if history.snapshot is not None:
        lines.append(
            "\n<b>Decision snapshot</b>\n"
            f"Technical: {history.snapshot.technical_score:+.2f} · "
            f"Total: {history.snapshot.total_score:+.2f} · "
            f"ATR: {_metric(history.snapshot.atr)}\n"
            "Snapshot immutable: да"
        )
    return "\n".join(line for line in lines if line)


class ForwardReportingService:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        operations: OperationalService,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.operations = operations
        self.started_at = datetime.now(UTC)

    async def _recipients(self) -> dict[int, TelegramUser | None]:
        async with self.session_factory() as session:
            users = list(
                await session.scalars(select(TelegramUser).where(TelegramUser.is_active.is_(True)))
            )
        recipients: dict[int, TelegramUser | None] = {user.telegram_id: user for user in users}
        for chat_id in self.settings.admin_chat_ids:
            recipients.setdefault(chat_id, None)
        return recipients

    async def _mark_sent(
        self,
        telegram_id: int,
        key: str,
        kind: str,
        *,
        idea_id: int | None = None,
        event_id: int | None = None,
        sent_at: datetime,
    ) -> None:
        async with self.session_factory() as session, session.begin():
            session.add(
                ForwardNotification(
                    telegram_id=telegram_id,
                    notification_key=key,
                    notification_type=kind,
                    idea_id=idea_id,
                    event_id=event_id,
                    sent_at=sent_at,
                )
            )

    async def _deactivate_if_registered(self, telegram_id: int) -> None:
        async with self.session_factory() as session, session.begin():
            await session.execute(
                update(TelegramUser)
                .where(TelegramUser.telegram_id == telegram_id)
                .values(is_active=False)
            )

    async def dispatch_notifications(
        self,
        bot: Bot,
        *,
        now: datetime | None = None,
    ) -> dict[str, int]:
        sent_at = now or datetime.now(UTC)
        recipients = await self._recipients()
        async with self.session_factory() as session:
            events = list(
                await session.scalars(
                    select(TradingIdeaEvent)
                    .where(TradingIdeaEvent.event_type.in_(NOTIFIABLE_EVENT_TYPES))
                    .order_by(TradingIdeaEvent.occurred_at, TradingIdeaEvent.id)
                )
            )
            ideas = {
                idea.id: idea
                for idea in await session.scalars(
                    select(TradingIdea).where(
                        TradingIdea.id.in_({event.idea_id for event in events})
                    )
                )
            }
            papers = {
                trade.idea_id: trade
                for trade in await session.scalars(
                    select(PaperTrade).where(PaperTrade.idea_id.in_(ideas))
                )
            }
        counters = {"recipients": len(recipients), "sent": 0, "errors": 0}
        for telegram_id, user in recipients.items():
            notifications_off = user is not None and user.report_frequency == "off"
            if notifications_off and telegram_id not in self.settings.admin_chat_ids:
                continue
            async with self.session_factory() as session:
                sent_keys = set(
                    await session.scalars(
                        select(ForwardNotification.notification_key).where(
                            ForwardNotification.telegram_id == telegram_id
                        )
                    )
                )
            for event in events:
                key = f"event:{event.id}"
                if key in sent_keys:
                    continue
                recipient_cutoff = user.created_at if user is not None else self.started_at
                if aware_utc(event.occurred_at) < aware_utc(recipient_cutoff):
                    continue
                idea = ideas.get(event.idea_id)
                if idea is None:
                    continue
                is_admin = telegram_id in self.settings.admin_chat_ids
                if not is_admin and user is not None:
                    if user.idea_horizon != "all" and user.idea_horizon != idea.horizon:
                        continue
                    if idea.confidence < user.minimum_confidence:
                        continue
                message = (
                    format_new_idea(idea, timezone=self.settings.scheduler_timezone)
                    if event.event_type == "CREATED"
                    else format_lifecycle_event(
                        idea,
                        event,
                        papers.get(idea.id),
                        timezone=self.settings.scheduler_timezone,
                    )
                )
                try:
                    await bot.send_message(telegram_id, message)
                    await self._mark_sent(
                        telegram_id,
                        key,
                        event.event_type,
                        idea_id=idea.id,
                        event_id=event.id,
                        sent_at=sent_at,
                    )
                    sent_keys.add(key)
                    counters["sent"] += 1
                except TelegramForbiddenError:
                    await self._deactivate_if_registered(telegram_id)
                    counters["errors"] += 1
                    break
                except Exception:
                    counters["errors"] += 1
                    logger.exception(
                        "Forward notification failed for chat=%s event=%s",
                        telegram_id,
                        event.id,
                    )
        return counters

    async def dispatch_daily_summary(
        self,
        bot: Bot,
        *,
        now: datetime | None = None,
    ) -> dict[str, int]:
        sent_at = aware_utc(now or datetime.now(UTC))
        local = sent_at.astimezone(ZoneInfo(self.settings.scheduler_timezone))
        start = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
        async with self.session_factory() as session:
            ideas = list(
                await session.scalars(select(TradingIdea).where(TradingIdea.created_at >= start))
            )
            events = list(
                await session.scalars(
                    select(TradingIdeaEvent).where(TradingIdeaEvent.occurred_at >= start)
                )
            )
            active_now = len(
                list(
                    await session.scalars(
                        select(TradingIdea.id).where(TradingIdea.status.in_(OPEN_IDEA_STATUSES))
                    )
                )
            )
        by_id = {idea.id: idea for idea in ideas}
        all_today_ideas = {**by_id}
        if events:
            async with self.session_factory() as session:
                for idea in await session.scalars(
                    select(TradingIdea).where(
                        TradingIdea.id.in_({event.idea_id for event in events})
                    )
                ):
                    all_today_ideas[idea.id] = idea

        def transition_count(status: str, horizon: str | None = None) -> int:
            return sum(
                event.to_status == status
                and (horizon is None or all_today_ideas[event.idea_id].horizon == horizon)
                for event in events
                if event.idea_id in all_today_ideas
            )

        lines = [
            "📅 <b>FORWARD PAPER — DAILY REPORT</b>",
            "",
            f"New ideas: <b>{len(ideas)}</b>",
            f"Activated: <b>{transition_count(IdeaStatus.ACTIVE.value)}</b>",
            f"TP: <b>{transition_count(IdeaStatus.TP_HIT.value)}</b>",
            f"SL: <b>{transition_count(IdeaStatus.SL_HIT.value)}</b>",
            f"Expired: <b>{transition_count(IdeaStatus.EXPIRED.value)}</b>",
            "",
            f"Active now: <b>{active_now}</b>",
        ]
        for horizon in IdeaHorizon:
            horizon_ideas = [idea for idea in ideas if idea.horizon == horizon.value]
            lines.extend(
                [
                    "",
                    (
                        f"<b>{HORIZON_LABELS[horizon.value]} · "
                        f"{self.settings.observation_mode(horizon)}</b>"
                    ),
                    f"New {len(horizon_ideas)} · "
                    f"Activated {transition_count(IdeaStatus.ACTIVE.value, horizon.value)} · "
                    f"TP {transition_count(IdeaStatus.TP_HIT.value, horizon.value)} · "
                    f"SL {transition_count(IdeaStatus.SL_HIT.value, horizon.value)} · "
                    f"Expired {transition_count(IdeaStatus.EXPIRED.value, horizon.value)}",
                ]
            )
        message = "\n".join(lines)
        recipients = await self._recipients()
        key = f"daily:{local.date().isoformat()}"
        counters = {"recipients": len(recipients), "sent": 0, "errors": 0}
        for telegram_id in recipients:
            async with self.session_factory() as session:
                exists = await session.scalar(
                    select(ForwardNotification.id).where(
                        ForwardNotification.telegram_id == telegram_id,
                        ForwardNotification.notification_key == key,
                    )
                )
            if exists is not None:
                continue
            try:
                await bot.send_message(telegram_id, message)
                await self._mark_sent(
                    telegram_id,
                    key,
                    "DAILY_SUMMARY",
                    sent_at=sent_at,
                )
                counters["sent"] += 1
            except TelegramForbiddenError:
                await self._deactivate_if_registered(telegram_id)
                counters["errors"] += 1
            except Exception:
                counters["errors"] += 1
                logger.exception("Daily summary failed for chat=%s", telegram_id)
        return counters
