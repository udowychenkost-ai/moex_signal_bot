from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.domain import IdeaHorizon, IdeaStatus
from app.migrations import HEAD_REVISION
from app.models import (
    CandidateExperiment,
    JobRunState,
    PaperTrade,
    TradingIdea,
    TradingIdeaEvent,
    TradingIdeaSnapshot,
)
from app.observation import DataFreshnessGuard, FreshnessOverview, aware_utc


@dataclass(frozen=True, slots=True)
class HorizonStatistics:
    horizon: str
    mode: str
    generated: int
    activated: int
    tp: int
    sl: int
    expired: int
    win_rate: float | None
    profit_factor: float | None
    average_r: float | None
    net_paper_pnl: float | None
    small_sample: bool


@dataclass(frozen=True, slots=True)
class PeriodStatistics:
    label: str
    start_at: datetime | None
    horizons: tuple[HorizonStatistics, ...]
    strategy_version: str = "v1"
    experiment_cohorts: tuple[ExperimentCohortStatistics, ...] = ()


@dataclass(frozen=True, slots=True)
class ExperimentCohortStatistics:
    horizon: str
    cohort: str
    generated: int
    activated: int
    tp: int
    sl: int
    expired: int
    win_rate: float | None
    profit_factor: float | None
    average_r: float | None
    small_sample: bool


@dataclass(frozen=True, slots=True)
class ApplicationStatus:
    app_version: str
    git_commit: str
    database_ok: bool
    freshness: FreshnessOverview
    latest_scan_time: datetime | None
    next_scan_time: datetime | None
    active_ideas: int
    pending_ideas: int
    ideas_closed_today: int
    scheduler_running: bool
    job_states: tuple[JobRunState, ...]


@dataclass(frozen=True, slots=True)
class IdeaHistory:
    idea: TradingIdea
    snapshot: TradingIdeaSnapshot | None
    events: tuple[TradingIdeaEvent, ...]
    paper_trade: PaperTrade | None


def realized_r(idea: TradingIdea) -> float | None:
    if idea.activation_price is None or idea.close_price is None:
        return None
    risk = abs(idea.activation_price - idea.stop_loss)
    if risk <= 0:
        return None
    movement = (
        idea.close_price - idea.activation_price
        if idea.direction == "BUY"
        else idea.activation_price - idea.close_price
    )
    return movement / risk


class OperationalService:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        freshness: DataFreshnessGuard,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.freshness = freshness
        self.scheduler: AsyncIOScheduler | None = None

    def attach_scheduler(self, scheduler: AsyncIOScheduler) -> None:
        self.scheduler = scheduler

    async def job_started(self, job_name: str, *, at: datetime | None = None) -> None:
        started = at or datetime.now(UTC)
        async with self.session_factory() as session, session.begin():
            state = await session.get(JobRunState, job_name)
            if state is None:
                state = JobRunState(job_name=job_name)
                session.add(state)
            state.started_at = started
            state.finished_at = None
            state.success = None
            state.error = ""
            state.updated_at = started

    async def job_finished(
        self,
        job_name: str,
        *,
        success: bool,
        details: dict[str, Any] | None = None,
        error: str = "",
        at: datetime | None = None,
    ) -> None:
        finished = at or datetime.now(UTC)
        async with self.session_factory() as session, session.begin():
            state = await session.get(JobRunState, job_name)
            if state is None:
                state = JobRunState(job_name=job_name, started_at=finished)
                session.add(state)
            state.finished_at = finished
            state.success = success
            state.details = json.dumps(details or {}, ensure_ascii=False, sort_keys=True)
            state.error = error[:4_000]
            state.updated_at = finished
            if success:
                state.last_success_at = finished

    async def database_ok(self) -> bool:
        try:
            async with self.session_factory() as session:
                connected = bool(await session.scalar(text("SELECT 1")))
                revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
                return connected and revision == HEAD_REVISION
        except Exception:
            return False

    async def status(self, *, now: datetime | None = None) -> ApplicationStatus:
        checked_at = aware_utc(now or datetime.now(UTC))
        local_now = checked_at.astimezone(ZoneInfo(self.settings.scheduler_timezone))
        local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        utc_start = local_start.astimezone(UTC)
        async with self.session_factory() as session:
            active = int(
                await session.scalar(
                    select(func.count())
                    .select_from(TradingIdea)
                    .where(TradingIdea.status == IdeaStatus.ACTIVE.value)
                )
                or 0
            )
            pending = int(
                await session.scalar(
                    select(func.count())
                    .select_from(TradingIdea)
                    .where(TradingIdea.status == IdeaStatus.PENDING_ENTRY.value)
                )
                or 0
            )
            closed_today = int(
                await session.scalar(
                    select(func.count())
                    .select_from(TradingIdea)
                    .where(TradingIdea.closed_at >= utc_start)
                )
                or 0
            )
            states = tuple(
                await session.scalars(select(JobRunState).order_by(JobRunState.job_name))
            )
        scan_state = next((item for item in states if item.job_name == "idea_scanning"), None)
        scan_job = self.scheduler.get_job("idea_scanning") if self.scheduler else None
        return ApplicationStatus(
            app_version=self.settings.app_version,
            git_commit=self.settings.git_commit,
            database_ok=await self.database_ok(),
            freshness=await self.freshness.overview(now=checked_at),
            latest_scan_time=scan_state.finished_at if scan_state else None,
            next_scan_time=scan_job.next_run_time if scan_job else None,
            active_ideas=active,
            pending_ideas=pending,
            ideas_closed_today=closed_today,
            scheduler_running=bool(self.scheduler and self.scheduler.running),
            job_states=states,
        )

    async def statistics(self, *, now: datetime | None = None) -> tuple[PeriodStatistics, ...]:
        checked_at = aware_utc(now or datetime.now(UTC))
        async with self.session_factory() as session:
            ideas = list(
                await session.scalars(
                    select(TradingIdea).where(
                        TradingIdea.strategy_version == self.settings.strategy_version
                    )
                )
            )
            trades = list(await session.scalars(select(PaperTrade)))
            experiments = list(
                await session.scalars(
                    select(CandidateExperiment).where(
                        CandidateExperiment.strategy_version == self.settings.strategy_version
                    )
                )
            )
        trade_by_idea = {trade.idea_id: trade for trade in trades}
        periods = (
            ("7 дней", checked_at - timedelta(days=7)),
            ("30 дней", checked_at - timedelta(days=30)),
            ("всё время", None),
        )
        result: list[PeriodStatistics] = []
        for label, start_at in periods:
            selected = [
                idea for idea in ideas if start_at is None or aware_utc(idea.created_at) >= start_at
            ]
            horizon_rows: list[HorizonStatistics] = []
            experiment_rows: list[ExperimentCohortStatistics] = []
            for horizon in IdeaHorizon:
                rows = [idea for idea in selected if idea.horizon == horizon.value]
                activated_rows = [idea for idea in rows if idea.activated_at is not None]
                mode = self.settings.observation_mode(horizon)
                r_values: list[float] = []
                paper_values: list[float] = []
                for idea in activated_rows:
                    trade = trade_by_idea.get(idea.id)
                    if mode == "PAPER" and trade is not None and trade.status == "CLOSED":
                        r_values.append(trade.r_multiple)
                        paper_values.append(trade.net_pnl)
                    elif mode == "RESEARCH":
                        value = realized_r(idea)
                        if value is not None:
                            r_values.append(value)
                gains = sum(value for value in r_values if value > 0)
                losses = abs(sum(value for value in r_values if value < 0))
                decisive = sum(
                    idea.status in {IdeaStatus.TP_HIT.value, IdeaStatus.SL_HIT.value}
                    for idea in rows
                )
                tp = sum(idea.status == IdeaStatus.TP_HIT.value for idea in rows)
                horizon_rows.append(
                    HorizonStatistics(
                        horizon=horizon.value,
                        mode=mode,
                        generated=len(rows),
                        activated=len(activated_rows),
                        tp=tp,
                        sl=sum(idea.status == IdeaStatus.SL_HIT.value for idea in rows),
                        expired=sum(idea.status == IdeaStatus.EXPIRED.value for idea in rows),
                        win_rate=tp / decisive * 100 if decisive else None,
                        profit_factor=(
                            gains / losses if losses else (float("inf") if gains else None)
                        ),
                        average_r=sum(r_values) / len(r_values) if r_values else None,
                        net_paper_pnl=sum(paper_values) if mode == "PAPER" else None,
                        small_sample=len(activated_rows) < self.settings.small_sample_threshold,
                    )
                )
                candidate_rows = [
                    row
                    for row in experiments
                    if row.horizon == horizon.value
                    and (start_at is None or aware_utc(row.decision_at) >= start_at)
                ]
                cohorts = (
                    ("ALL QUANT CANDIDATES", candidate_rows),
                    (
                        "AI APPROVED",
                        [
                            row
                            for row in candidate_rows
                            if row.ai_verdict in {"STRONG_APPROVE", "APPROVE"}
                        ],
                    ),
                    (
                        "AI REJECTED",
                        [row for row in candidate_rows if row.ai_verdict == "REJECT"],
                    ),
                )
                for cohort_name, cohort_rows in cohorts:
                    r_values = [row.actual_r for row in cohort_rows if row.actual_r is not None]
                    gains = sum(value for value in r_values if value > 0)
                    losses = abs(sum(value for value in r_values if value < 0))
                    tp_count = sum(row.status == IdeaStatus.TP_HIT.value for row in cohort_rows)
                    sl_count = sum(row.status == IdeaStatus.SL_HIT.value for row in cohort_rows)
                    decisive = tp_count + sl_count
                    activated_count = sum(row.activated_at is not None for row in cohort_rows)
                    experiment_rows.append(
                        ExperimentCohortStatistics(
                            horizon=horizon.value,
                            cohort=cohort_name,
                            generated=len(cohort_rows),
                            activated=activated_count,
                            tp=tp_count,
                            sl=sl_count,
                            expired=sum(
                                row.status == IdeaStatus.EXPIRED.value for row in cohort_rows
                            ),
                            win_rate=tp_count / decisive * 100 if decisive else None,
                            profit_factor=(
                                gains / losses if losses else (float("inf") if gains else None)
                            ),
                            average_r=(sum(r_values) / len(r_values) if r_values else None),
                            small_sample=(activated_count < self.settings.small_sample_threshold),
                        )
                    )
            result.append(
                PeriodStatistics(
                    label,
                    start_at,
                    tuple(horizon_rows),
                    strategy_version=self.settings.strategy_version,
                    experiment_cohorts=tuple(experiment_rows),
                )
            )
        return tuple(result)

    async def idea_history(self, idea_id: int) -> IdeaHistory | None:
        async with self.session_factory() as session:
            idea = await session.get(TradingIdea, idea_id)
            if idea is None:
                return None
            snapshot = await session.get(TradingIdeaSnapshot, idea_id)
            events = tuple(
                await session.scalars(
                    select(TradingIdeaEvent)
                    .where(TradingIdeaEvent.idea_id == idea_id)
                    .order_by(TradingIdeaEvent.occurred_at, TradingIdeaEvent.id)
                )
            )
            paper = await session.scalar(select(PaperTrade).where(PaperTrade.idea_id == idea_id))
        return IdeaHistory(idea, snapshot, events, paper)
