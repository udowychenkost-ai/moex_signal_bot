from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from app.v24_domain import (
    DataSLAResult,
    JournalDirection,
    PositionAdvisoryAction,
    StopManagementState,
    ThesisStatus,
)


@dataclass(frozen=True, slots=True)
class OpenPositionMark:
    current_price: float
    price_as_of: datetime
    data_sla_result: DataSLAResult
    volume: float | None = None
    vwap: float | None = None
    structure: str | None = None
    market_regime: str | None = None
    sector: str | None = None
    news: str | None = None


@dataclass(frozen=True, slots=True)
class OpenPositionInputs:
    ticker: str
    trade_id: str
    direction: JournalDirection
    entry: float
    initial_stop: float
    current_stop: float
    tp1: float | None
    tp2: float | None
    position_shares: float | None
    position_rub_at_entry: float | None
    entry_time: datetime
    mark: OpenPositionMark
    execution_quality_now: float | None
    trading_sessions_elapsed: int | None
    atr: float | None = None
    confirmed_higher_low: float | None = None
    confirmed_lower_high: float | None = None
    atr_buffer_multiplier: float | None = None
    mfe_pct: float | None = None
    mae_pct: float | None = None
    momentum_confirmed: bool | None = None
    market_against: bool | None = None
    structure_against: bool | None = None


@dataclass(frozen=True, slots=True)
class OpenPositionAssessment:
    ticker: str
    trade_id: str
    actual_entry: float
    current_price: float
    price_as_of: datetime
    data_sla_result: DataSLAResult
    pnl_rub: float
    pnl_pct: float
    current_r: float
    current_stop: float
    tp1: float | None
    tp2: float | None
    mfe_pct: float | None
    mae_pct: float | None
    time_in_trade_hours: float
    volume: float | None
    vwap: float | None
    structure: str | None
    market_regime: str | None
    sector: str | None
    news: str | None
    execution_quality_now: float | None
    thesis_status: ThesisStatus
    stop_state: StopManagementState
    action: PositionAdvisoryAction
    proposed_stop: float | None
    next_target: float | None
    profit_giveback_pct: float | None
    runner_allowed: bool
    reasons: tuple[str, ...]


class OpenPositionManagerV24:
    """Advisory analysis only; it never appends an execution event or places an order."""

    def assess(self, value: OpenPositionInputs) -> OpenPositionAssessment:
        positive_values = (
            value.entry,
            value.initial_stop,
            value.current_stop,
            value.mark.current_price,
        )
        if any(not math.isfinite(item) or item <= 0 for item in positive_values):
            raise ValueError("Position prices must be finite and positive")
        if value.mark.price_as_of.tzinfo is None or value.entry_time.tzinfo is None:
            raise ValueError("Position timestamps must be timezone-aware")
        if value.direction is JournalDirection.LONG and value.initial_stop >= value.entry:
            raise ValueError("LONG initial stop must be below entry")
        if value.direction is JournalDirection.SHORT and value.initial_stop <= value.entry:
            raise ValueError("SHORT initial stop must be above entry")
        if (value.position_shares is None) == (value.position_rub_at_entry is None):
            raise ValueError("Provide shares or entry amount, but not both")

        units = (
            value.position_shares
            if value.position_shares is not None
            else value.position_rub_at_entry / value.entry
        )
        if units is None or not math.isfinite(units) or units <= 0:
            raise ValueError("Position size must be finite and positive")
        multiplier = 1 if value.direction is JournalDirection.LONG else -1
        movement = multiplier * (value.mark.current_price - value.entry)
        risk_per_share = abs(value.entry - value.initial_stop)
        current_r = movement / risk_per_share
        pnl_rub = movement * units
        entry_amount = units * value.entry
        pnl_pct = pnl_rub / entry_amount * 100
        stopped = (
            value.mark.current_price <= value.current_stop
            if value.direction is JournalDirection.LONG
            else value.mark.current_price >= value.current_stop
        )
        reasons: list[str] = []
        proposed_stop = self._structural_stop(value)
        if value.mark.data_sla_result is not DataSLAResult.PASS:
            action = PositionAdvisoryAction.VERIFY_DATA
            reasons.append("DATA_SLA_NOT_PASS")
        elif stopped:
            action = PositionAdvisoryAction.EXIT
            reasons.append("CURRENT_STOP_BREACHED")
        elif value.trading_sessions_elapsed is not None and value.trading_sessions_elapsed >= 2:
            action = PositionAdvisoryAction.EXIT_REVIEW
            reasons.append("MAX_HOLDING_TWO_TRADING_SESSIONS")
        elif proposed_stop is not None:
            action = PositionAdvisoryAction.MOVE_STOP
            reasons.append("CONFIRMED_STRUCTURE_TRAIL_AVAILABLE")
        elif current_r >= 1.0:
            action = PositionAdvisoryAction.PARTIAL_CLOSE_REVIEW
            reasons.append("PROFIT_CHECKPOINT_REACHED")
        else:
            action = PositionAdvisoryAction.HOLD
            reasons.append("THESIS_REMAINS_WITHIN_INITIAL_PLAN")

        if stopped:
            thesis = ThesisStatus.INVALID
        elif value.market_against is True or value.structure_against is True:
            thesis = ThesisStatus.WEAKENING
        elif current_r >= 1 and value.momentum_confirmed is True:
            thesis = ThesisStatus.STRONG
        else:
            thesis = ThesisStatus.VALID
        stop_state = self._stop_state(current_r, stopped)
        giveback = max(0.0, value.mfe_pct - pnl_pct) if value.mfe_pct is not None else None
        runner_allowed = bool(value.momentum_confirmed is True and current_r >= 1.5)
        tp1_reached = value.tp1 is not None and (
            value.mark.current_price >= value.tp1
            if value.direction is JournalDirection.LONG
            else value.mark.current_price <= value.tp1
        )
        next_target = value.tp2 if tp1_reached else value.tp1
        return OpenPositionAssessment(
            ticker=value.ticker,
            trade_id=value.trade_id,
            actual_entry=value.entry,
            current_price=value.mark.current_price,
            price_as_of=value.mark.price_as_of,
            data_sla_result=value.mark.data_sla_result,
            pnl_rub=pnl_rub,
            pnl_pct=pnl_pct,
            current_r=current_r,
            current_stop=value.current_stop,
            tp1=value.tp1,
            tp2=value.tp2,
            mfe_pct=value.mfe_pct,
            mae_pct=value.mae_pct,
            time_in_trade_hours=max(
                0.0,
                (value.mark.price_as_of - value.entry_time).total_seconds() / 3_600,
            ),
            volume=value.mark.volume,
            vwap=value.mark.vwap,
            structure=value.mark.structure,
            market_regime=value.mark.market_regime,
            sector=value.mark.sector,
            news=value.mark.news,
            execution_quality_now=value.execution_quality_now,
            thesis_status=thesis,
            stop_state=stop_state,
            action=action,
            proposed_stop=proposed_stop,
            next_target=next_target,
            profit_giveback_pct=giveback,
            runner_allowed=runner_allowed,
            reasons=tuple(reasons),
        )

    @staticmethod
    def _stop_state(current_r: float, stopped: bool) -> StopManagementState:
        if stopped:
            return StopManagementState.EXIT
        if current_r >= 2:
            return StopManagementState.INCREASING_PROTECTED_PROFIT
        if current_r >= 1:
            return StopManagementState.PROTECTED_PROFIT
        if current_r >= 0.75:
            return StopManagementState.BREAK_EVEN
        if current_r >= 0.5:
            return StopManagementState.REDUCED_RISK
        return StopManagementState.INITIAL_RISK

    @staticmethod
    def _structural_stop(value: OpenPositionInputs) -> float | None:
        if (
            value.atr is None
            or value.atr_buffer_multiplier is None
            or value.atr <= 0
            or value.atr_buffer_multiplier <= 0
        ):
            return None
        if value.direction is JournalDirection.LONG and value.confirmed_higher_low is not None:
            candidate = value.confirmed_higher_low - value.atr * value.atr_buffer_multiplier
            return candidate if value.current_stop < candidate < value.mark.current_price else None
        if value.direction is JournalDirection.SHORT and value.confirmed_lower_high is not None:
            candidate = value.confirmed_lower_high + value.atr * value.atr_buffer_multiplier
            return candidate if value.mark.current_price < candidate < value.current_stop else None
        return None
