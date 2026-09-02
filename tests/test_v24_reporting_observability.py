from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select

from app.config import Settings
from app.db import create_engine_and_session, init_db
from app.journal import create_idea_journal
from app.journal_health import JournalStorageHealth
from app.kill_switch import KillSwitchStatus
from app.models import DailyJournalSummaryV24, V24NotificationOutbox
from app.observability_v24 import V24ObservabilityService
from app.position_management_v24 import (
    OpenPositionInputs,
    OpenPositionManagerV24,
    OpenPositionMark,
)
from app.reporting_v24 import (
    DailyJournalServiceV24,
    V24IdeaDisplayContext,
    V24OutboxService,
    format_v24_active_position_card,
    format_v24_idea_card,
)
from app.scheduler_v24 import V24SchedulerCoordinator
from app.v24_domain import (
    CalibrationStatus,
    DataSLAResult,
    JournalDirection,
    KillSwitchReason,
    KillSwitchState,
    PositionAdvisoryAction,
    ProbabilityStatus,
    SetupLifecycleStatus,
    StopManagementState,
)

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def migration_config(database_url: str) -> Config:
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    return config


async def database():
    engine, factory = create_engine_and_session("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    return engine, factory


def position_inputs(
    *,
    data_sla: DataSLAResult = DataSLAResult.PASS,
    current_price: float = 104.0,
    sessions: int = 1,
    higher_low: float | None = None,
    atr_multiplier: float | None = None,
) -> OpenPositionInputs:
    return OpenPositionInputs(
        ticker="SBER",
        trade_id="20260901-SBER-LONG-01",
        direction=JournalDirection.LONG,
        entry=100.0,
        initial_stop=95.0,
        current_stop=95.0,
        tp1=105.0,
        tp2=110.0,
        position_shares=100.0,
        position_rub_at_entry=None,
        entry_time=NOW - timedelta(hours=4),
        mark=OpenPositionMark(
            current_price=current_price,
            price_as_of=NOW,
            data_sla_result=data_sla,
            volume=1_000_000.0,
            vwap=102.0,
            structure="HH_HL",
            market_regime="UPTREND",
            sector="FINANCIALS",
            news="DATA NOT AVAILABLE",
        ),
        execution_quality_now=82.0,
        trading_sessions_elapsed=sessions,
        atr=2.0,
        confirmed_higher_low=higher_low,
        atr_buffer_multiplier=atr_multiplier,
        mfe_pct=6.0,
        mae_pct=-1.0,
        momentum_confirmed=True,
    )


def test_v24_idea_card_never_prints_uncalibrated_probability() -> None:
    from app.models import IdeaJournal

    idea = IdeaJournal(
        trade_id="20260901-SBER-LONG-01",
        strategy_version="intraday_v2_4",
        signal_datetime=NOW,
        ticker="SBER",
        direction="LONG",
        optimal_entry=100.0,
        initial_stop=95.0,
        tp1=110.0,
        tp2=115.0,
        stated_probability=0.99,
        probability_status="NOT_RELIABLY_CALIBRATED",
        data_sla_status="CONFIGURED",
        data_sla_result="PASS",
        price_as_of=NOW,
        data_delay_seconds=2.0,
        microstructure_status="PASS",
        journal_status="AVAILABLE",
        risk_budget_status="CONFIGURED",
        statistical_admission_status="FAIL",
        audit_status="FAIL",
        final_classification="STRUCTURALLY_QUALIFIED",
        final_decision="WATCH",
        reason_for_trade="Проверка структуры.",
    )
    rendered = format_v24_idea_card(
        idea,
        V24IdeaDisplayContext(
            probability_status=ProbabilityStatus.NOT_RELIABLY_CALIBRATED,
            calibrated_probability=0.99,
            calibration_status=CalibrationStatus.UNCALIBRATED,
            setup_status=SetupLifecycleStatus.WATCH,
        ),
    )
    assert "NOT RELIABLY CALIBRATED" in rendered
    assert "99.0%" not in rendered
    assert "TRADE_ID" in rendered
    assert "DATA SLA" in rendered
    assert "MAX SAFE POSITION".title() not in rendered
    assert "Max safe position" in rendered


def test_position_management_is_advisory_fail_closed_and_complete() -> None:
    manager = OpenPositionManagerV24()
    stale = manager.assess(position_inputs(data_sla=DataSLAResult.FAIL))
    assert stale.action is PositionAdvisoryAction.VERIFY_DATA

    time_stop = manager.assess(position_inputs(sessions=2))
    assert time_stop.action is PositionAdvisoryAction.EXIT_REVIEW

    trailed = manager.assess(position_inputs(higher_low=101.0, atr_multiplier=0.5))
    assert trailed.action is PositionAdvisoryAction.MOVE_STOP
    assert trailed.proposed_stop == pytest.approx(100.0)
    assert trailed.stop_state is StopManagementState.BREAK_EVEN
    assert trailed.time_in_trade_hours == pytest.approx(4.0)
    rendered = format_v24_active_position_card(trailed)
    assert "ACTUAL ENTRY" in rendered
    assert "EXECUTION QUALITY NOW" in rendered
    assert "VOLUME / VWAP" in rendered
    assert "advisory-only" in rendered


@pytest.mark.asyncio
async def test_daily_summary_is_persistent_idempotent_and_keeps_actual_unknown() -> None:
    engine, factory = await database()
    try:
        async with factory() as session, session.begin():
            await create_idea_journal(
                session,
                signal_datetime=NOW,
                ticker="SBER",
                direction="LONG",
                journal_values={
                    "audit_status": "PASS",
                    "data_sla_result": "PASS",
                    "data_confidence": "HIGH",
                },
            )
        service = DailyJournalServiceV24(factory, timezone="Europe/Moscow")
        first = await service.persist(date(2026, 9, 1), strategy_version="intraday_v2_4")
        second = await service.persist(date(2026, 9, 1), strategy_version="intraday_v2_4")
        assert first.id == second.id
        assert first.ideas_issued == 1
        assert first.actual_net_pl_rub is None
        assert "автоматически" in first.lessons
        async with factory() as session:
            count = await session.scalar(select(func.count()).select_from(DailyJournalSummaryV24))
        assert count == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_v24_outbox_is_idempotent_and_market_summary_sends_once() -> None:
    engine, factory = await database()
    outbox = V24OutboxService(factory)
    try:
        before = NOW.replace(hour=7)
        assert not await outbox.enqueue_market_summary_once(
            telegram_id=101,
            payload="summary",
            now=before,
            timezone="Europe/Moscow",
            after_hour=11,
        )
        assert await outbox.enqueue_market_summary_once(
            telegram_id=101,
            payload="summary",
            now=NOW,
            timezone="Europe/Moscow",
            after_hour=11,
        )
        assert not await outbox.enqueue_market_summary_once(
            telegram_id=101,
            payload="duplicate",
            now=NOW,
            timezone="Europe/Moscow",
            after_hour=11,
        )
        bot = AsyncMock()
        first = await outbox.dispatch(bot, now=NOW)
        second = await outbox.dispatch(bot, now=NOW)
        assert first == {"pending": 1, "sent": 1, "errors": 0}
        assert second == {"pending": 0, "sent": 0, "errors": 0}
        bot.send_message.assert_awaited_once_with(101, "summary")
        async with factory() as session:
            row = await session.scalar(select(V24NotificationOutbox))
        assert row is not None and row.status == "SENT" and row.attempt_count == 1
    finally:
        await engine.dispose()


class _Health:
    def __init__(self, available: bool) -> None:
        self.available = available

    async def check(self) -> JournalStorageHealth:
        return JournalStorageHealth(
            available=self.available,
            writable=self.available,
            migration_current=self.available,
            revision="20260902_0021" if self.available else None,
            missing_tables=() if self.available else ("idea_journals",),
            checked_at=NOW,
        )


class _Kill:
    def __init__(self, state: KillSwitchState) -> None:
        self.state = state

    async def current(self) -> KillSwitchStatus:
        reasons = (
            (KillSwitchReason.DATA_SLA_FAIL,)
            if self.state is KillSwitchState.CAPITAL_PRESERVATION
            else ()
        )
        return KillSwitchStatus(True, self.state, reasons, "TEST", NOW, 1)

    async def evaluate(self, reasons, **_kwargs) -> KillSwitchStatus:
        state = KillSwitchState.CAPITAL_PRESERVATION if reasons else KillSwitchState.NORMAL
        return KillSwitchStatus(True, state, tuple(sorted(reasons)), "TEST", NOW, 2)


@pytest.mark.asyncio
async def test_scheduler_priority_and_kill_switch_block_only_new_scan() -> None:
    calls: list[str] = []

    async def handler(name: str) -> dict[str, int]:
        calls.append(name)
        return {name.lower(): 1}

    coordinator = V24SchedulerCoordinator(
        journal_health=_Health(True),  # type: ignore[arg-type]
        kill_switch=_Kill(KillSwitchState.NORMAL),  # type: ignore[arg-type]
        actual_handler=lambda: handler("ACTUAL"),
        model_handler=lambda: handler("MODEL"),
        scan_handler=lambda: handler("SCAN"),
    )
    result = await coordinator.run()
    assert calls == ["ACTUAL", "MODEL", "SCAN"]
    assert result["priority_order"] == ["ACTUAL_OPEN", "MODEL_ACTIVE_PENDING", "NEW_SCAN"]

    calls.clear()
    blocked = V24SchedulerCoordinator(
        journal_health=_Health(True),  # type: ignore[arg-type]
        kill_switch=_Kill(KillSwitchState.CAPITAL_PRESERVATION),  # type: ignore[arg-type]
        actual_handler=lambda: handler("ACTUAL"),
        model_handler=lambda: handler("MODEL"),
        scan_handler=lambda: handler("SCAN"),
    )
    blocked_result = await blocked.run()
    assert calls == ["ACTUAL", "MODEL"]
    assert blocked_result["scan"]["reason"] == "CAPITAL_PRESERVATION"


@pytest.mark.asyncio
async def test_v24_observability_uses_real_persistence_and_no_fake_configuration(
    tmp_path,
) -> None:
    database_path = tmp_path / "observability.db"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    await __import__("asyncio").to_thread(command.upgrade, migration_config(database_url), "head")
    engine, factory = create_engine_and_session(database_url)
    try:
        async with factory() as session, session.begin():
            await create_idea_journal(
                session,
                signal_datetime=NOW,
                ticker="SBER",
                direction="LONG",
                journal_values={
                    "data_sla_result": "FAIL",
                    "data_confidence": "LOW",
                    "microstructure_status": "DATA_NOT_AVAILABLE",
                    "probability_status": "NOT_RELIABLY_CALIBRATED",
                    "audit_status": "FAIL",
                },
                snapshot_values={
                    "gate_results": {"final_audit": {"gates": {"DATA_SLA": "FAIL", "DATA": "PASS"}}}
                },
            )
        status = await V24ObservabilityService(Settings(_env_file=None), factory).status(now=NOW)
        assert not status.enabled
        assert status.journal.available
        assert status.data_sla.value == "NOT_CONFIGURED"
        assert status.risk_budget.value == "NOT_CONFIGURED"
        assert status.statistical_admission.value == "NOT_CONFIGURED"
        assert status.calibration is CalibrationStatus.UNCALIBRATED
        assert status.data_availability_failure_count == 1
        assert status.audit_failure_reasons == {"DATA_SLA": 1}
        assert status.microstructure_distribution == {"DATA_NOT_AVAILABLE": 1}
        assert status.journal_write_count == 1
        assert status.journal_error_count == 0
    finally:
        await engine.dispose()
