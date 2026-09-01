from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RiskBudgetSetting
from app.v24_domain import (
    CalculationReliability,
    CostConfigurationStatus,
    JournalDirection,
    RiskBudgetStatus,
)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _valid_positive(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and value > 0


def _valid_nonnegative(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and value >= 0


@dataclass(frozen=True, slots=True)
class CostModelConfig:
    broker_commission_pct: float | None = None
    exchange_fee_pct: float | None = None
    entry_slippage_bps: float | None = None
    exit_slippage_bps: float | None = None
    stop_slippage_bps: float | None = None
    short_carry_pct_per_day: float | None = None

    def status_for(self, direction: JournalDirection) -> CostConfigurationStatus:
        required = [
            self.broker_commission_pct,
            self.exchange_fee_pct,
            self.entry_slippage_bps,
            self.exit_slippage_bps,
            self.stop_slippage_bps,
        ]
        if direction is JournalDirection.SHORT:
            required.append(self.short_carry_pct_per_day)
        known = sum(value is not None for value in required)
        if known == 0:
            return CostConfigurationStatus.NOT_CONFIGURED
        if known < len(required):
            return CostConfigurationStatus.PARTIAL
        return CostConfigurationStatus.CONFIGURED


@dataclass(frozen=True, slots=True)
class CostEstimate:
    status: CostConfigurationStatus
    entry_costs_rub: float | None
    exit_costs_rub: float | None
    stop_slippage_rub: float | None
    short_carry_rub: float | None
    total_expected_costs_rub: float | None
    known_costs_rub: float


class CostModel:
    def __init__(self, config: CostModelConfig) -> None:
        for value in (
            config.broker_commission_pct,
            config.exchange_fee_pct,
            config.entry_slippage_bps,
            config.exit_slippage_bps,
            config.stop_slippage_bps,
            config.short_carry_pct_per_day,
        ):
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ValueError("Cost model inputs must be finite and non-negative")
        self.config = config

    def estimate(
        self,
        *,
        direction: JournalDirection,
        position_amount_rub: float,
        holding_days: float | None = None,
    ) -> CostEstimate:
        if not _valid_positive(position_amount_rub):
            raise ValueError("position_amount_rub must be positive")
        if holding_days is not None and (not math.isfinite(holding_days) or holding_days < 0):
            raise ValueError("holding_days must be finite and non-negative")
        config = self.config
        fee_pct = (
            config.broker_commission_pct + config.exchange_fee_pct
            if config.broker_commission_pct is not None and config.exchange_fee_pct is not None
            else None
        )
        entry_costs = (
            position_amount_rub * (fee_pct / 100 + config.entry_slippage_bps / 10_000)
            if fee_pct is not None and config.entry_slippage_bps is not None
            else None
        )
        exit_costs = (
            position_amount_rub * (fee_pct / 100 + config.exit_slippage_bps / 10_000)
            if fee_pct is not None and config.exit_slippage_bps is not None
            else None
        )
        stop_slippage = (
            position_amount_rub * config.stop_slippage_bps / 10_000
            if config.stop_slippage_bps is not None
            else None
        )
        short_carry = 0.0
        if direction is JournalDirection.SHORT:
            short_carry = (
                position_amount_rub * config.short_carry_pct_per_day / 100 * holding_days
                if config.short_carry_pct_per_day is not None and holding_days is not None
                else None
            )
        components = (entry_costs, exit_costs, short_carry)
        known_costs = sum(value for value in components if value is not None)
        status = config.status_for(direction)
        fully_estimable = status is CostConfigurationStatus.CONFIGURED and all(
            value is not None for value in components
        )
        return CostEstimate(
            status=status,
            entry_costs_rub=entry_costs,
            exit_costs_rub=exit_costs,
            stop_slippage_rub=stop_slippage,
            short_carry_rub=short_carry,
            total_expected_costs_rub=known_costs if fully_estimable else None,
            known_costs_rub=known_costs,
        )


@dataclass(frozen=True, slots=True)
class PositionRiskInput:
    direction: JournalDirection
    capital_rub: float
    position_amount_rub: float
    entry: float
    stop: float
    cost_estimate: CostEstimate


@dataclass(frozen=True, slots=True)
class PositionRiskAssessment:
    position_amount_rub: float
    distance_to_stop_pct: float
    gross_loss_to_stop_rub: float
    estimated_costs_rub: float | None
    stress_slippage_rub: float | None
    net_stress_loss_rub: float | None
    net_stress_loss_pct_capital: float | None
    reliability: CalculationReliability


def assess_position_risk(inputs: PositionRiskInput) -> PositionRiskAssessment:
    if not all(
        _valid_positive(value)
        for value in (inputs.capital_rub, inputs.position_amount_rub, inputs.entry, inputs.stop)
    ):
        raise ValueError("Capital, position, entry and stop must be positive")
    if inputs.direction is JournalDirection.LONG and inputs.stop >= inputs.entry:
        raise ValueError("LONG stop must be below entry")
    if inputs.direction is JournalDirection.SHORT and inputs.stop <= inputs.entry:
        raise ValueError("SHORT stop must be above entry")
    distance_pct = abs(inputs.entry - inputs.stop) / inputs.entry
    gross_loss = inputs.position_amount_rub * distance_pct
    costs = inputs.cost_estimate.total_expected_costs_rub
    stress = inputs.cost_estimate.stop_slippage_rub
    net_loss = gross_loss + costs + stress if costs is not None and stress is not None else None
    return PositionRiskAssessment(
        position_amount_rub=inputs.position_amount_rub,
        distance_to_stop_pct=distance_pct * 100,
        gross_loss_to_stop_rub=gross_loss,
        estimated_costs_rub=costs,
        stress_slippage_rub=stress,
        net_stress_loss_rub=net_loss,
        net_stress_loss_pct_capital=(
            net_loss / inputs.capital_rub * 100 if net_loss is not None else None
        ),
        reliability=(
            CalculationReliability.RELIABLE
            if net_loss is not None
            else CalculationReliability.NOT_RELIABLY_CALCULABLE
        ),
    )


@dataclass(frozen=True, slots=True)
class RiskBudgetPolicy:
    scope: str
    configuration_version: str
    working_capital_rub: float | None
    max_risk_per_trade_pct: float | None
    max_daily_loss_pct: float | None
    max_portfolio_heat_pct: float | None
    max_sector_heat_pct: float | None
    max_correlated_factor_heat_pct: float | None
    available_capital_pct: float | None
    configured_at: datetime
    effective_from: datetime

    @property
    def status(self) -> RiskBudgetStatus:
        required = (
            self.working_capital_rub,
            self.max_risk_per_trade_pct,
            self.max_daily_loss_pct,
            self.max_portfolio_heat_pct,
            self.max_sector_heat_pct,
            self.max_correlated_factor_heat_pct,
            self.available_capital_pct,
        )
        return (
            RiskBudgetStatus.CONFIGURED
            if all(_valid_positive(value) for value in required)
            else RiskBudgetStatus.NOT_CONFIGURED
        )


class RiskBudgetRepository:
    async def create_policy(
        self,
        session: AsyncSession,
        policy: RiskBudgetPolicy,
        *,
        created_by_telegram_id: int | None = None,
        notes: str | None = None,
    ) -> RiskBudgetSetting:
        if not policy.scope.strip() or not policy.configuration_version.strip():
            raise ValueError("Risk policy scope and version are required")
        values = (
            policy.working_capital_rub,
            policy.max_risk_per_trade_pct,
            policy.max_daily_loss_pct,
            policy.max_portfolio_heat_pct,
            policy.max_sector_heat_pct,
            policy.max_correlated_factor_heat_pct,
            policy.available_capital_pct,
        )
        if any(value is not None and (not math.isfinite(value) or value <= 0) for value in values):
            raise ValueError("Configured risk limits must be finite and positive")
        record = RiskBudgetSetting(
            scope=policy.scope.strip().upper(),
            configuration_version=policy.configuration_version.strip(),
            working_capital_rub=policy.working_capital_rub,
            max_risk_per_trade_pct=policy.max_risk_per_trade_pct,
            max_daily_loss_pct=policy.max_daily_loss_pct,
            max_portfolio_heat_pct=policy.max_portfolio_heat_pct,
            max_sector_heat_pct=policy.max_sector_heat_pct,
            max_correlated_factor_heat_pct=policy.max_correlated_factor_heat_pct,
            available_capital_pct=policy.available_capital_pct,
            configured_at=_aware_utc(policy.configured_at),
            effective_from=_aware_utc(policy.effective_from),
            created_by_telegram_id=created_by_telegram_id,
            notes=notes,
        )
        session.add(record)
        await session.flush()
        return record

    async def effective_policy(
        self,
        session: AsyncSession,
        *,
        scope: str = "GLOBAL",
        as_of: datetime | None = None,
    ) -> RiskBudgetPolicy | None:
        effective_at = _aware_utc(as_of or datetime.now(UTC))
        record = await session.scalar(
            select(RiskBudgetSetting)
            .where(
                RiskBudgetSetting.scope == scope.strip().upper(),
                RiskBudgetSetting.effective_from <= effective_at,
            )
            .order_by(RiskBudgetSetting.effective_from.desc(), RiskBudgetSetting.id.desc())
            .limit(1)
        )
        if record is None:
            return None
        return RiskBudgetPolicy(
            scope=record.scope,
            configuration_version=record.configuration_version,
            working_capital_rub=record.working_capital_rub,
            max_risk_per_trade_pct=record.max_risk_per_trade_pct,
            max_daily_loss_pct=record.max_daily_loss_pct,
            max_portfolio_heat_pct=record.max_portfolio_heat_pct,
            max_sector_heat_pct=record.max_sector_heat_pct,
            max_correlated_factor_heat_pct=record.max_correlated_factor_heat_pct,
            available_capital_pct=record.available_capital_pct,
            configured_at=record.configured_at,
            effective_from=record.effective_from,
        )


@dataclass(frozen=True, slots=True)
class PortfolioRiskState:
    daily_loss_rub: float
    portfolio_heat_rub: float
    sector_heat_rub: float
    factor_heat_rub: Mapping[str, float]
    allocated_capital_rub: float


@dataclass(frozen=True, slots=True)
class RiskBudgetAssessment:
    status: RiskBudgetStatus
    configuration_version: str | None
    risk_cap_rub: float | None
    max_loss_budget_rub: float | None
    available_capital_cap_rub: float | None
    portfolio_cap_rub: float | None
    correlation_cap_rub: float | None
    full_risk_pass: bool
    recommended_position_status: CalculationReliability
    reasons: tuple[str, ...]


class RiskEngineV24:
    def evaluate_budget(
        self,
        *,
        policy: RiskBudgetPolicy | None,
        position_risk: PositionRiskAssessment,
        portfolio: PortfolioRiskState,
        shared_factors: tuple[str, ...],
    ) -> RiskBudgetAssessment:
        if policy is None or policy.status is RiskBudgetStatus.NOT_CONFIGURED:
            return RiskBudgetAssessment(
                status=RiskBudgetStatus.NOT_CONFIGURED,
                configuration_version=policy.configuration_version if policy else None,
                risk_cap_rub=None,
                max_loss_budget_rub=None,
                available_capital_cap_rub=None,
                portfolio_cap_rub=None,
                correlation_cap_rub=None,
                full_risk_pass=False,
                recommended_position_status=CalculationReliability.PROVISIONAL,
                reasons=("RISK_BUDGET_NOT_CONFIGURED",),
            )
        capital = policy.working_capital_rub
        assert capital is not None
        limits = (
            policy.max_risk_per_trade_pct,
            policy.max_daily_loss_pct,
            policy.max_portfolio_heat_pct,
            policy.max_sector_heat_pct,
            policy.max_correlated_factor_heat_pct,
            policy.available_capital_pct,
        )
        assert all(value is not None for value in limits)
        (
            max_trade_pct,
            max_daily_pct,
            max_portfolio_pct,
            max_sector_pct,
            max_factor_pct,
            available_pct,
        ) = limits
        assert max_trade_pct is not None
        assert max_daily_pct is not None
        assert max_portfolio_pct is not None
        assert max_sector_pct is not None
        assert max_factor_pct is not None
        assert available_pct is not None
        for value in (
            portfolio.daily_loss_rub,
            portfolio.portfolio_heat_rub,
            portfolio.sector_heat_rub,
            portfolio.allocated_capital_rub,
            *portfolio.factor_heat_rub.values(),
        ):
            if not math.isfinite(value) or value < 0:
                raise ValueError("Portfolio risk state values must be finite and non-negative")

        per_trade_loss = capital * max_trade_pct / 100
        daily_remaining = max(0.0, capital * max_daily_pct / 100 - portfolio.daily_loss_rub)
        portfolio_remaining = max(
            0.0, capital * max_portfolio_pct / 100 - portfolio.portfolio_heat_rub
        )
        sector_remaining = max(0.0, capital * max_sector_pct / 100 - portfolio.sector_heat_rub)
        loss_budget = min(per_trade_loss, daily_remaining, portfolio_remaining, sector_remaining)

        missing_factors = [
            factor for factor in shared_factors if factor not in portfolio.factor_heat_rub
        ]
        factor_remaining: float | None = None
        if not missing_factors:
            factor_remaining = min(
                (
                    max(
                        0.0,
                        capital * max_factor_pct / 100 - portfolio.factor_heat_rub[factor],
                    )
                    for factor in shared_factors
                ),
                default=capital * max_factor_pct / 100,
            )
            loss_budget = min(loss_budget, factor_remaining)

        loss_fraction = (
            position_risk.net_stress_loss_rub / position_risk.position_amount_rub
            if position_risk.net_stress_loss_rub is not None
            else None
        )
        risk_cap = loss_budget / loss_fraction if loss_fraction and loss_fraction > 0 else None
        available_cap = max(0.0, capital * available_pct / 100 - portfolio.allocated_capital_rub)
        portfolio_cap = (
            portfolio_remaining / loss_fraction if loss_fraction and loss_fraction > 0 else None
        )
        correlation_cap = (
            factor_remaining / loss_fraction
            if factor_remaining is not None and loss_fraction and loss_fraction > 0
            else None
        )
        reasons: list[str] = []
        if position_risk.net_stress_loss_rub is None:
            reasons.append("NET_STRESS_LOSS_NOT_RELIABLY_CALCULABLE")
        if missing_factors:
            reasons.append("MISSING_FACTOR_EXPOSURE:" + ",".join(sorted(missing_factors)))
        full_pass = bool(
            risk_cap is not None
            and correlation_cap is not None
            and position_risk.position_amount_rub <= min(risk_cap, available_cap)
        )
        if not full_pass and not reasons:
            reasons.append("PROPOSED_POSITION_EXCEEDS_RISK_BUDGET")
        return RiskBudgetAssessment(
            status=RiskBudgetStatus.CONFIGURED,
            configuration_version=policy.configuration_version,
            risk_cap_rub=risk_cap,
            max_loss_budget_rub=loss_budget,
            available_capital_cap_rub=available_cap,
            portfolio_cap_rub=portfolio_cap,
            correlation_cap_rub=correlation_cap,
            full_risk_pass=full_pass,
            recommended_position_status=(
                CalculationReliability.RELIABLE
                if risk_cap is not None and correlation_cap is not None
                else CalculationReliability.NOT_RELIABLY_CALCULABLE
            ),
            reasons=tuple(reasons),
        )


@dataclass(frozen=True, slots=True)
class PositionCapInputs:
    liquidity_cap_rub: float | None
    risk_cap_rub: float | None
    available_capital_cap_rub: float | None
    portfolio_cap_rub: float | None
    correlation_cap_rub: float | None
    price: float | None = None
    lot_size: int | None = None


@dataclass(frozen=True, slots=True)
class PositionRecommendation:
    status: CalculationReliability
    recommended_position_rub: float | None
    units: int | None
    lots: int | None
    limiting_caps: tuple[str, ...]
    missing_caps: tuple[str, ...]


def recommend_position(inputs: PositionCapInputs) -> PositionRecommendation:
    caps = {
        "LIQUIDITY_CAP": inputs.liquidity_cap_rub,
        "RISK_CAP": inputs.risk_cap_rub,
        "AVAILABLE_CAPITAL_CAP": inputs.available_capital_cap_rub,
        "PORTFOLIO_CAP": inputs.portfolio_cap_rub,
        "CORRELATION_CAP": inputs.correlation_cap_rub,
    }
    missing = tuple(name for name, value in caps.items() if not _valid_nonnegative(value))
    if missing:
        return PositionRecommendation(
            status=CalculationReliability.NOT_RELIABLY_CALCULABLE,
            recommended_position_rub=None,
            units=None,
            lots=None,
            limiting_caps=(),
            missing_caps=missing,
        )
    minimum = min(value for value in caps.values() if value is not None)
    limiting = tuple(name for name, value in caps.items() if math.isclose(value or 0, minimum))
    units: int | None = None
    lots: int | None = None
    exact_position = minimum
    if inputs.price is not None or inputs.lot_size is not None:
        if not _valid_positive(inputs.price) or inputs.lot_size is None or inputs.lot_size <= 0:
            return PositionRecommendation(
                status=CalculationReliability.NOT_RELIABLY_CALCULABLE,
                recommended_position_rub=None,
                units=None,
                lots=None,
                limiting_caps=limiting,
                missing_caps=("PRICE_OR_LOT_SIZE",),
            )
        lots = int(minimum // (inputs.price * inputs.lot_size))
        units = lots * inputs.lot_size
        exact_position = units * inputs.price
    return PositionRecommendation(
        status=CalculationReliability.RELIABLE,
        recommended_position_rub=exact_position,
        units=units,
        lots=lots,
        limiting_caps=limiting,
        missing_caps=(),
    )
