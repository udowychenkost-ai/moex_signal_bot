from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.calibration import CalibrationAssessment
from app.config import Settings
from app.db import create_engine_and_session, init_db
from app.domain import AnalysisMode, StrategyFamily
from app.execution_v24 import EntryPlanV24
from app.final_audit import FinalAuditService
from app.forward import format_strategy_conflicts
from app.intraday_setup import (
    D1_H1_NOT_ALIGNED_REASON,
    NO_DETERMINISTIC_SETUP_REASON,
    SetupDetection,
)
from app.intraday_v24 import (
    MARKET_REGIME_DIRECTION_BLOCKED_REASON,
    IntradayPipelineResultV24,
)
from app.journal import create_actual_trade, create_idea_journal, create_model_trade
from app.market_regime_v24 import MarketRegimeAssessmentV24
from app.models import (
    ActualTradeJournal,
    DecisionSnapshotV24,
    IdeaJournal,
    Instrument,
    ModelTradeJournal,
    PaperTrade,
    RiskBudgetSetting,
    TelegramUser,
    TradingIdea,
    V24NotificationOutbox,
)
from app.observation import DataFreshnessGuard
from app.operations import OperationalService
from app.orchestrator_v24 import (
    IntradayV24Orchestrator,
    V24CandidateDecision,
    V24EvaluationOutcome,
)
from app.reporting import ReportingService
from app.repositories import ensure_user, update_user_settings
from app.risk_policy_admin import (
    RiskPolicyAdminService,
    RiskPolicyAuthorizationError,
    RiskPolicyConfirmationRequired,
    RiskPolicyDraft,
)
from app.scheduler import ScheduledJobs, build_scheduler
from app.v24_domain import (
    AuditGate,
    AuditGateResult,
    CalculationReliability,
    CalibrationStatus,
    EventStateV24,
    FinalDecision,
    JournalDirection,
    KillSwitchReason,
    MarketBiasV24,
    MarketTrendRegime,
    ProbabilityStatus,
    SetupType,
    StatisticalAdmissionStatus,
    V24Classification,
    VolatilityStateV24,
)

NOW = datetime(2026, 9, 2, 10, 30, tzinfo=UTC)


async def database(tmp_path: Path, name: str = "final.db"):
    url = f"sqlite+aiosqlite:///{(tmp_path / name).as_posix()}"
    engine, factory = create_engine_and_session(url)
    await init_db(engine)
    return url, engine, factory


def calibration() -> CalibrationAssessment:
    return CalibrationAssessment(
        calibration_status=CalibrationStatus.UNCALIBRATED,
        statistical_admission=StatisticalAdmissionStatus.NOT_CONFIGURED,
        probability_status=ProbabilityStatus.NOT_RELIABLY_CALIBRATED,
        reliable_probability=None,
        strategy_version="intraday_v2_4",
        calibration_group="CG-TEST",
        policy_version=None,
        n_oos=0,
        n_forward=0,
        n_total=0,
        successes=0,
        interval=None,
        degradation=None,
        reasons=("NO_POLICY",),
    )


def decision(*, hard_fail: AuditGate | None = None, suffix: str = "a") -> V24CandidateDecision:
    gates = {gate: AuditGateResult.PASS for gate in AuditGate}
    gates[AuditGate.CALIBRATION] = AuditGateResult.NOT_REQUIRED
    if hard_fail is not None:
        gates[hard_fail] = AuditGateResult.FAIL
    audit = FinalAuditService().evaluate(gates)
    return V24CandidateDecision(
        candidate_key=hashlib.sha256(suffix.encode("utf-8")).hexdigest(),
        signal_datetime=NOW,
        ticker="SBER",
        direction=JournalDirection.LONG,
        pipeline=None,  # type: ignore[arg-type]
        entry_plan=EntryPlanV24(
            direction=JournalDirection.LONG,
            optimal_entry=100.0,
            acceptable_entry=101.0,
            no_chase_level=102.0,
            invalidation=95.0,
            initial_stop=95.0,
            reliability=CalculationReliability.RELIABLE,
            reasons=(),
        ),
        target=110.0,
        gate_results=gates,
        gate_details={"gates": {gate.value: value.value for gate, value in gates.items()}},
        audit=audit,
        final_decision=FinalDecision.ENTER_NOW,
        classification=V24Classification.PRODUCTION_QUALIFIED,
        calibration=calibration(),
        ai_review=None,
        journal_values={
            "setup": "BREAKOUT",
            "optimal_entry": 100.0,
            "initial_stop": 95.0,
            "tp1": 110.0,
            "audit_status": audit.status.value,
            "final_classification": V24Classification.PRODUCTION_QUALIFIED.value,
            "final_decision": FinalDecision.ENTER_NOW.value,
        },
        snapshot_values={
            "risk_inputs": {"policy_version": "risk-test"},
            "gate_results": {gate.value: value.value for gate, value in gates.items()},
        },
        model_values={},
        model_eligible=False,
    )


def legacy_idea(*, direction: str = "BUY") -> TradingIdea:
    return TradingIdea(
        ticker="SBER",
        instrument_name="Сбербанк",
        direction=direction,
        horizon="SWING_5D",
        primary_timeframe="1h",
        entry_price_from=99.0,
        entry_price_to=101.0,
        current_price=100.0,
        take_profit=110.0,
        stop_loss=95.0,
        confidence=80.0,
        expected_return_pct=10.0,
        risk_pct=5.0,
        risk_reward_ratio=2.0,
        rationale="test",
        invalidation_reason="below 95",
        status="ACTIVE",
        source_timeframes="1h",
        source_candle_begin=NOW,
        material_hash="legacy-test",
        expires_at=NOW + timedelta(days=5),
    )


async def initialize_normal_kill(orchestrator: IntradayV24Orchestrator) -> None:
    await orchestrator.kill_switch.evaluate(set(), source="TEST", checked_at=NOW)


async def test_existing_and_new_users_default_legacy_only_and_setting_persists(
    tmp_path: Path,
) -> None:
    url, engine, factory = await database(tmp_path)
    try:
        async with factory() as session, session.begin():
            user = await ensure_user(session, 101, "u", "15m", 1.0)
            assert user.analysis_mode == AnalysisMode.LEGACY_ONLY.value
            await update_user_settings(session, 101, analysis_mode=AnalysisMode.BOTH.value)
        await engine.dispose()
        engine, factory = create_engine_and_session(url)
        async with factory() as session:
            restarted = await session.get(TelegramUser, 101)
            assert restarted is not None and restarted.analysis_mode == AnalysisMode.BOTH.value
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (AnalysisMode.LEGACY_ONLY, 0),
        (AnalysisMode.INTRADAY_V24_ONLY, 1),
        (AnalysisMode.BOTH, 1),
    ],
)
async def test_v24_delivery_respects_user_mode(
    tmp_path: Path, mode: AnalysisMode, expected: int
) -> None:
    _, engine, factory = await database(tmp_path, f"delivery-{mode.value}.db")
    settings = Settings(_env_file=None, intraday_v24_enabled=True)
    orchestrator = IntradayV24Orchestrator(settings, factory)
    try:
        await initialize_normal_kill(orchestrator)
        async with factory() as session, session.begin():
            session.add(TelegramUser(telegram_id=101, analysis_mode=mode.value))
        result = await orchestrator.persist_decision(decision(suffix=mode.value[0].lower()))
        assert result.notifications_queued == expected
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (AnalysisMode.LEGACY_ONLY, 1),
        (AnalysisMode.INTRADAY_V24_ONLY, 0),
        (AnalysisMode.BOTH, 1),
    ],
)
async def test_legacy_delivery_respects_user_mode(
    tmp_path: Path, mode: AnalysisMode, expected: int
) -> None:
    _, engine, factory = await database(tmp_path, f"legacy-delivery-{mode.value}.db")
    try:
        async with factory() as session, session.begin():
            session.add(
                Instrument(secid="SBER", short_name="Сбербанк", lot_size=10, is_active=True)
            )
            user = TelegramUser(
                telegram_id=101,
                analysis_mode=mode.value,
                minimum_confidence=70,
                idea_horizon="all",
            )
            session.add(user)
            session.add(legacy_idea())
        assert len(await ReportingService(factory).best_for_user(user)) == expected
    finally:
        await engine.dispose()


async def test_disabled_system_flag_overrides_v24_user_selection(tmp_path: Path) -> None:
    _, engine, factory = await database(tmp_path)
    orchestrator = IntradayV24Orchestrator(
        Settings(_env_file=None, intraday_v24_enabled=False, intraday_v24_shadow_enabled=True),
        factory,
    )
    try:
        await initialize_normal_kill(orchestrator)
        async with factory() as session, session.begin():
            session.add(
                TelegramUser(
                    telegram_id=101,
                    analysis_mode=AnalysisMode.INTRADAY_V24_ONLY.value,
                )
            )
        result = await orchestrator.persist_decision(decision())
        assert result.created and result.notifications_queued == 0
        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(IdeaJournal)) == 1
    finally:
        await engine.dispose()


async def test_scan_exposes_pre_candidate_diagnostics_without_persisting_them(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, engine, factory = await database(tmp_path, "scan-diagnostics.db")
    orchestrator = IntradayV24Orchestrator(
        Settings(
            _env_file=None,
            intraday_v24_enabled=False,
            intraday_v24_shadow_enabled=True,
        ),
        factory,
    )
    try:
        await initialize_normal_kill(orchestrator)
        async with factory() as session, session.begin():
            for ticker in ("ERR", "MARKET", "MISMATCH", "MISSING", "NOSETUP"):
                session.add(
                    Instrument(
                        secid=ticker,
                        short_name=ticker,
                        lot_size=10,
                        echelon=1,
                        is_active=True,
                    )
                )

        async def evaluate_stub(instrument: Instrument, *, as_of: datetime):
            del as_of
            if instrument.secid == "ERR":
                raise RuntimeError("diagnostic test")
            if instrument.secid == "MARKET":
                return V24EvaluationOutcome(
                    decision=None,
                    diagnostics=("setup_detected", "market_regime_direction_blocked"),
                )
            diagnostic = {
                "MISMATCH": "d1_h1_not_aligned",
                "MISSING": "missing_mtf",
                "NOSETUP": "no_deterministic_setup",
            }[instrument.secid]
            return V24EvaluationOutcome(decision=None, diagnostics=(diagnostic,))

        monkeypatch.setattr(orchestrator, "_evaluate_instrument", evaluate_stub)
        result = await orchestrator.scan(as_of=NOW)

        assert result["checked"] == 5
        assert result["candidates"] == 0
        assert result["missing_mtf"] == 1
        assert result["d1_h1_not_aligned"] == 1
        assert result["market_regime_direction_blocked"] == 1
        assert result["no_deterministic_setup"] == 1
        assert result["setup_detected"] == 1
        assert result["errors"] == 1
        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(IdeaJournal)) == 0
            assert await session.scalar(select(func.count()).select_from(ModelTradeJournal)) == 0
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    ("setup", "reasons", "expected_diagnostics"),
    [
        (
            SetupDetection(
                SetupType.UNKNOWN,
                None,
                NOW,
                (D1_H1_NOT_ALIGNED_REASON,),
                None,
                ("1d", "1h", "15m", "5m"),
            ),
            (D1_H1_NOT_ALIGNED_REASON,),
            ("d1_h1_not_aligned",),
        ),
        (
            SetupDetection(
                SetupType.UNKNOWN,
                None,
                NOW,
                (NO_DETERMINISTIC_SETUP_REASON,),
                None,
                ("1d", "1h", "15m", "5m"),
            ),
            (NO_DETERMINISTIC_SETUP_REASON,),
            ("no_deterministic_setup",),
        ),
        (
            SetupDetection(
                SetupType.BREAKOUT_RETEST,
                JournalDirection.LONG,
                NOW,
                ("D1 and H1 trend aligned LONG", "market regime RANGE"),
                95.0,
                ("1h", "15m", "5m"),
            ),
            (f"{MARKET_REGIME_DIRECTION_BLOCKED_REASON}:market=RANGE:direction=LONG",),
            ("setup_detected", "market_regime_direction_blocked"),
        ),
    ],
)
async def test_orchestrator_rejects_pre_candidates_with_specific_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    setup: SetupDetection,
    reasons: tuple[str, ...],
    expected_diagnostics: tuple[str, ...],
) -> None:
    _, engine, factory = await database(tmp_path, f"pre-{setup.evidence[-1]}.db")
    orchestrator = IntradayV24Orchestrator(Settings(_env_file=None), factory)
    market = MarketRegimeAssessmentV24(
        regime=MarketTrendRegime.RANGE,
        volatility=VolatilityStateV24.NORMAL_VOL,
        event_state=EventStateV24.NORMAL,
        bias=MarketBiasV24.NEUTRAL,
        as_of=NOW,
        ema20=100,
        ema50=100,
        return_20_pct=0,
        realized_volatility=0.01,
        volatility_percentile=50,
        reasons=(),
    )
    pipeline = IntradayPipelineResultV24(
        strategy_version="intraday_v2_4",
        snapshots={},
        market=market,
        setup=setup,
        no_trade=True,
        no_trade_reasons=reasons,
        maximum_holding_trading_days=2,
        leverage_enabled=False,
    )

    async def market_data(_ticker: str):
        frames = {timeframe: [object()] for timeframe in ("1d", "1h", "15m", "5m")}
        return frames, frames, []

    try:
        monkeypatch.setattr(orchestrator, "_load_market_data", market_data)
        monkeypatch.setattr(orchestrator.pipeline, "analyze", lambda **_kwargs: pipeline)
        outcome = await orchestrator._evaluate_instrument(
            Instrument(secid="SBER", short_name="Сбербанк", echelon=1),
            as_of=NOW,
        )

        assert outcome.decision is None
        assert outcome.diagnostics == expected_diagnostics
    finally:
        await engine.dispose()


async def test_hard_gate_and_gemini_like_approval_cannot_force_publication(tmp_path: Path) -> None:
    _, engine, factory = await database(tmp_path)
    orchestrator = IntradayV24Orchestrator(
        Settings(_env_file=None, intraday_v24_enabled=True), factory
    )
    try:
        await initialize_normal_kill(orchestrator)
        async with factory() as session, session.begin():
            session.add(TelegramUser(telegram_id=101, analysis_mode=AnalysisMode.BOTH.value))
        forged = decision(hard_fail=AuditGate.DATA, suffix="b")
        result = await orchestrator.persist_decision(forged)
        assert result.created and result.notifications_queued == 0
    finally:
        await engine.dispose()


async def test_data_unavailable_and_missing_risk_remain_hard_fail() -> None:
    gates = {gate: AuditGateResult.PASS for gate in AuditGate}
    gates[AuditGate.CALIBRATION] = AuditGateResult.NOT_REQUIRED
    gates[AuditGate.DATA] = AuditGateResult.FAIL
    gates[AuditGate.RISK_BUDGET] = AuditGateResult.FAIL
    result = FinalAuditService().evaluate(gates)
    assert result.status.value == "FAIL"
    assert set(result.reasons) >= {"DATA:FAIL", "RISK_BUDGET:FAIL"}


async def test_kill_switch_blocks_new_v24_publication(tmp_path: Path) -> None:
    _, engine, factory = await database(tmp_path)
    orchestrator = IntradayV24Orchestrator(
        Settings(_env_file=None, intraday_v24_enabled=True), factory
    )
    try:
        await orchestrator.kill_switch.evaluate(
            {KillSwitchReason.DATA_LOSS}, source="TEST", checked_at=NOW
        )
        async with factory() as session, session.begin():
            session.add(TelegramUser(telegram_id=101, analysis_mode=AnalysisMode.BOTH.value))
        result = await orchestrator.persist_decision(decision(suffix="kill"))
        assert result.created and result.notifications_queued == 0
    finally:
        await engine.dispose()


async def test_duplicate_scheduler_retry_is_idempotent_for_journal_snapshot_and_outbox(
    tmp_path: Path,
) -> None:
    _, engine, factory = await database(tmp_path)
    orchestrator = IntradayV24Orchestrator(
        Settings(_env_file=None, intraday_v24_enabled=True), factory
    )
    try:
        await initialize_normal_kill(orchestrator)
        async with factory() as session, session.begin():
            session.add(TelegramUser(telegram_id=101, analysis_mode=AnalysisMode.BOTH.value))
        first = await orchestrator.persist_decision(decision())
        second = await orchestrator.persist_decision(decision())
        assert first.created and second.duplicate
        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(IdeaJournal)) == 1
            assert await session.scalar(select(func.count()).select_from(DecisionSnapshotV24)) == 1
            assert (
                await session.scalar(select(func.count()).select_from(V24NotificationOutbox)) == 1
            )
    finally:
        await engine.dispose()


async def test_snapshot_freezes_all_policy_versions_at_decision_time(tmp_path: Path) -> None:
    _, engine, factory = await database(tmp_path)
    orchestrator = IntradayV24Orchestrator(
        Settings(
            _env_file=None,
            intraday_v24_shadow_enabled=True,
            data_sla_version="sla-7",
            intraday_v24_cost_model_version="cost-4",
            intraday_v24_calibration_model_version="cal-3",
        ),
        factory,
    )
    try:
        await initialize_normal_kill(orchestrator)
        created = await orchestrator.persist_decision(decision(suffix="policy"))
        async with factory() as session:
            snapshot = await session.get(DecisionSnapshotV24, created.trade_id)
        assert snapshot is not None
        assert snapshot.risk_policy_version == "risk-test"
        assert snapshot.data_sla_policy_version == "sla-7"
        assert snapshot.cost_model_version == "cost-4"
        assert snapshot.calibration_model_version == "cal-3"
    finally:
        await engine.dispose()


async def test_mode_change_before_dispatch_suppresses_queued_v24_recommendation(
    tmp_path: Path,
) -> None:
    _, engine, factory = await database(tmp_path)
    orchestrator = IntradayV24Orchestrator(
        Settings(_env_file=None, intraday_v24_enabled=True), factory
    )

    class BotStub:
        def __init__(self) -> None:
            self.messages: list[tuple[int, str]] = []

        async def send_message(self, telegram_id: int, payload: str) -> None:
            self.messages.append((telegram_id, payload))

    try:
        await initialize_normal_kill(orchestrator)
        async with factory() as session, session.begin():
            session.add(TelegramUser(telegram_id=101, analysis_mode=AnalysisMode.BOTH.value))
        result = await orchestrator.persist_decision(decision(suffix="queued-mode"))
        assert result.notifications_queued == 1
        async with factory() as session, session.begin():
            await update_user_settings(
                session,
                101,
                analysis_mode=AnalysisMode.LEGACY_ONLY.value,
            )
        bot = BotStub()
        dispatched = await orchestrator.outbox.dispatch(
            bot,
            now=datetime.now(UTC) + timedelta(minutes=1),
        )
        assert dispatched == {"pending": 1, "sent": 0, "errors": 0}
        assert bot.messages == []
        async with factory() as session:
            status = await session.scalar(select(V24NotificationOutbox.status))
        assert status == "CANCELLED"
    finally:
        await engine.dispose()


async def test_same_ticker_legacy_and_v24_are_independent_and_conflict_is_informational(
    tmp_path: Path,
) -> None:
    _, engine, factory = await database(tmp_path)
    try:
        legacy = legacy_idea(direction="BUY")
        async with factory() as session, session.begin():
            session.add(
                Instrument(
                    secid="SBER",
                    short_name="Сбербанк",
                    full_name="Сбербанк",
                    lot_size=10,
                    is_active=True,
                )
            )
            session.add(legacy)
            v24, _ = await create_idea_journal(
                session,
                signal_datetime=NOW,
                ticker="SBER",
                direction="SHORT",
                journal_values={"final_classification": "SHADOW"},
            )
        from app.operations import V24OpenIdea

        warning = format_strategy_conflicts([legacy], (V24OpenIdea(v24, None, None, "PENDING"),))
        assert legacy.strategy_family == StrategyFamily.LEGACY.value
        assert v24.strategy_family == StrategyFamily.INTRADAY_V24.value
        assert "разные направления" in warning
        assert "Классический: <b>LONG</b>" in warning
        assert "Intraday: <b>SHORT</b>" in warning
    finally:
        await engine.dispose()


async def test_mode_change_does_not_delete_history_or_close_actual_trade(tmp_path: Path) -> None:
    _, engine, factory = await database(tmp_path)
    try:
        async with factory() as session, session.begin():
            session.add(TelegramUser(telegram_id=101, analysis_mode=AnalysisMode.BOTH.value))
            idea, _ = await create_idea_journal(
                session,
                signal_datetime=NOW,
                ticker="SBER",
                direction="LONG",
                journal_values={"optimal_entry": 100.0, "initial_stop": 95.0, "tp1": 110.0},
            )
            model = await create_model_trade(session, trade_id=idea.trade_id)
            actual = await create_actual_trade(
                session,
                trade_id=idea.trade_id,
                model_trade_id=model.model_trade_id,
                confirmed_by_user=True,
                telegram_id=101,
                confirmation_key="mode-change-entry",
                actual_entry_time=NOW,
                actual_entry=100.0,
                values={"actual_position_shares": 10.0},
            )
            await update_user_settings(session, 101, analysis_mode=AnalysisMode.LEGACY_ONLY.value)
        async with factory() as session:
            assert await session.get(IdeaJournal, idea.trade_id) is not None
            assert await session.get(ActualTradeJournal, actual.actual_trade_id) is not None
    finally:
        await engine.dispose()


async def test_legacy_paper_and_v24_model_statistics_are_not_mixed(tmp_path: Path) -> None:
    _, engine, factory = await database(tmp_path)
    settings = Settings(_env_file=None)
    try:
        legacy = legacy_idea()
        legacy.horizon = "POSITION_1M"
        legacy.strategy_version = settings.strategy_version
        legacy.status = "TP_HIT"
        legacy.activated_at = NOW - timedelta(hours=2)
        legacy.activation_price = 100.0
        legacy.closed_at = NOW
        async with factory() as session, session.begin():
            session.add(
                Instrument(secid="SBER", short_name="Сбербанк", lot_size=10, is_active=True)
            )
            session.add(legacy)
            await session.flush()
            session.add(
                PaperTrade(
                    idea_id=legacy.id,
                    ticker="SBER",
                    direction="BUY",
                    status="CLOSED",
                    entry_price=100,
                    entry_fill_price=100,
                    exit_price=110,
                    exit_fill_price=110,
                    units=10,
                    lots=1,
                    risk_budget=100,
                    actual_risk=50,
                    position_value=1_000,
                    gross_pnl=1_000,
                    commission=1,
                    slippage=0,
                    net_pnl=999,
                    r_multiple=2,
                    opened_at=NOW - timedelta(hours=2),
                    closed_at=NOW,
                    exit_reason="TP_HIT",
                )
            )
            v24, _ = await create_idea_journal(
                session,
                signal_datetime=NOW - timedelta(hours=2),
                ticker="SBER",
                direction="LONG",
            )
            await create_model_trade(
                session,
                trade_id=v24.trade_id,
                calibration_eligible=False,
                values={
                    "model_entry_time": NOW - timedelta(hours=1),
                    "model_entry": 100.0,
                    "final_exit_time": NOW,
                    "final_exit": 101.0,
                    "result_r": 1.0,
                    "win_1_0": 1,
                    "net_pl_rub": 100.0,
                },
            )
        operations = OperationalService(
            settings,
            factory,
            DataFreshnessGuard(settings, factory),
        )
        legacy_all = (await operations.statistics(now=NOW))[-1]
        v24_all = (await operations.v24_statistics(now=NOW))[-1]
        position = next(row for row in legacy_all.horizons if row.horizon == "POSITION_1M")
        assert position.net_paper_pnl == 999
        assert v24_all.model.net_pl_rub == 100
        assert v24_all.actual.net_pl_rub is None
    finally:
        await engine.dispose()


def risk_draft(version: str) -> RiskPolicyDraft:
    return RiskPolicyDraft(version, 1_000_000, 1, 2, 5, 3, 3, 80)


async def test_risk_policy_requires_admin_and_explicit_confirmation(tmp_path: Path) -> None:
    _, engine, factory = await database(tmp_path)
    service = RiskPolicyAdminService(factory, [101])
    try:
        with pytest.raises(RiskPolicyAuthorizationError):
            await service.activate(999, risk_draft("risk-1"), confirmed=True, now=NOW)
        with pytest.raises(RiskPolicyConfirmationRequired):
            await service.activate(101, risk_draft("risk-1"), confirmed=False, now=NOW)
        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(RiskBudgetSetting)) == 0
    finally:
        await engine.dispose()


async def test_risk_policy_activation_creates_versions_without_overwriting_old(
    tmp_path: Path,
) -> None:
    _, engine, factory = await database(tmp_path)
    service = RiskPolicyAdminService(factory, [101])
    try:
        effective = datetime.now(UTC) - timedelta(minutes=2)
        await service.activate(101, risk_draft("risk-1"), confirmed=True, now=effective)
        await service.activate(
            101,
            RiskPolicyDraft("risk-2", 2_000_000, 0.5, 1.5, 4, 2, 2, 75),
            confirmed=True,
            now=effective + timedelta(minutes=1),
        )
        current = await service.current(101)
        assert current is not None and current.configuration_version == "risk-2"
        async with factory() as session:
            policies = list(
                await session.scalars(
                    select(RiskBudgetSetting).order_by(RiskBudgetSetting.effective_from)
                )
            )
        assert [item.configuration_version for item in policies] == ["risk-1", "risk-2"]
        assert policies[0].working_capital_rub == 1_000_000
    finally:
        await engine.dispose()


class BrokenScanner:
    async def scan_ideas(self):
        raise RuntimeError("legacy failed")

    async def ingest(self):
        return {"errors": 0}

    async def track_lifecycle(self):
        return {"evaluated": 0, "transitions": 0}

    async def sync_paper(self):
        return {"open": 0, "closed": 0}


class ReportingStub:
    async def dispatch_due(self, _bot):
        return {"reports_sent": 1, "errors": 0}


async def test_legacy_failure_does_not_break_independent_reporting_job() -> None:
    jobs = ScheduledJobs(Settings(_env_file=None), BrokenScanner(), ReportingStub(), object())
    assert (await jobs.scan_market())["errors"] == 1
    assert (await jobs.dispatch_reports())["reports_sent"] == 1


class GoodScanner(BrokenScanner):
    async def scan_ideas(self):
        return {"checked_instruments": 1, "errors": 0}


class BrokenCoordinator:
    async def run(self):
        raise RuntimeError("v24 failed")


async def test_v24_failure_does_not_break_independent_legacy_scan() -> None:
    jobs = ScheduledJobs(
        Settings(_env_file=None, intraday_v24_shadow_enabled=True),
        GoodScanner(),
        ReportingStub(),
        object(),
        v24_coordinator=BrokenCoordinator(),  # type: ignore[arg-type]
    )
    assert (await jobs.intraday_v24_cycle())["errors"] == 1
    assert (await jobs.scan_market())["checked_instruments"] == 1


def test_shadow_flag_registers_one_v24_job_without_enabling_production() -> None:
    class Coordinator:
        async def run(self):
            return {"errors": 0}

    settings = Settings(
        _env_file=None,
        intraday_v24_enabled=False,
        intraday_v24_shadow_enabled=True,
    )
    jobs = ScheduledJobs(
        settings,
        BrokenScanner(),
        ReportingStub(),
        object(),
        v24_coordinator=Coordinator(),  # type: ignore[arg-type]
    )
    scheduler = build_scheduler(settings, jobs)
    assert scheduler.get_job("intraday_v24_cycle") is not None
    assert len([job for job in scheduler.get_jobs() if job.id == "intraday_v24_cycle"]) == 1
    assert settings.intraday_v24_enabled is False
