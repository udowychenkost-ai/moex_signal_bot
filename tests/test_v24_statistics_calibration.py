from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from app.actual_trades import (
    ActualActionConfirmation,
    ActualTradeConfirmation,
    ActualTradeService,
)
from app.calibration import (
    AdmissionPolicyInput,
    CalibrationCohort,
    CalibrationService,
    create_admission_policy,
    wilson_interval,
)
from app.db import create_engine_and_session, init_db
from app.journal import create_idea_journal, create_model_trade
from app.statistics_v24 import (
    DegradationPolicy,
    ModelDegradationDetector,
    TradeObservation,
    V24StatisticsService,
    regime_statistics,
    reliability_metrics,
    rolling_statistics,
    setup_statistics,
)
from app.v24_domain import (
    ActualTradeAction,
    CalibrationStatus,
    DegradationStatus,
    ProbabilityStatus,
    RegimePerformanceStatus,
    SampleType,
    SetupLifecycleStatus,
    StatisticalAdmissionStatus,
)

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
COHORT = CalibrationCohort(
    setup="BREAKOUT_RETEST",
    direction="LONG",
    market_regime="UPTREND",
    trend="UPTREND",
    volatility="NORMAL_VOL",
    time_of_day="MID_SESSION",
    rr_bucket="2_TO_3",
    liquidity_state="LIQUID",
    context="NO_EVENT",
)


def migration_config(database_url: str) -> Config:
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    return config


async def database():
    engine, factory = create_engine_and_session("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    return engine, factory


def observation(index: int, *, win: int = 1, result_r: float = 1.0) -> TradeObservation:
    return TradeObservation(
        trade_id=f"T-{index}",
        source="MODEL",
        strategy_version="intraday_v2_4",
        closed_at=NOW + timedelta(minutes=index),
        direction="LONG",
        setup="BREAKOUT_RETEST",
        market_regime="UPTREND",
        trend="UPTREND",
        volatility="NORMAL_VOL",
        time_of_day="MID_SESSION",
        rr_bucket="2_TO_3",
        liquidity_state="LIQUID",
        context="NO_EVENT",
        calibration_group=COHORT.key(),
        calibration_eligible=True,
        sample_type="FORWARD",
        win=win,
        tp_before_sl=win,
        result_r=result_r,
        net_pl_rub=result_r * 1_000,
        mfe_pct=2.0,
        mae_pct=-1.0,
        capture_ratio=0.5,
        holding_hours=3.0,
        stated_probability=0.8,
        execution_quality=80.0,
    )


async def seed_calibration_rows(factory, *, count: int = 20) -> None:
    async with factory() as session, session.begin():
        for index in range(count):
            signal_time = NOW + timedelta(minutes=index)
            idea, _ = await create_idea_journal(
                session,
                signal_datetime=signal_time,
                ticker=f"T{index}",
                direction="LONG",
                journal_values={
                    "setup": COHORT.setup,
                    "market_regime": COHORT.market_regime,
                    "audit_status": "PASS",
                },
            )
            await create_model_trade(
                session,
                trade_id=idea.trade_id,
                sample_type=SampleType.OOS if index < count // 2 else SampleType.FORWARD,
                calibration_eligible=True,
                values={
                    **COHORT.model_values(),
                    "model_entry_time": signal_time,
                    "model_entry": 100.0,
                    "final_exit_time": signal_time + timedelta(hours=1),
                    "final_exit": 105.0,
                    "tp_before_sl_1_0": 1,
                    "win_1_0": 1,
                    "result_r": 1.0,
                    "gross_pl_rub": 1_000.0,
                    "net_pl_rub": 990.0,
                },
            )


def test_wilson_interval_is_two_sided_and_not_a_point_estimate() -> None:
    interval = wilson_interval(18, 20)
    assert interval.estimate == 0.9
    assert interval.lower < interval.estimate < interval.upper
    assert interval.width == pytest.approx(interval.upper - interval.lower)


@pytest.mark.asyncio
async def test_calibration_is_not_configured_and_never_publishes_probability() -> None:
    engine, factory = await database()
    try:
        result = await CalibrationService(factory).assess(
            strategy_version="intraday_v2_4",
            cohort=COHORT,
            as_of=NOW,
        )
        assert result.calibration_status is CalibrationStatus.UNCALIBRATED
        assert result.statistical_admission is StatisticalAdmissionStatus.NOT_CONFIGURED
        assert result.probability_status is ProbabilityStatus.NOT_RELIABLY_CALIBRATED
        assert result.reliable_probability is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_calibration_requires_exact_cohort_oos_forward_wilson_and_stability() -> None:
    engine, factory = await database()
    try:
        await seed_calibration_rows(factory)
        async with factory() as session, session.begin():
            await create_admission_policy(
                session,
                AdmissionPolicyInput(
                    strategy_version="intraday_v2_4",
                    configuration_version="policy-1",
                    min_oos_trades=10,
                    min_forward_trades=10,
                    min_total_comparable_trades=20,
                    max_confidence_interval_width=0.20,
                    degradation_min_recent_trades=5,
                    degradation_min_history_trades=5,
                    degradation_expectancy_drop_r=0.5,
                    degradation_win_rate_drop=0.2,
                    configured_at=NOW,
                    effective_from=NOW,
                ),
            )
        result = await CalibrationService(factory).assess(
            strategy_version="intraday_v2_4",
            cohort=COHORT,
            as_of=NOW + timedelta(minutes=1),
        )
        assert result.calibration_status is CalibrationStatus.CALIBRATED
        assert result.statistical_admission is StatisticalAdmissionStatus.PASS
        assert result.n_oos == 10
        assert result.n_forward == 10
        assert result.interval is not None and result.interval.lower >= 0.70
        assert result.reliable_probability == 1.0

        mismatched = await CalibrationService(factory).assess(
            strategy_version="intraday_v2_4",
            cohort=replace(COHORT, volatility="HIGH_VOL"),
            as_of=NOW + timedelta(minutes=1),
        )
        assert mismatched.calibration_status is CalibrationStatus.PRELIMINARY
        assert mismatched.n_total == 0
        assert mismatched.reliable_probability is None
    finally:
        await engine.dispose()


def test_reliability_brier_ece_use_only_calibration_eligible_rows() -> None:
    eligible = observation(1)
    excluded = replace(observation(2, win=0, result_r=-1), calibration_eligible=False)
    average_brier, ece, buckets = reliability_metrics([eligible, excluded])
    assert average_brier == pytest.approx(0.04)
    assert ece == pytest.approx(0.2)
    assert sum(bucket.count for bucket in buckets) == 1


def test_rolling_setup_regime_and_degradation_are_explicit() -> None:
    stable_history = [observation(index) for index in range(30)]
    deteriorating = stable_history + [
        observation(index + 30, win=0, result_r=-1.0) for index in range(10)
    ]
    rolling = rolling_statistics(deteriorating)
    assert rolling["LAST_20"].n_trades == 20
    assert rolling["LAST_50"].n_trades == 40
    assert rolling["ALL_HISTORY"].n_trades == 40
    degradation = ModelDegradationDetector().assess(
        deteriorating,
        DegradationPolicy(10, 20, 0.5, 0.2),
    )
    assert degradation.status is DegradationStatus.MODEL_CONFIDENCE_REDUCED
    assert {"EXPECTANCY_DROP", "WIN_RATE_DROP"}.issubset(degradation.reasons)

    setups = setup_statistics(deteriorating, minimum_sample=20)
    assert setups[0].status is SetupLifecycleStatus.ACTIVE
    unknown_regime = regime_statistics(
        [replace(observation(1), volatility=None)],
        minimum_sample=1,
    )
    assert unknown_regime[0].status is RegimePerformanceStatus.REGIME_PERFORMANCE_UNKNOWN


@pytest.mark.asyncio
async def test_model_and_actual_statistics_are_separate_with_explicit_costs() -> None:
    engine, factory = await database()
    try:
        async with factory() as session, session.begin():
            idea, _ = await create_idea_journal(
                session,
                signal_datetime=NOW,
                ticker="SBER",
                direction="LONG",
                journal_values={
                    "setup": COHORT.setup,
                    "market_regime": COHORT.market_regime,
                    "initial_stop": 95.0,
                    "tp1": 110.0,
                    "audit_status": "PASS",
                },
            )
            await create_model_trade(
                session,
                trade_id=idea.trade_id,
                calibration_eligible=True,
                values={
                    **COHORT.model_values(),
                    "model_entry_time": NOW,
                    "model_entry": 99.0,
                    "final_exit_time": NOW + timedelta(hours=1),
                    "final_exit": 109.0,
                    "win_1_0": 1,
                    "tp_before_sl_1_0": 1,
                    "result_r": 2.0,
                    "gross_pl_rub": 200.0,
                    "net_pl_rub": 190.0,
                },
            )
        actual_service = ActualTradeService(factory)
        actual = await actual_service.confirm_entry(
            ActualTradeConfirmation(
                trade_id=idea.trade_id,
                telegram_id=101,
                confirmation_key="entry-stats",
                entry_time=NOW,
                entry_price=100.0,
                position_shares=10.0,
                commission_rub=1.0,
            )
        )
        await actual_service.record_action(
            ActualActionConfirmation(
                actual_trade_id=actual.actual.actual_trade_id,
                telegram_id=101,
                confirmation_key="close-stats",
                action=ActualTradeAction.FULL_CLOSE,
                event_time=NOW + timedelta(hours=1),
                current_price=110.0,
                commission_rub=1.0,
            )
        )
        service = V24StatisticsService(factory)
        separated = await service.separate_global_statistics(
            strategy_version="intraday_v2_4"
        )
        assert separated["MODEL"].n_trades == 1
        assert separated["MODEL"].net_pl_rub == 190.0
        assert separated["ACTUAL"].n_trades == 1
        assert separated["ACTUAL"].net_pl_rub == 98.0
        assert separated["ACTUAL"].average_r == pytest.approx(1.96)
    finally:
        await engine.dispose()


def test_statistical_admission_migration_is_append_only(tmp_path: Path) -> None:
    database_path = tmp_path / "statistical-admission.db"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    command.upgrade(migration_config(database_url), "head")
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO statistical_admission_settings (
                strategy_version, configuration_version, min_oos_trades,
                min_forward_trades, max_confidence_interval_width,
                configured_at, effective_from
            ) VALUES (
                'intraday_v2_4', 'policy-1', 20, 20, 0.20,
                '2026-09-01 12:00:00', '2026-09-01 12:00:00'
            )
            """
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE statistical_admission_settings SET min_oos_trades = 1"
            )
