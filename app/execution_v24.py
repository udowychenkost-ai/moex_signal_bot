from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.journal import append_trade_event
from app.risk_v24 import CostEstimate
from app.v24_domain import (
    CalculationReliability,
    ExecutionAction,
    FinalClassification,
    JournalDirection,
    PathToTargetStatus,
    TradeEventType,
)


@dataclass(frozen=True, slots=True)
class PriceObstacle:
    kind: str
    price: float
    blocking: bool = False


@dataclass(frozen=True, slots=True)
class PathToTargetInput:
    direction: JournalDirection
    entry: float
    target: float
    vwap: float | None
    support_levels: tuple[float, ...]
    resistance_levels: tuple[float, ...]
    previous_day_high: float | None
    previous_day_low: float | None
    gap_levels: tuple[float, ...]
    volume_nodes: tuple[float, ...] | None
    liquidity_obstacles: tuple[PriceObstacle, ...]
    opposing_structure: bool | None


@dataclass(frozen=True, slots=True)
class PathToTargetAssessment:
    status: PathToTargetStatus
    obstacles: tuple[PriceObstacle, ...]
    unavailable_inputs: tuple[str, ...]
    reasons: tuple[str, ...]


def assess_path_to_target(inputs: PathToTargetInput) -> PathToTargetAssessment:
    if min(inputs.entry, inputs.target) <= 0 or math.isclose(inputs.entry, inputs.target):
        raise ValueError("Entry and target must be positive and different")
    if inputs.direction is JournalDirection.LONG and inputs.target <= inputs.entry:
        raise ValueError("LONG target must be above entry")
    if inputs.direction is JournalDirection.SHORT and inputs.target >= inputs.entry:
        raise ValueError("SHORT target must be below entry")

    def between(price: float) -> bool:
        return (
            inputs.entry < price < inputs.target
            if inputs.direction is JournalDirection.LONG
            else inputs.target < price < inputs.entry
        )

    candidates: list[PriceObstacle] = []
    level_kind = "RESISTANCE" if inputs.direction is JournalDirection.LONG else "SUPPORT"
    directional_levels = (
        inputs.resistance_levels
        if inputs.direction is JournalDirection.LONG
        else inputs.support_levels
    )
    candidates.extend(PriceObstacle(level_kind, value) for value in directional_levels)
    previous_level = (
        inputs.previous_day_high
        if inputs.direction is JournalDirection.LONG
        else inputs.previous_day_low
    )
    if previous_level is not None:
        candidates.append(PriceObstacle("PREVIOUS_DAY_LEVEL", previous_level))
    if inputs.vwap is not None:
        candidates.append(PriceObstacle("VWAP", inputs.vwap))
    candidates.extend(PriceObstacle("GAP_LEVEL", value) for value in inputs.gap_levels)
    if inputs.volume_nodes is not None:
        candidates.extend(PriceObstacle("VOLUME_NODE", value) for value in inputs.volume_nodes)
    candidates.extend(inputs.liquidity_obstacles)
    obstacles = tuple(item for item in candidates if between(item.price))
    unavailable: list[str] = []
    if inputs.volume_nodes is None:
        unavailable.append("VOLUME_NODES")
    if inputs.opposing_structure is None:
        unavailable.append("OPPOSING_STRUCTURE")
    blocking = inputs.opposing_structure is True or any(item.blocking for item in obstacles)
    if blocking:
        status = PathToTargetStatus.BLOCKED
        reasons = ("Blocking structure or liquidity obstacle lies before target",)
    elif unavailable:
        status = PathToTargetStatus.UNKNOWN
        reasons = ("Path cannot be fully verified from available market data",)
    elif not obstacles:
        status = PathToTargetStatus.CLEAN
        reasons = ("No verified obstacle before target",)
    else:
        status = PathToTargetStatus.ACCEPTABLE
        reasons = (f"{len(obstacles)} non-blocking obstacle(s) before target",)
    return PathToTargetAssessment(
        status=status,
        obstacles=obstacles,
        unavailable_inputs=tuple(unavailable),
        reasons=reasons,
    )


@dataclass(frozen=True, slots=True)
class EntryModelConfigV24:
    version: str = "intraday_v2_4_entry_1"
    acceptable_entry_atr: float = 0.25
    no_chase_atr: float = 0.75
    stop_buffer_atr: float = 0.10

    def __post_init__(self) -> None:
        if min(self.acceptable_entry_atr, self.no_chase_atr, self.stop_buffer_atr) <= 0:
            raise ValueError("Entry model ATR multipliers must be positive")
        if self.no_chase_atr <= self.acceptable_entry_atr:
            raise ValueError("no_chase_atr must exceed acceptable_entry_atr")


@dataclass(frozen=True, slots=True)
class EntryPlanV24:
    direction: JournalDirection
    optimal_entry: float | None
    acceptable_entry: float | None
    no_chase_level: float | None
    invalidation: float | None
    initial_stop: float | None
    reliability: CalculationReliability
    reasons: tuple[str, ...]


def build_entry_plan(
    *,
    direction: JournalDirection,
    current_price: float,
    atr: float | None,
    preferred_reference: float | None,
    invalidation: float | None,
    config: EntryModelConfigV24 | None = None,
) -> EntryPlanV24:
    policy = config or EntryModelConfigV24()
    if current_price <= 0:
        raise ValueError("current_price must be positive")
    if atr is None or not math.isfinite(atr) or atr <= 0 or invalidation is None:
        return EntryPlanV24(
            direction=direction,
            optimal_entry=None,
            acceptable_entry=None,
            no_chase_level=None,
            invalidation=invalidation,
            initial_stop=None,
            reliability=CalculationReliability.NOT_RELIABLY_CALCULABLE,
            reasons=("ATR_OR_INVALIDATION_DATA_NOT_AVAILABLE",),
        )
    reference = (
        preferred_reference
        if preferred_reference is not None and preferred_reference > 0
        else current_price
    )
    if direction is JournalDirection.LONG:
        if invalidation >= reference:
            return EntryPlanV24(
                direction,
                None,
                None,
                None,
                invalidation,
                None,
                CalculationReliability.NOT_RELIABLY_CALCULABLE,
                ("LONG_INVALIDATION_NOT_BELOW_ENTRY",),
            )
        acceptable = reference + policy.acceptable_entry_atr * atr
        no_chase = reference + policy.no_chase_atr * atr
        stop = invalidation - policy.stop_buffer_atr * atr
    else:
        if invalidation <= reference:
            return EntryPlanV24(
                direction,
                None,
                None,
                None,
                invalidation,
                None,
                CalculationReliability.NOT_RELIABLY_CALCULABLE,
                ("SHORT_INVALIDATION_NOT_ABOVE_ENTRY",),
            )
        acceptable = reference - policy.acceptable_entry_atr * atr
        no_chase = reference - policy.no_chase_atr * atr
        stop = invalidation + policy.stop_buffer_atr * atr
    return EntryPlanV24(
        direction=direction,
        optimal_entry=reference,
        acceptable_entry=acceptable,
        no_chase_level=no_chase,
        invalidation=invalidation,
        initial_stop=stop,
        reliability=CalculationReliability.RELIABLE,
        reasons=(),
    )


@dataclass(frozen=True, slots=True)
class ExecutionAssessmentV24:
    assessed_at: datetime
    current_price: float
    entry: float | None
    stop: float | None
    target: float | None
    gross_risk_per_share: float | None
    gross_reward_per_share: float | None
    gross_rr: float | None
    net_rr: float | None
    estimated_costs_rub: float | None
    remaining_potential_pct: float | None
    execution_quality: float | None
    path_status: PathToTargetStatus
    classification: FinalClassification
    action: ExecutionAction
    reasons: tuple[str, ...]


def reassess_execution(
    *,
    plan: EntryPlanV24,
    current_price: float,
    target: float | None,
    path: PathToTargetAssessment,
    cost_estimate: CostEstimate,
    position_amount_rub: float | None,
    assessed_at: datetime,
) -> ExecutionAssessmentV24:
    reasons: list[str] = []
    if plan.reliability is not CalculationReliability.RELIABLE or plan.initial_stop is None:
        return ExecutionAssessmentV24(
            assessed_at,
            current_price,
            None,
            None,
            target,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            path.status,
            FinalClassification.DATA_INSUFFICIENT,
            ExecutionAction.WAIT,
            plan.reasons,
        )
    if current_price <= 0 or target is None or target <= 0:
        reasons.append("CURRENT_PRICE_OR_TARGET_INVALID")
    is_long = plan.direction is JournalDirection.LONG
    invalidated = (
        current_price <= plan.invalidation
        if is_long and plan.invalidation is not None
        else current_price >= plan.invalidation
        if plan.invalidation is not None
        else True
    )
    chased = (
        current_price > plan.no_chase_level
        if is_long and plan.no_chase_level is not None
        else current_price < plan.no_chase_level
        if plan.no_chase_level is not None
        else True
    )
    if invalidated:
        reasons.append("SETUP_INVALIDATED")
    if chased:
        reasons.append("NO_CHASE_LEVEL_BREACHED")
    stop = plan.initial_stop
    risk = abs(current_price - stop)
    reward = abs(target - current_price) if target is not None else None
    directional_target = target is not None and (
        target > current_price if is_long else target < current_price
    )
    if not directional_target:
        reasons.append("NO_REMAINING_DIRECTIONAL_TARGET")
    gross_rr = reward / risk if reward is not None and risk > 0 and directional_target else None
    costs = cost_estimate.total_expected_costs_rub
    net_rr: float | None = None
    if costs is not None and position_amount_rub is not None and position_amount_rub > 0:
        cost_price_equivalent = current_price * costs / position_amount_rub
        net_reward = max(0.0, (reward or 0) - cost_price_equivalent)
        net_risk = risk + cost_price_equivalent
        net_rr = net_reward / net_risk if net_risk > 0 and directional_target else None
    else:
        reasons.append("NET_RR_NOT_RELIABLY_CALCULABLE")
    remaining = (
        reward / current_price * 100
        if reward is not None and current_price > 0 and directional_target
        else None
    )
    distance_from_optimal = (
        abs(current_price - plan.optimal_entry) / risk
        if plan.optimal_entry is not None and risk > 0
        else None
    )
    quality = (
        max(0.0, min(100.0, 100 - distance_from_optimal * 50))
        if distance_from_optimal is not None
        else None
    )
    if invalidated or not directional_target or path.status is PathToTargetStatus.BLOCKED:
        classification = FinalClassification.REJECTED
        action = ExecutionAction.NO_TRADE
    elif chased:
        classification = FinalClassification.SIGNAL_VALID_EXECUTION_INVALID
        action = ExecutionAction.NO_CHASE
    elif path.status is PathToTargetStatus.UNKNOWN or net_rr is None:
        classification = FinalClassification.WATCH_ONLY
        action = ExecutionAction.WAIT
    else:
        classification = FinalClassification.TRADEABLE
        action = ExecutionAction.ENTER
    reasons.extend(path.reasons)
    return ExecutionAssessmentV24(
        assessed_at=assessed_at,
        current_price=current_price,
        entry=current_price,
        stop=stop,
        target=target,
        gross_risk_per_share=risk,
        gross_reward_per_share=reward,
        gross_rr=gross_rr,
        net_rr=net_rr,
        estimated_costs_rub=costs,
        remaining_potential_pct=remaining,
        execution_quality=quality,
        path_status=path.status,
        classification=classification,
        action=action,
        reasons=tuple(dict.fromkeys(reasons)),
    )


async def append_execution_assessment(
    session: AsyncSession,
    *,
    trade_id: str,
    assessment: ExecutionAssessmentV24,
    model_trade_id: str | None = None,
    actual_trade_id: str | None = None,
) -> None:
    """Append reassessment evidence without mutating the initial decision snapshot."""
    await append_trade_event(
        session,
        trade_id=trade_id,
        model_trade_id=model_trade_id,
        actual_trade_id=actual_trade_id,
        event_datetime=assessment.assessed_at,
        event_type=TradeEventType.OTHER,
        values={
            "current_price": assessment.current_price,
            "reason": "EXECUTION_REASSESSMENT",
            "notes": json.dumps(asdict(assessment), ensure_ascii=False, default=str),
        },
    )
