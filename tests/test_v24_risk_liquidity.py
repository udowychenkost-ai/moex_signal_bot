from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from app.config import Settings
from app.db import create_engine_and_session, init_db
from app.liquidity_v24 import LiquidityModelConfig, LiquidityModelInput, LiquidityModelV24
from app.opportunity import (
    OpportunityCandidate,
    OpportunityCostRanker,
    OpportunityCostWeights,
)
from app.risk_v24 import (
    CostModel,
    CostModelConfig,
    PortfolioRiskState,
    PositionCapInputs,
    PositionRiskInput,
    RiskBudgetPolicy,
    RiskBudgetRepository,
    RiskEngineV24,
    assess_position_risk,
    recommend_position,
)
from app.v24_domain import (
    CalculationReliability,
    ConfigurationStatus,
    CostConfigurationStatus,
    JournalDirection,
    OpportunityRankingMode,
    RiskBudgetStatus,
)

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def migration_config(database_url: str) -> Config:
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    return config


def _configured_costs() -> CostModel:
    return CostModel(
        CostModelConfig(
            broker_commission_pct=0.05,
            exchange_fee_pct=0.01,
            entry_slippage_bps=2,
            exit_slippage_bps=3,
            stop_slippage_bps=10,
            short_carry_pct_per_day=0.02,
        )
    )


def _position_risk(position_amount: float = 1_000_000):
    costs = _configured_costs().estimate(
        direction=JournalDirection.LONG,
        position_amount_rub=position_amount,
        holding_days=1,
    )
    return assess_position_risk(
        PositionRiskInput(
            direction=JournalDirection.LONG,
            capital_rub=50_000_000,
            position_amount_rub=position_amount,
            entry=100,
            stop=95,
            cost_estimate=costs,
        )
    )


def _policy(**overrides: object) -> RiskBudgetPolicy:
    values: dict[str, object] = {
        "scope": "GLOBAL",
        "configuration_version": "risk-policy-1",
        "working_capital_rub": 50_000_000.0,
        "max_risk_per_trade_pct": 0.5,
        "max_daily_loss_pct": 2.0,
        "max_portfolio_heat_pct": 5.0,
        "max_sector_heat_pct": 3.0,
        "max_correlated_factor_heat_pct": 4.0,
        "available_capital_pct": 50.0,
        "configured_at": NOW,
        "effective_from": NOW,
    }
    values.update(overrides)
    return RiskBudgetPolicy(**values)  # type: ignore[arg-type]


def test_cost_model_never_invents_missing_user_costs() -> None:
    unknown = CostModel(CostModelConfig()).estimate(
        direction=JournalDirection.LONG,
        position_amount_rub=1_000_000,
    )
    partial = CostModel(CostModelConfig(broker_commission_pct=0.05)).estimate(
        direction=JournalDirection.LONG,
        position_amount_rub=1_000_000,
    )

    assert unknown.status is CostConfigurationStatus.NOT_CONFIGURED
    assert unknown.total_expected_costs_rub is None
    assert partial.status is CostConfigurationStatus.PARTIAL
    assert partial.total_expected_costs_rub is None


def test_position_risk_includes_costs_and_stress_slippage() -> None:
    assessment = _position_risk()

    assert assessment.distance_to_stop_pct == pytest.approx(5)
    assert assessment.gross_loss_to_stop_rub == pytest.approx(50_000)
    assert assessment.estimated_costs_rub == pytest.approx(1_700)
    assert assessment.stress_slippage_rub == pytest.approx(1_000)
    assert assessment.net_stress_loss_rub == pytest.approx(52_700)
    assert assessment.net_stress_loss_pct_capital == pytest.approx(0.1054)
    assert assessment.reliability is CalculationReliability.RELIABLE


def test_unconfigured_risk_budget_never_full_passes() -> None:
    result = RiskEngineV24().evaluate_budget(
        policy=None,
        position_risk=_position_risk(),
        portfolio=PortfolioRiskState(0, 0, 0, {}, 0),
        shared_factors=("MARKET",),
    )

    assert result.status is RiskBudgetStatus.NOT_CONFIGURED
    assert result.risk_cap_rub is None
    assert result.full_risk_pass is False
    assert result.recommended_position_status is CalculationReliability.PROVISIONAL


def test_configured_risk_budget_uses_deterministic_factor_caps() -> None:
    result = RiskEngineV24().evaluate_budget(
        policy=_policy(),
        position_risk=_position_risk(),
        portfolio=PortfolioRiskState(
            daily_loss_rub=100_000,
            portfolio_heat_rub=500_000,
            sector_heat_rub=200_000,
            factor_heat_rub={"MARKET": 300_000, "SECTOR": 100_000},
            allocated_capital_rub=5_000_000,
        ),
        shared_factors=("MARKET", "SECTOR"),
    )

    assert result.status is RiskBudgetStatus.CONFIGURED
    assert result.risk_cap_rub is not None
    assert result.correlation_cap_rub is not None
    assert result.full_risk_pass is True
    assert result.reasons == ()


def test_missing_factor_exposure_blocks_precise_risk_cap() -> None:
    result = RiskEngineV24().evaluate_budget(
        policy=_policy(),
        position_risk=_position_risk(),
        portfolio=PortfolioRiskState(0, 0, 0, {"MARKET": 0}, 0),
        shared_factors=("MARKET", "SECTOR"),
    )

    assert result.full_risk_pass is False
    assert result.correlation_cap_rub is None
    assert result.recommended_position_status is CalculationReliability.NOT_RELIABLY_CALCULABLE
    assert result.reasons == ("MISSING_FACTOR_EXPOSURE:SECTOR",)


async def test_risk_budget_policy_is_versioned_and_point_in_time(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'risk.db').as_posix()}"
    engine, factory = create_engine_and_session(database_url)
    await init_db(engine)
    repository = RiskBudgetRepository()
    try:
        async with factory() as session, session.begin():
            await repository.create_policy(session, _policy())
            await repository.create_policy(
                session,
                _policy(
                    configuration_version="risk-policy-2",
                    configured_at=NOW + timedelta(days=1),
                    effective_from=NOW + timedelta(days=1),
                    max_risk_per_trade_pct=0.4,
                ),
            )
        async with factory() as session:
            first = await repository.effective_policy(session, as_of=NOW + timedelta(hours=1))
            second = await repository.effective_policy(session, as_of=NOW + timedelta(days=2))

        assert first is not None and first.configuration_version == "risk-policy-1"
        assert second is not None and second.configuration_version == "risk-policy-2"
        assert second.max_risk_per_trade_pct == 0.4
    finally:
        await engine.dispose()


def _liquidity_config() -> LiquidityModelConfig:
    return LiquidityModelConfig(
        adv_participation_rate=0.01,
        session_participation_rate=0.02,
        orderbook_participation_rate=0.10,
        normal_exit_participation_rate=0.02,
        fast_exit_participation_rate=0.01,
        stress_exit_participation_rate=0.005,
        max_expected_slippage_bps=10,
        max_market_impact_bps=15,
    )


def _liquidity_input(**overrides: object) -> LiquidityModelInput:
    values: dict[str, object] = {
        "adv20_rub": 2_000_000_000.0,
        "median_adv_rub": 1_800_000_000.0,
        "current_turnover_rub": 800_000_000.0,
        "expected_remaining_turnover_rub": 1_000_000_000.0,
        "rvol": 1.1,
        "spread_pct": 0.001,
        "number_of_trades": 20_000,
        "orderbook_depth_rub": 50_000_000.0,
        "orderbook_depth_is_full": True,
        "expected_slippage_bps": 4.0,
        "market_impact_bps": 6.0,
        "remaining_session_hours": 4.0,
        "proposed_position_rub": 1_000_000.0,
    }
    values.update(overrides)
    return LiquidityModelInput(**values)  # type: ignore[arg-type]


def test_liquidity_v24_is_not_configured_by_default() -> None:
    assessment = LiquidityModelV24(
        LiquidityModelConfig.from_settings(Settings(_env_file=None))
    ).assess(_liquidity_input())

    assert assessment.config_status is ConfigurationStatus.NOT_CONFIGURED
    assert assessment.liquidity_cap_rub is None
    assert assessment.reliability is CalculationReliability.NOT_RELIABLY_CALCULABLE


def test_public_level1_is_not_treated_as_full_depth() -> None:
    assessment = LiquidityModelV24(_liquidity_config()).assess(
        _liquidity_input(orderbook_depth_rub=None, orderbook_depth_is_full=False)
    )

    assert assessment.liquidity_cap_rub is None
    assert "FULL_ORDERBOOK_DEPTH" in assessment.missing_inputs
    assert assessment.reliability is CalculationReliability.NOT_RELIABLY_CALCULABLE


def test_liquidity_v24_caps_and_exit_times_use_complete_inputs() -> None:
    assessment = LiquidityModelV24(_liquidity_config()).assess(_liquidity_input())

    assert assessment.adv_participation_cap_rub == pytest.approx(18_000_000)
    assert assessment.session_participation_cap_rub == pytest.approx(20_000_000)
    assert assessment.orderbook_slippage_cap_rub == pytest.approx(5_000_000)
    assert assessment.stress_exit_cap_rub == pytest.approx(5_000_000)
    assert assessment.liquidity_cap_rub == pytest.approx(5_000_000)
    assert assessment.time_to_exit_100_minutes == pytest.approx(12)
    assert assessment.reliability is CalculationReliability.RELIABLE


def test_liquidity_policy_breach_returns_zero_tradable_cap() -> None:
    assessment = LiquidityModelV24(_liquidity_config()).assess(
        _liquidity_input(expected_slippage_bps=20)
    )

    assert assessment.liquidity_cap_rub == 0
    assert assessment.reasons == ("EXPECTED_SLIPPAGE_LIMIT_EXCEEDED",)


def test_position_recommendation_requires_every_cap_and_respects_lot() -> None:
    missing = recommend_position(PositionCapInputs(1_000_000, None, 500_000, 700_000, 600_000))
    complete = recommend_position(
        PositionCapInputs(
            liquidity_cap_rub=1_000_000,
            risk_cap_rub=700_000,
            available_capital_cap_rub=800_000,
            portfolio_cap_rub=900_000,
            correlation_cap_rub=600_000,
            price=250,
            lot_size=10,
        )
    )

    assert missing.status is CalculationReliability.NOT_RELIABLY_CALCULABLE
    assert missing.recommended_position_rub is None
    assert complete.status is CalculationReliability.RELIABLE
    assert complete.recommended_position_rub == 600_000
    assert complete.units == 2_400
    assert complete.lots == 240
    assert complete.limiting_caps == ("CORRELATION_CAP",)


def _opportunity(candidate_id: str, **overrides: object) -> OpportunityCandidate:
    values: dict[str, object] = {
        "candidate_id": candidate_id,
        "structural_quality": 80.0,
        "risk_rub": 50_000.0,
        "capital_rub": 1_000_000.0,
        "expected_holding_hours": 8.0,
        "liquidity_score": 0.8,
        "factor_risk_score": 0.2,
        "event_risk_score": 0.1,
        "overnight_risk_score": 0.1,
    }
    values.update(overrides)
    return OpportunityCandidate(**values)  # type: ignore[arg-type]


def _weights() -> OpportunityCostWeights:
    return OpportunityCostWeights(1, 1, 1, 1, 1, 1, 1, 1)


def test_cold_start_opportunity_ranking_has_no_fake_ev() -> None:
    ranked = OpportunityCostRanker(_weights()).rank(
        (_opportunity("A"), _opportunity("B", structural_quality=60))
    )

    assert ranked[0].candidate_id == "A"
    assert all(item.mode is OpportunityRankingMode.COLD_START_COMPOSITE for item in ranked)
    assert all(item.net_ev_rub is None for item in ranked)


def test_ev_per_capital_time_requires_every_candidate_to_be_calibrated() -> None:
    calibrated = (
        _opportunity(
            "A",
            statistically_calibrated=True,
            probability_tp_before_sl=0.7,
            reward_r=2.0,
            loss_r=1.0,
        ),
        _opportunity(
            "B",
            statistically_calibrated=True,
            probability_tp_before_sl=0.6,
            reward_r=2.0,
            loss_r=1.0,
        ),
    )
    ranked = OpportunityCostRanker(_weights()).rank(calibrated)

    assert ranked[0].candidate_id == "A"
    assert all(item.mode is OpportunityRankingMode.CALIBRATED_EV for item in ranked)
    assert all(item.net_ev_rub is not None for item in ranked)


def test_risk_budget_migration_is_append_only(tmp_path: Path) -> None:
    database_path = tmp_path / "risk-policy.db"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    command.upgrade(migration_config(database_url), "head")
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO risk_budget_settings (
                scope, configuration_version, configured_at, effective_from
            ) VALUES (
                'GLOBAL', 'unconfigured-1',
                '2026-09-01 12:00:00', '2026-09-01 12:00:00'
            )
            """
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE risk_budget_settings SET max_risk_per_trade_pct = 1 WHERE id = 1"
            )
