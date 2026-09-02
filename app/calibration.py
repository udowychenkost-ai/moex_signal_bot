from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import IdeaJournal, ModelTradeJournal, StatisticalAdmissionSetting
from app.statistics_v24 import (
    DegradationAssessment,
    DegradationPolicy,
    ModelDegradationDetector,
    TradeObservation,
)
from app.v24_domain import (
    AuditStatus,
    CalibrationStatus,
    DegradationStatus,
    ProbabilityStatus,
    SampleType,
    StatisticalAdmissionStatus,
)

CALIBRATION_TARGET_LOWER_BOUND = 0.70


@dataclass(frozen=True, slots=True)
class CalibrationCohort:
    setup: str
    direction: str
    market_regime: str
    trend: str
    volatility: str
    time_of_day: str
    rr_bucket: str
    liquidity_state: str
    context: str

    def values(self) -> dict[str, str]:
        return {
            "setup": self.setup,
            "direction": self.direction,
            "market_regime": self.market_regime,
            "trend": self.trend,
            "volatility": self.volatility,
            "time_of_day": self.time_of_day,
            "rr_bucket": self.rr_bucket,
            "liquidity_state": self.liquidity_state,
            "context": self.context,
        }

    def is_complete(self) -> bool:
        unavailable = {"", "UNKNOWN", "DATA_NOT_AVAILABLE", "NOT_CONFIGURED"}
        return all(value.strip().upper() not in unavailable for value in self.values().values())

    def key(self) -> str:
        payload = json.dumps(
            self.values(),
            sort_keys=True,
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return "CG-" + hashlib.sha256(payload.encode()).hexdigest()[:56]

    def model_values(self) -> dict[str, str]:
        return {
            "calibration_group": self.key(),
            "calibration_setup": self.setup,
            "calibration_direction": self.direction,
            "calibration_market_regime": self.market_regime,
            "calibration_trend": self.trend,
            "calibration_volatility": self.volatility,
            "calibration_time_of_day": self.time_of_day,
            "calibration_rr_bucket": self.rr_bucket,
            "calibration_liquidity_state": self.liquidity_state,
            "calibration_context": self.context,
        }


@dataclass(frozen=True, slots=True)
class AdmissionPolicyInput:
    strategy_version: str
    configuration_version: str
    min_oos_trades: int
    min_forward_trades: int
    max_confidence_interval_width: float
    configured_at: datetime
    effective_from: datetime
    min_total_comparable_trades: int | None = None
    degradation_min_recent_trades: int | None = None
    degradation_min_history_trades: int | None = None
    degradation_expectancy_drop_r: float | None = None
    degradation_win_rate_drop: float | None = None
    created_by_telegram_id: int | None = None
    notes: str | None = None


@dataclass(frozen=True, slots=True)
class WilsonInterval:
    estimate: float
    lower: float
    upper: float
    width: float


@dataclass(frozen=True, slots=True)
class CalibrationAssessment:
    calibration_status: CalibrationStatus
    statistical_admission: StatisticalAdmissionStatus
    probability_status: ProbabilityStatus
    reliable_probability: float | None
    strategy_version: str
    calibration_group: str
    policy_version: str | None
    n_oos: int
    n_forward: int
    n_total: int
    successes: int
    interval: WilsonInterval | None
    degradation: DegradationAssessment | None
    reasons: tuple[str, ...]


def wilson_interval(successes: int, total: int, *, z: float = 1.959963984540054) -> WilsonInterval:
    if total <= 0 or not 0 <= successes <= total:
        raise ValueError("Wilson interval requires 0 <= successes <= total and total > 0")
    estimate = successes / total
    denominator = 1 + z**2 / total
    centre = (estimate + z**2 / (2 * total)) / denominator
    margin = z * math.sqrt(estimate * (1 - estimate) / total + z**2 / (4 * total**2)) / denominator
    lower = max(0.0, centre - margin)
    upper = min(1.0, centre + margin)
    return WilsonInterval(estimate, lower, upper, upper - lower)


def _validate_policy(value: AdmissionPolicyInput) -> None:
    if not value.strategy_version.strip() or not value.configuration_version.strip():
        raise ValueError("strategy_version and configuration_version are required")
    if value.min_oos_trades <= 0 or value.min_forward_trades <= 0:
        raise ValueError("Minimum OOS and forward samples must be positive")
    if not 0 < value.max_confidence_interval_width <= 1:
        raise ValueError("Maximum confidence interval width must be in (0, 1]")
    if value.min_total_comparable_trades is not None and value.min_total_comparable_trades <= 0:
        raise ValueError("Minimum total comparable sample must be positive")
    if value.configured_at.tzinfo is None or value.effective_from.tzinfo is None:
        raise ValueError("Policy timestamps must be timezone-aware")
    degradation_values = (
        value.degradation_min_recent_trades,
        value.degradation_min_history_trades,
        value.degradation_expectancy_drop_r,
        value.degradation_win_rate_drop,
    )
    if any(item is not None for item in degradation_values) and not all(
        item is not None for item in degradation_values
    ):
        raise ValueError("Configure all degradation thresholds together or leave all unset")
    if value.degradation_min_recent_trades is not None:
        if value.degradation_min_recent_trades <= 0 or value.degradation_min_history_trades <= 0:
            raise ValueError("Degradation sample sizes must be positive")
        if value.degradation_expectancy_drop_r <= 0:
            raise ValueError("Expectancy degradation threshold must be positive")
        if not 0 < value.degradation_win_rate_drop <= 1:
            raise ValueError("Win-rate degradation threshold must be in (0, 1]")


async def create_admission_policy(
    session: AsyncSession,
    value: AdmissionPolicyInput,
) -> StatisticalAdmissionSetting:
    _validate_policy(value)
    setting = StatisticalAdmissionSetting(**asdict(value))
    session.add(setting)
    await session.flush()
    return setting


async def latest_admission_policy(
    session: AsyncSession,
    *,
    strategy_version: str,
    as_of: datetime,
) -> StatisticalAdmissionSetting | None:
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    return await session.scalar(
        select(StatisticalAdmissionSetting)
        .where(
            StatisticalAdmissionSetting.strategy_version == strategy_version,
            StatisticalAdmissionSetting.effective_from <= as_of,
        )
        .order_by(
            StatisticalAdmissionSetting.effective_from.desc(),
            StatisticalAdmissionSetting.id.desc(),
        )
        .limit(1)
    )


class CalibrationService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory
        self.degradation = ModelDegradationDetector()

    async def assess(
        self,
        *,
        strategy_version: str,
        cohort: CalibrationCohort,
        as_of: datetime,
    ) -> CalibrationAssessment:
        group = cohort.key()
        if not cohort.is_complete():
            return self._uncalibrated(
                strategy_version,
                group,
                reasons=("COMPARABLE_COHORT_INCOMPLETE",),
            )
        async with self.session_factory() as session:
            policy = await latest_admission_policy(
                session,
                strategy_version=strategy_version,
                as_of=as_of,
            )
            if policy is None:
                return self._uncalibrated(
                    strategy_version,
                    group,
                    reasons=("STATISTICAL_ADMISSION_NOT_CONFIGURED",),
                )
            rows = (
                await session.execute(
                    select(ModelTradeJournal, IdeaJournal)
                    .join(IdeaJournal, IdeaJournal.trade_id == ModelTradeJournal.trade_id)
                    .where(
                        ModelTradeJournal.strategy_version == strategy_version,
                        ModelTradeJournal.calibration_group == group,
                        ModelTradeJournal.calibration_eligible.is_(True),
                        ModelTradeJournal.ambiguous_execution.is_(False),
                        ModelTradeJournal.tp_before_sl_1_0.in_((0, 1)),
                        ModelTradeJournal.sample_type.in_(
                            (SampleType.OOS.value, SampleType.FORWARD.value)
                        ),
                        IdeaJournal.audit_status == AuditStatus.PASS.value,
                    )
                )
            ).all()

        observations = [self._observation(model, idea) for model, idea in rows]
        oos = [item for item in observations if item.sample_type == SampleType.OOS.value]
        forward = [item for item in observations if item.sample_type == SampleType.FORWARD.value]
        successes = sum(item.tp_before_sl or 0 for item in observations)
        interval = wilson_interval(successes, len(observations)) if observations else None
        degradation = self.degradation.assess(
            observations,
            DegradationPolicy(
                min_recent_trades=policy.degradation_min_recent_trades,
                min_history_trades=policy.degradation_min_history_trades,
                expectancy_drop_r=policy.degradation_expectancy_drop_r,
                win_rate_drop=policy.degradation_win_rate_drop,
            ),
        )
        reasons: list[str] = []
        if len(oos) < policy.min_oos_trades:
            reasons.append("MIN_OOS_TRADES_NOT_MET")
        if len(forward) < policy.min_forward_trades:
            reasons.append("MIN_FORWARD_TRADES_NOT_MET")
        if (
            policy.min_total_comparable_trades is not None
            and len(observations) < policy.min_total_comparable_trades
        ):
            reasons.append("MIN_TOTAL_COMPARABLE_TRADES_NOT_MET")
        if interval is None:
            reasons.append("NO_ELIGIBLE_OUTCOMES")
        elif interval.width > policy.max_confidence_interval_width:
            reasons.append("CONFIDENCE_INTERVAL_TOO_WIDE")
        if degradation.status is not DegradationStatus.STABLE:
            reasons.append(f"DEGRADATION_{degradation.status.value}")
        admission = (
            StatisticalAdmissionStatus.PASS if not reasons else StatisticalAdmissionStatus.FAIL
        )
        if interval is not None and interval.lower < CALIBRATION_TARGET_LOWER_BOUND:
            reasons.append("WILSON_LOWER_BOUND_BELOW_70_PERCENT")
        calibrated = admission is StatisticalAdmissionStatus.PASS and not reasons
        return CalibrationAssessment(
            calibration_status=(
                CalibrationStatus.CALIBRATED if calibrated else CalibrationStatus.PRELIMINARY
            ),
            statistical_admission=admission,
            probability_status=(
                ProbabilityStatus.CALIBRATED
                if calibrated
                else ProbabilityStatus.NOT_RELIABLY_CALIBRATED
            ),
            reliable_probability=interval.estimate if calibrated and interval else None,
            strategy_version=strategy_version,
            calibration_group=group,
            policy_version=policy.configuration_version,
            n_oos=len(oos),
            n_forward=len(forward),
            n_total=len(observations),
            successes=successes,
            interval=interval,
            degradation=degradation,
            reasons=tuple(reasons),
        )

    @staticmethod
    def _observation(model: ModelTradeJournal, idea: IdeaJournal) -> TradeObservation:
        return TradeObservation(
            trade_id=model.trade_id,
            source="MODEL",
            strategy_version=model.strategy_version,
            closed_at=model.final_exit_time or model.model_entry_time or idea.signal_datetime,
            direction=model.calibration_direction or idea.direction,
            setup=model.calibration_setup or idea.setup,
            market_regime=model.calibration_market_regime or idea.market_regime,
            trend=model.calibration_trend,
            volatility=model.calibration_volatility,
            time_of_day=model.calibration_time_of_day,
            rr_bucket=model.calibration_rr_bucket,
            liquidity_state=model.calibration_liquidity_state,
            context=model.calibration_context,
            calibration_group=model.calibration_group,
            calibration_eligible=model.calibration_eligible,
            sample_type=model.sample_type,
            win=model.win_1_0,
            tp_before_sl=model.tp_before_sl_1_0,
            result_r=model.result_r,
            net_pl_rub=model.net_pl_rub,
            stated_probability=model.stated_probability,
        )

    @staticmethod
    def _uncalibrated(
        strategy_version: str,
        group: str,
        *,
        reasons: tuple[str, ...],
    ) -> CalibrationAssessment:
        return CalibrationAssessment(
            calibration_status=CalibrationStatus.UNCALIBRATED,
            statistical_admission=StatisticalAdmissionStatus.NOT_CONFIGURED,
            probability_status=ProbabilityStatus.NOT_RELIABLY_CALIBRATED,
            reliable_probability=None,
            strategy_version=strategy_version,
            calibration_group=group,
            policy_version=None,
            n_oos=0,
            n_forward=0,
            n_total=0,
            successes=0,
            interval=None,
            degradation=None,
            reasons=reasons,
        )
