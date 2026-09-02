from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from html import escape
from zoneinfo import ZoneInfo

from aiogram import Bot
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain import AnalysisMode
from app.models import (
    ActualTradeJournal,
    DailyJournalSummaryV24,
    IdeaJournal,
    ModelTradeJournal,
    TelegramUser,
    V24NotificationOutbox,
)
from app.position_management_v24 import OpenPositionAssessment
from app.statistics_v24 import TradeObservation, V24StatisticsService, calculate_performance
from app.v24_domain import CalibrationStatus, ProbabilityStatus, SetupLifecycleStatus


@dataclass(frozen=True, slots=True)
class V24IdeaDisplayContext:
    probability_status: ProbabilityStatus
    calibrated_probability: float | None
    calibration_status: CalibrationStatus
    setup_status: SetupLifecycleStatus
    horizon: str = "INTRADAY · max 2 trading days"


def _number(value: float | None, *, suffix: str = "") -> str:
    return "DATA NOT AVAILABLE" if value is None else f"{value:,.2f}{suffix}"


def _text(value: object | None) -> str:
    return "DATA NOT AVAILABLE" if value is None or str(value).strip() == "" else escape(str(value))


def _derived_percent(direction: str, entry: float | None, target: float | None) -> float | None:
    if entry is None or target is None or entry <= 0:
        return None
    multiplier = 1 if direction == "LONG" else -1
    return multiplier * (target - entry) / entry * 100


def _aware_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def format_v24_idea_card(
    idea: IdeaJournal,
    context: V24IdeaDisplayContext,
    *,
    compact: bool = False,
) -> str:
    entry = idea.optimal_entry
    potential = _derived_percent(idea.direction, entry, idea.tp1)
    raw_risk = _derived_percent(idea.direction, entry, idea.initial_stop)
    risk = abs(raw_risk) if raw_risk is not None else None
    probability = (
        f"{context.calibrated_probability * 100:.1f}%"
        if (
            context.probability_status is ProbabilityStatus.CALIBRATED
            and context.calibrated_probability is not None
        )
        else "NOT RELIABLY CALIBRATED"
    )
    direction = "BUY" if idea.direction == "LONG" else "SELL"
    lines = [
        f"📌 <b>{escape(idea.ticker)} — {direction}</b>",
        f"TRADE_ID: <code>{escape(idea.trade_id)}</code>",
        f"CLASSIFICATION: <b>{_text(idea.final_classification)}</b>",
        f"PRICE AS OF: <b>{_text(idea.price_as_of)}</b>",
        f"DATA DELAY: <b>{_number(idea.data_delay_seconds, suffix=' sec')}</b>",
        f"DATA SLA: <b>{_text(idea.data_sla_status)} / {_text(idea.data_sla_result)}</b>",
        f"MICROSTRUCTURE: <b>{_text(idea.microstructure_status)}</b>",
        f"JOURNAL: <b>{_text(idea.journal_status)}</b>",
        f"RISK BUDGET: <b>{_text(idea.risk_budget_status)}</b>",
        f"STATISTICAL ADMISSION: <b>{_text(idea.statistical_admission_status)}</b>",
        "",
        f"ENTRY: <b>{_number(idea.optimal_entry)}</b>",
        f"TP1 / TP2: <b>{_number(idea.tp1)} / {_number(idea.tp2)}</b>",
        f"STOP: <b>{_number(idea.initial_stop)}</b>",
        f"Potential: <b>{_number(potential, suffix='%')}</b>",
        f"Risk: <b>{_number(risk, suffix='%')}</b>",
        f"R:R: <b>{_number(idea.gross_rr)}</b>",
        f"Probability: <b>{probability}</b>",
        f"AUDIT: <b>{_text(idea.audit_status)}</b>",
        f"ACTION: <b>{_text(idea.final_decision)}</b>",
        f"WHY: {_text(idea.reason_for_trade)}",
    ]
    if compact:
        return "\n".join(lines)
    details = [
        "",
        "<b>Risk / liquidity</b>",
        f"Risk to stop: <b>{_number(idea.risk_to_stop_rub, suffix=' ₽')}</b>",
        f"Risk % capital: <b>{_number(idea.risk_to_stop_pct_capital, suffix='%')}</b>",
        f"Liquidity cap: <b>{_number(idea.liquidity_cap, suffix=' ₽')}</b>",
        f"Normal / Fast / Stress exit cap: <b>{_number(idea.normal_exit_cap)} / "
        f"{_number(idea.fast_exit_cap)} / {_number(idea.stress_exit_cap)} ₽</b>",
        f"Risk cap: <b>{_number(idea.risk_cap, suffix=' ₽')}</b>",
        f"Max safe position: <b>{_number(idea.max_safe_position, suffix=' ₽')}</b>",
        f"Recommended position: <b>{_number(idea.recommended_position, suffix=' ₽')}</b>",
        "",
        "<b>Quality / context</b>",
        f"Setup quality: <b>{_number(idea.setup_quality)}</b>",
        f"Execution quality: <b>{_number(idea.execution_quality)}</b>",
        f"Opportunity cost: <b>{_number(idea.opportunity_cost)}</b>",
        f"Horizon: <b>{escape(context.horizon)}</b>",
        f"Data confidence: <b>{_text(idea.data_confidence)}</b>",
        f"Calibration: <b>{context.calibration_status.value}</b>",
        f"Setup status: <b>{context.setup_status.value}</b>",
        "",
        "⚠️ Это аналитический журнал. Торговое поручение не создаётся.",
    ]
    return "\n".join([*lines, *details])


def format_v24_active_position_card(assessment: OpenPositionAssessment) -> str:
    return (
        f"📍 <b>{escape(assessment.ticker)} — ACTIVE</b>\n"
        f"TRADE_ID: <code>{escape(assessment.trade_id)}</code>\n"
        f"ACTUAL ENTRY: <b>{assessment.actual_entry:g}</b>\n"
        f"CURRENT PRICE: <b>{assessment.current_price:g}</b> · "
        f"{escape(assessment.price_as_of.isoformat())}\n"
        f"DATA SLA: <b>{assessment.data_sla_result.value}</b>\n"
        f"P/L: <b>{assessment.pnl_rub:+,.2f} ₽ / {assessment.pnl_pct:+.2f}%</b>\n"
        f"CURRENT R: <b>{assessment.current_r:+.2f}R</b>\n"
        f"CURRENT STOP: <b>{assessment.current_stop:g}</b>\n"
        f"TP1 / TP2: <b>{_number(assessment.tp1)} / {_number(assessment.tp2)}</b>\n"
        f"MFE / MAE: <b>{_number(assessment.mfe_pct, suffix='%')} / "
        f"{_number(assessment.mae_pct, suffix='%')}</b>\n"
        f"PROFIT GIVEBACK: <b>{_number(assessment.profit_giveback_pct, suffix='%')}</b>\n"
        f"TIME IN TRADE: <b>{assessment.time_in_trade_hours:.1f} h</b>\n"
        f"VOLUME / VWAP: <b>{_number(assessment.volume)} / "
        f"{_number(assessment.vwap)}</b>\n"
        f"STRUCTURE / REGIME: <b>{_text(assessment.structure)} / "
        f"{_text(assessment.market_regime)}</b>\n"
        f"SECTOR / NEWS: <b>{_text(assessment.sector)} / {_text(assessment.news)}</b>\n"
        f"THESIS: <b>{assessment.thesis_status.value}</b>\n"
        f"EXECUTION QUALITY NOW: <b>{_number(assessment.execution_quality_now)}</b>\n"
        f"STOP STATE: <b>{assessment.stop_state.value}</b>\n"
        f"ACTION NOW: <b>{assessment.action.value}</b>\n"
        f"NEW STOP: <b>{_number(assessment.proposed_stop)}</b>\n"
        f"NEXT TARGET: <b>{_number(assessment.next_target)}</b>\n"
        f"WHY: {_text(', '.join(assessment.reasons))}\n\n"
        "Рекомендация advisory-only. Исполнение подтверждает пользователь."
    )


def _local_bounds(day: date, timezone: str) -> tuple[datetime, datetime]:
    zone = ZoneInfo(timezone)
    start = datetime.combine(day, time.min, tzinfo=zone).astimezone(UTC)
    end = datetime.combine(day, time.max, tzinfo=zone).astimezone(UTC)
    return start, end


class DailyJournalServiceV24:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        timezone: str,
    ) -> None:
        self.session_factory = session_factory
        self.timezone = timezone
        self.statistics = V24StatisticsService(session_factory)

    async def persist(
        self,
        summary_date: date,
        *,
        strategy_version: str,
    ) -> DailyJournalSummaryV24:
        async with self.session_factory() as session:
            existing = await session.scalar(
                select(DailyJournalSummaryV24).where(
                    DailyJournalSummaryV24.summary_date == summary_date,
                    DailyJournalSummaryV24.strategy_version == strategy_version,
                )
            )
        if existing is not None:
            return existing
        start, end = _local_bounds(summary_date, self.timezone)
        model = [
            item
            for item in await self.statistics.model_observations(strategy_version=strategy_version)
            if start <= _aware_utc(item.closed_at) <= end
        ]
        actual = [
            item
            for item in await self.statistics.actual_observations(strategy_version=strategy_version)
            if start <= _aware_utc(item.closed_at) <= end
        ]
        model_metrics = calculate_performance(model)
        actual_metrics = calculate_performance(actual)
        combined = [*model, *actual]
        ranked = [item for item in combined if item.result_r is not None]
        best = max(ranked, key=lambda item: item.result_r).trade_id if ranked else None
        worst = min(ranked, key=lambda item: item.result_r).trade_id if ranked else None
        async with self.session_factory() as session:
            ideas_issued = int(
                await session.scalar(
                    select(func.count())
                    .select_from(IdeaJournal)
                    .where(
                        IdeaJournal.strategy_version == strategy_version,
                        IdeaJournal.signal_datetime >= start,
                        IdeaJournal.signal_datetime <= end,
                    )
                )
                or 0
            )
            model_opened = int(
                await session.scalar(
                    select(func.count())
                    .select_from(ModelTradeJournal)
                    .where(
                        ModelTradeJournal.strategy_version == strategy_version,
                        ModelTradeJournal.model_entry_time >= start,
                        ModelTradeJournal.model_entry_time <= end,
                    )
                )
                or 0
            )
            model_closed = int(
                await session.scalar(
                    select(func.count())
                    .select_from(ModelTradeJournal)
                    .where(
                        ModelTradeJournal.strategy_version == strategy_version,
                        ModelTradeJournal.final_exit_time >= start,
                        ModelTradeJournal.final_exit_time <= end,
                    )
                )
                or 0
            )
            actual_confirmed = int(
                await session.scalar(
                    select(func.count())
                    .select_from(ActualTradeJournal)
                    .where(
                        ActualTradeJournal.strategy_version == strategy_version,
                        ActualTradeJournal.confirmed_at >= start,
                        ActualTradeJournal.confirmed_at <= end,
                    )
                )
                or 0
            )
            day_ideas = list(
                await session.scalars(
                    select(IdeaJournal).where(
                        IdeaJournal.strategy_version == strategy_version,
                        IdeaJournal.signal_datetime >= start,
                        IdeaJournal.signal_datetime <= end,
                    )
                )
            )
            ambiguous = int(
                await session.scalar(
                    select(func.count())
                    .select_from(ModelTradeJournal)
                    .where(
                        ModelTradeJournal.strategy_version == strategy_version,
                        ModelTradeJournal.ambiguous_execution.is_(True),
                        ModelTradeJournal.created_at >= start,
                        ModelTradeJournal.created_at <= end,
                    )
                )
                or 0
            )
        data_issues = {
            "data_sla_not_pass": sum(item.data_sla_result != "PASS" for item in day_ideas),
            "low_or_unknown_confidence": sum(
                item.data_confidence not in {"HIGH", "MEDIUM"} for item in day_ideas
            ),
        }
        execution_issues = {"ambiguous_execution": ambiguous}
        violations = sum(item.audit_status == "FAIL" for item in day_ideas)
        lessons = self._lessons(model, actual, data_issues, execution_issues)
        drawdowns = [
            value
            for value in (model_metrics.max_drawdown_rub, actual_metrics.max_drawdown_rub)
            if value is not None
        ]
        record = DailyJournalSummaryV24(
            summary_date=summary_date,
            strategy_version=strategy_version,
            ideas_issued=ideas_issued,
            model_trades_opened=model_opened,
            model_trades_closed=model_closed,
            actual_trades_confirmed=actual_confirmed,
            model_net_pl_rub=model_metrics.net_pl_rub,
            actual_net_pl_rub=actual_metrics.net_pl_rub,
            model_avg_r=model_metrics.average_r,
            actual_avg_r=actual_metrics.average_r,
            best_trade=best,
            worst_trade=worst,
            max_intraday_drawdown=max(drawdowns) if drawdowns else None,
            data_issues=json.dumps(data_issues, ensure_ascii=False, sort_keys=True),
            execution_issues=json.dumps(
                execution_issues,
                ensure_ascii=False,
                sort_keys=True,
            ),
            false_positives=None,
            false_rejects=None,
            rule_violations=violations,
            lessons=json.dumps(lessons, ensure_ascii=False),
        )
        async with self.session_factory() as session, session.begin():
            session.add(record)
            await session.flush()
        return record

    @staticmethod
    def _lessons(
        model: list[TradeObservation],
        actual: list[TradeObservation],
        data_issues: dict[str, int],
        execution_issues: dict[str, int],
    ) -> list[str]:
        observations = [
            f"Закрыто MODEL: {len(model)}; ACTUAL: {len(actual)}.",
            f"Data issues: {sum(data_issues.values())}; "
            f"execution issues: {sum(execution_issues.values())}.",
        ]
        if not model and not actual:
            observations.append("Закрытых сделок для оценки результата нет.")
        observations.append("Наблюдения не изменяют стратегию автоматически.")
        return observations


def format_daily_journal_summary(record: DailyJournalSummaryV24) -> str:
    return (
        f"📓 <b>V2.4 DAILY JOURNAL — {record.summary_date.isoformat()}</b>\n\n"
        f"Ideas issued: <b>{record.ideas_issued}</b>\n"
        f"Model opened / closed: <b>{record.model_trades_opened} / "
        f"{record.model_trades_closed}</b>\n"
        f"Actual confirmed: <b>{record.actual_trades_confirmed}</b>\n"
        f"MODEL net / avg R: <b>{_number(record.model_net_pl_rub, suffix=' ₽')} / "
        f"{_number(record.model_avg_r)}</b>\n"
        f"ACTUAL net / avg R: <b>{_number(record.actual_net_pl_rub, suffix=' ₽')} / "
        f"{_number(record.actual_avg_r)}</b>\n"
        f"Best / worst: <b>{_text(record.best_trade)} / {_text(record.worst_trade)}</b>\n"
        f"Max intraday drawdown: <b>{_number(record.max_intraday_drawdown, suffix=' ₽')}</b>\n"
        f"Rule violations: <b>{record.rule_violations}</b>\n\n"
        "LESSONS — только наблюдения; стратегия автоматически не меняется."
    )


class V24OutboxService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def enqueue(
        self,
        *,
        telegram_id: int,
        notification_key: str,
        notification_type: str,
        payload: str,
        available_at: datetime,
        trade_id: str | None = None,
    ) -> bool:
        async with self.session_factory() as session, session.begin():
            return await self.enqueue_in_session(
                session,
                telegram_id=telegram_id,
                notification_key=notification_key,
                notification_type=notification_type,
                payload=payload,
                available_at=available_at,
                trade_id=trade_id,
            )

    async def enqueue_in_session(
        self,
        session: AsyncSession,
        *,
        telegram_id: int,
        notification_key: str,
        notification_type: str,
        payload: str,
        available_at: datetime,
        trade_id: str | None = None,
    ) -> bool:
        """Enqueue within the decision transaction to close the journal/send crash gap."""

        values = {
            "telegram_id": telegram_id,
            "notification_key": notification_key,
            "notification_type": notification_type,
            "trade_id": trade_id,
            "payload": payload,
            "status": "PENDING",
            "attempt_count": 0,
            "available_at": available_at,
            "last_error": "",
        }
        dialect = session.get_bind().dialect.name
        if dialect == "postgresql":
            statement = postgresql_insert(V24NotificationOutbox).values(**values)
        elif dialect == "sqlite":
            statement = sqlite_insert(V24NotificationOutbox).values(**values)
        else:
            existing = await session.scalar(
                select(V24NotificationOutbox.id).where(
                    V24NotificationOutbox.telegram_id == telegram_id,
                    V24NotificationOutbox.notification_key == notification_key,
                )
            )
            if existing is not None:
                return False
            session.add(V24NotificationOutbox(**values))
            return True
        statement = statement.on_conflict_do_nothing(
            index_elements=["telegram_id", "notification_key"]
        )
        result = await session.execute(statement)
        return bool(result.rowcount)

    async def enqueue_market_summary_once(
        self,
        *,
        telegram_id: int,
        payload: str,
        now: datetime,
        timezone: str,
        after_hour: int,
    ) -> bool:
        local = now.astimezone(ZoneInfo(timezone))
        if local.hour < after_hour:
            return False
        return await self.enqueue(
            telegram_id=telegram_id,
            notification_key=f"market-summary:{local.date().isoformat()}",
            notification_type="DAILY_MARKET_SUMMARY",
            payload=payload,
            available_at=now,
        )

    async def dispatch(self, bot: Bot, *, now: datetime | None = None) -> dict[str, int]:
        sent_at = now or datetime.now(UTC)
        async with self.session_factory() as session:
            pending = list(
                await session.scalars(
                    select(V24NotificationOutbox)
                    .where(
                        V24NotificationOutbox.status == "PENDING",
                        V24NotificationOutbox.available_at <= sent_at,
                    )
                    .order_by(V24NotificationOutbox.id)
                )
            )
        counters = {"pending": len(pending), "sent": 0, "errors": 0}
        for item in pending:
            async with self.session_factory() as session, session.begin():
                claimed = await session.get(V24NotificationOutbox, item.id, with_for_update=True)
                if claimed is None or claimed.status != "PENDING":
                    continue
                user = await session.get(TelegramUser, claimed.telegram_id)
                if (
                    user is not None
                    and claimed.notification_type
                    in {"NEW_V24_IDEA", "V24_DAILY_JOURNAL", "DAILY_MARKET_SUMMARY"}
                    and not AnalysisMode(user.analysis_mode).includes_v24
                ):
                    claimed.status = "CANCELLED"
                    claimed.last_error = "USER_ANALYSIS_MODE_CHANGED"
                    continue
                claimed.status = "SENDING"
                claimed.attempt_count += 1
            try:
                await bot.send_message(item.telegram_id, item.payload)
            except Exception as error:
                counters["errors"] += 1
                async with self.session_factory() as session, session.begin():
                    failed = await session.get(V24NotificationOutbox, item.id)
                    if failed is not None:
                        failed.status = "ERROR"
                        failed.last_error = f"{type(error).__name__}: send failed"
            else:
                counters["sent"] += 1
                async with self.session_factory() as session, session.begin():
                    completed = await session.get(V24NotificationOutbox, item.id)
                    if completed is not None:
                        completed.status = "SENT"
                        completed.sent_at = sent_at
                        completed.last_error = ""
        return counters
