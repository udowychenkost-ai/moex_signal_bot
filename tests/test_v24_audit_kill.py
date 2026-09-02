from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import fields, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select

from app.adversarial import AdversarialCheck, AdversarialInputs
from app.calibration import CalibrationAssessment, WilsonInterval
from app.classification_v24 import (
    ClassificationInputs,
    V24ClassificationService,
    initial_journal_audit_values,
)
from app.db import create_engine_and_session, init_db
from app.final_audit import FinalAuditService
from app.journal import create_idea_journal
from app.journal_health import JournalHealthService
from app.kill_switch import KillSwitchService
from app.models import DecisionSnapshotV24, IdeaJournal, JournalHealthProbe, KillSwitchEvent
from app.v24_domain import (
    AdversarialResult,
    AuditGate,
    AuditGateResult,
    AuditStatus,
    CalibrationStatus,
    FinalDecision,
    KillSwitchReason,
    KillSwitchState,
    ProbabilityStatus,
    StatisticalAdmissionStatus,
    V24Classification,
)

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def migration_config(database_url: str) -> Config:
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    return config


def all_adversarial_inputs(value: bool | None = False) -> AdversarialInputs:
    return AdversarialInputs(**{field.name: value for field in fields(AdversarialInputs)})


def audit_pass(*, calibration_required: bool = False):
    gates = {gate: AuditGateResult.PASS for gate in AuditGate}
    if not calibration_required:
        gates[AuditGate.CALIBRATION] = AuditGateResult.NOT_REQUIRED
    return FinalAuditService().evaluate(gates)


def calibrated_assessment() -> CalibrationAssessment:
    return CalibrationAssessment(
        calibration_status=CalibrationStatus.CALIBRATED,
        statistical_admission=StatisticalAdmissionStatus.PASS,
        probability_status=ProbabilityStatus.CALIBRATED,
        reliable_probability=0.9,
        strategy_version="intraday_v2_4",
        calibration_group="CG-test",
        policy_version="policy-1",
        n_oos=20,
        n_forward=20,
        n_total=40,
        successes=36,
        interval=WilsonInterval(0.9, 0.75, 0.96, 0.21),
        degradation=None,
        reasons=(),
    )


@pytest.mark.asyncio
async def test_kill_switch_is_fail_closed_persistent_and_transition_only() -> None:
    engine, factory = create_engine_and_session("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    service = KillSwitchService(factory)
    try:
        uninitialized = await service.current()
        assert not uninitialized.initialized
        assert uninitialized.state is KillSwitchState.CAPITAL_PRESERVATION
        assert not uninitialized.allows_new_positions

        normal = await service.evaluate(set(), source="STARTUP", checked_at=NOW)
        assert normal.allows_new_positions
        preserved = await service.evaluate(
            {KillSwitchReason.DATA_SLA_FAIL, KillSwitchReason.JOURNAL_ERROR},
            source="RISK_MONITOR",
            checked_at=NOW,
        )
        assert preserved.state is KillSwitchState.CAPITAL_PRESERVATION
        assert not preserved.allows_new_positions
        repeated = await service.evaluate(
            {KillSwitchReason.JOURNAL_ERROR, KillSwitchReason.DATA_SLA_FAIL},
            source="RISK_MONITOR",
            checked_at=NOW,
        )
        assert repeated.event_id == preserved.event_id

        cleared = await service.clear_manual(
            telegram_id=101,
            cleared_at=NOW,
            notes="operator verified recovery",
        )
        assert cleared.state is KillSwitchState.NORMAL
        async with factory() as session:
            rows = list(
                (
                    await session.execute(select(KillSwitchEvent).order_by(KillSwitchEvent.id))
                ).scalars()
            )
        assert [row.state for row in rows] == [
            KillSwitchState.NORMAL.value,
            KillSwitchState.CAPITAL_PRESERVATION.value,
            KillSwitchState.NORMAL.value,
        ]
        assert rows[-1].created_by_telegram_id == 101
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_journal_health_requires_migration_tables_and_real_rollback_write(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "journal-health.db"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    await asyncio.to_thread(command.upgrade, migration_config(database_url), "head")
    engine, factory = create_engine_and_session(database_url)
    try:
        health = await JournalHealthService(factory).check()
        assert health.available
        assert health.writable
        assert health.migration_current
        assert health.missing_tables == ()
        async with factory() as session:
            probes = await session.scalar(select(func.count()).select_from(JournalHealthProbe))
        assert probes == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_journal_health_fails_closed_without_alembic_revision() -> None:
    engine, factory = create_engine_and_session("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    try:
        health = await JournalHealthService(factory).check()
        assert not health.available
        assert health.writable
        assert not health.migration_current
    finally:
        await engine.dispose()


def test_adversarial_hard_fail_overrides_ai_and_unknown_waits() -> None:
    checker = AdversarialCheck()
    hard = checker.evaluate(
        replace(all_adversarial_inputs(), journal_gap=True),
        ai_result=AdversarialResult.PASS,
    )
    assert hard.result is AdversarialResult.FAIL
    assert "JOURNAL_GAP" in hard.hard_fail_reasons

    waiting = checker.evaluate(replace(all_adversarial_inputs(), probability_uncalibrated=True))
    assert waiting.result is AdversarialResult.WAIT
    assert checker.evaluate(all_adversarial_inputs()).result is AdversarialResult.PASS


def test_final_audit_requires_every_mandatory_gate_without_weighted_bypass() -> None:
    service = FinalAuditService()
    missing = service.evaluate({AuditGate.DATA: AuditGateResult.PASS})
    assert missing.status is AuditStatus.FAIL
    assert AuditGate.JOURNAL in missing.failed_gates

    gates = {gate: AuditGateResult.PASS for gate in AuditGate}
    gates[AuditGate.CALIBRATION] = AuditGateResult.NOT_REQUIRED
    gates[AuditGate.RISK_BUDGET] = AuditGateResult.FAIL
    failed = service.evaluate(gates)
    assert failed.status is AuditStatus.FAIL
    assert failed.failed_gates == (AuditGate.RISK_BUDGET,)

    passed = audit_pass()
    assert passed.status is AuditStatus.PASS
    assert passed.gates[AuditGate.CALIBRATION.value] == AuditGateResult.NOT_REQUIRED.value


def test_final_classification_is_exclusive_and_cannot_hide_audit_failure() -> None:
    adversarial = AdversarialCheck().evaluate(all_adversarial_inputs())
    classifier = V24ClassificationService()
    statistically_qualified = classifier.classify(
        ClassificationInputs(
            final_decision=FinalDecision.ENTER_NOW,
            audit=audit_pass(calibration_required=True),
            adversarial=adversarial,
            calibration=calibrated_assessment(),
            setup_strong=True,
        )
    )
    assert statistically_qualified.classification is V24Classification.STATISTICALLY_QUALIFIED_70

    production = classifier.classify(
        ClassificationInputs(
            final_decision=FinalDecision.ENTER_NOW,
            audit=audit_pass(),
            adversarial=adversarial,
            calibration=None,
            setup_strong=True,
        )
    )
    assert production.classification is V24Classification.PRODUCTION_QUALIFIED

    failed_gates = {gate: AuditGateResult.PASS for gate in AuditGate}
    failed_gates[AuditGate.CALIBRATION] = AuditGateResult.NOT_REQUIRED
    failed_gates[AuditGate.DATA_SLA] = AuditGateResult.FAIL
    structural = classifier.classify(
        ClassificationInputs(
            final_decision=FinalDecision.ENTER_NOW,
            audit=FinalAuditService().evaluate(failed_gates),
            adversarial=adversarial,
            calibration=None,
            setup_strong=True,
        )
    )
    assert structural.classification is V24Classification.STRUCTURALLY_QUALIFIED

    cancelled = classifier.classify(
        ClassificationInputs(
            final_decision=FinalDecision.CANCEL,
            audit=audit_pass(),
            adversarial=adversarial,
            calibration=calibrated_assessment(),
            setup_strong=True,
        )
    )
    assert cancelled.classification is V24Classification.CANCEL


@pytest.mark.asyncio
async def test_audit_reasons_persist_only_in_initial_immutable_snapshot() -> None:
    engine, factory = create_engine_and_session("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    adversarial = AdversarialCheck().evaluate(all_adversarial_inputs())
    audit = audit_pass()
    classification = V24ClassificationService().classify(
        ClassificationInputs(
            final_decision=FinalDecision.ENTER_NOW,
            audit=audit,
            adversarial=adversarial,
            calibration=None,
            setup_strong=True,
        )
    )
    journal_values, snapshot_values = initial_journal_audit_values(
        classification=classification,
        audit=audit,
        adversarial=adversarial,
    )
    try:
        async with factory() as session, session.begin():
            idea, snapshot = await create_idea_journal(
                session,
                signal_datetime=NOW,
                ticker="SBER",
                direction="LONG",
                journal_values=journal_values,
                snapshot_values=snapshot_values,
            )
            trade_id = idea.trade_id
        async with factory() as session:
            persisted_idea = await session.get(IdeaJournal, trade_id)
            persisted_snapshot = await session.get(DecisionSnapshotV24, trade_id)
        assert persisted_idea is not None
        assert persisted_snapshot is not None
        gates = json.loads(persisted_snapshot.gate_results)
        assert gates["final_audit"]["status"] == "PASS"
        assert gates["classification"]["value"] == "PRODUCTION_QUALIFIED"
    finally:
        await engine.dispose()


def test_kill_switch_migration_is_append_only(tmp_path: Path) -> None:
    database_path = tmp_path / "kill-switch.db"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    command.upgrade(migration_config(database_url), "head")
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO kill_switch_events (state, reasons, source, triggered_at)
            VALUES (
                'CAPITAL_PRESERVATION', '["DATA_LOSS"]', 'TEST',
                '2026-09-01 12:00:00'
            )
            """
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE kill_switch_events SET state = 'NORMAL'")
