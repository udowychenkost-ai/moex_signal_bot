from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.calibration import CalibrationCohort, CalibrationService, latest_admission_policy
from app.config import Settings
from app.data_sla import DataSLAConfig
from app.journal_health import JournalHealthService, JournalStorageHealth
from app.kill_switch import KillSwitchService, KillSwitchStatus
from app.liquidity_v24 import LiquidityModelConfig
from app.models import (
    ActualTradeJournal,
    DecisionSnapshotV24,
    IdeaJournal,
    JobRunState,
    ModelTradeJournal,
)
from app.provider_health import GeminiHealthMonitor
from app.risk_v24 import CostModelConfig, RiskBudgetRepository
from app.v24_domain import (
    CalibrationStatus,
    ConfigurationStatus,
    JournalDirection,
    RiskBudgetStatus,
    StatisticalAdmissionStatus,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class V24RuntimeStatus:
    enabled: bool
    journal: JournalStorageHealth
    data_sla: ConfigurationStatus
    risk_budget: RiskBudgetStatus
    risk_policy_version: str | None
    statistical_admission: StatisticalAdmissionStatus
    admission_policy_version: str | None
    calibration: CalibrationStatus
    kill_switch: KillSwitchStatus
    gemini: str
    model_trade_count: int
    actual_trade_count: int
    ambiguous_execution_count: int
    uncalibrated_signal_count: int
    data_availability_failure_count: int
    journal_write_count: int
    journal_error_count: int
    data_sla_distribution: dict[str, int]
    microstructure_distribution: dict[str, int]
    audit_failure_reasons: dict[str, int]
    classification_distribution: dict[str, int]
    setup_distribution: dict[str, int]
    regime_distribution: dict[str, int]
    legacy_enabled: bool = True
    shadow_enabled: bool = False
    production_notification_enabled: bool = False
    final_audit_ready: bool = False
    external_context_ready: bool = False
    liquidity_configured: bool = False
    cost_configured: bool = False
    latest_scan_at: datetime | None = None
    latest_scan_status: str = "NEVER"
    latest_scan_candidates: int = 0
    latest_scan_errors: int = 0


class V24ObservabilityService:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        gemini_health: GeminiHealthMonitor | None = None,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.gemini_health = gemini_health
        self.journal_health = JournalHealthService(session_factory)
        self.kill_switch = KillSwitchService(session_factory)
        self.calibration = CalibrationService(session_factory)

    async def status(self, *, now: datetime | None = None) -> V24RuntimeStatus:
        checked_at = now or datetime.now(UTC)
        journal = await self.journal_health.check()
        kill = await self.kill_switch.current()
        data_sla = DataSLAConfig.from_settings(self.settings).status
        async with self.session_factory() as session:
            risk_policy = await RiskBudgetRepository().effective_policy(
                session,
                as_of=checked_at,
            )
            admission = await latest_admission_policy(
                session,
                strategy_version=self.settings.intraday_v24_strategy_version,
                as_of=checked_at,
            )
            ideas = list(
                await session.scalars(
                    select(IdeaJournal)
                    .where(
                        IdeaJournal.strategy_version == self.settings.intraday_v24_strategy_version
                    )
                    .order_by(IdeaJournal.signal_datetime)
                )
            )
            models = list(
                await session.scalars(
                    select(ModelTradeJournal).where(
                        ModelTradeJournal.strategy_version
                        == self.settings.intraday_v24_strategy_version
                    )
                )
            )
            actual_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(ActualTradeJournal)
                    .where(
                        ActualTradeJournal.strategy_version
                        == self.settings.intraday_v24_strategy_version
                    )
                )
                or 0
            )
            snapshots = list(
                await session.scalars(
                    select(DecisionSnapshotV24).where(
                        DecisionSnapshotV24.strategy_version
                        == self.settings.intraday_v24_strategy_version
                    )
                )
            )
            scan_state = await session.get(JobRunState, "intraday_v24_cycle")
        calibration_status = await self._calibration_status(
            models,
            checked_at,
            admission is not None,
        )
        gemini = (
            self.gemini_health.last_report.api_status
            if self.gemini_health is not None and self.gemini_health.last_report is not None
            else "NOT_CHECKED"
        )
        try:
            scan_details = json.loads(scan_state.details or "{}") if scan_state else {}
        except (TypeError, json.JSONDecodeError):
            scan_details = {}
        if not isinstance(scan_details, dict):
            scan_details = {}
        scan_result = scan_details.get("scan", scan_details)
        if not isinstance(scan_result, dict):
            scan_result = {}
        liquidity_configured = (
            LiquidityModelConfig.from_settings(self.settings).status
            is ConfigurationStatus.CONFIGURED
        )
        cost_config = CostModelConfig(
            broker_commission_pct=self.settings.intraday_v24_broker_commission_pct,
            exchange_fee_pct=self.settings.intraday_v24_exchange_fee_pct,
            entry_slippage_bps=self.settings.intraday_v24_entry_slippage_bps,
            exit_slippage_bps=self.settings.intraday_v24_exit_slippage_bps,
            stop_slippage_bps=self.settings.intraday_v24_stop_slippage_bps,
            short_carry_pct_per_day=self.settings.intraday_v24_short_carry_pct,
        )
        cost_configured = all(
            cost_config.status_for(direction).value == "CONFIGURED"
            for direction in (JournalDirection.LONG, JournalDirection.SHORT)
        )
        latest_audit_pass = bool(ideas and ideas[-1].audit_status == "PASS")
        # The bundled provider deliberately returns DATA_NOT_AVAILABLE. This
        # must only become true when an authenticated point-in-time provider is
        # wired and health-checked, not merely when a feature flag is toggled.
        external_context_ready = False
        final_audit_ready = bool(
            journal.available
            and data_sla is ConfigurationStatus.CONFIGURED
            and risk_policy is not None
            and risk_policy.status is RiskBudgetStatus.CONFIGURED
            and liquidity_configured
            and cost_configured
            and external_context_ready
            and latest_audit_pass
        )
        status = V24RuntimeStatus(
            enabled=self.settings.intraday_v24_enabled,
            journal=journal,
            data_sla=data_sla,
            risk_budget=(
                risk_policy.status if risk_policy is not None else RiskBudgetStatus.NOT_CONFIGURED
            ),
            risk_policy_version=(
                risk_policy.configuration_version if risk_policy is not None else None
            ),
            statistical_admission=(
                StatisticalAdmissionStatus.NOT_CONFIGURED
                if admission is None
                else (
                    StatisticalAdmissionStatus.PASS
                    if calibration_status is CalibrationStatus.CALIBRATED
                    else StatisticalAdmissionStatus.FAIL
                )
            ),
            admission_policy_version=(
                admission.configuration_version if admission is not None else None
            ),
            calibration=calibration_status,
            kill_switch=kill,
            gemini=gemini,
            model_trade_count=len(models),
            actual_trade_count=actual_count,
            ambiguous_execution_count=sum(item.ambiguous_execution for item in models),
            uncalibrated_signal_count=sum(
                item.probability_status != "CALIBRATED" for item in ideas
            ),
            data_availability_failure_count=sum(
                item.data_sla_result not in {"PASS", None}
                or item.data_confidence in {"LOW", "UNKNOWN"}
                for item in ideas
            ),
            journal_write_count=len(ideas) + len(models) + actual_count,
            journal_error_count=0 if journal.available else 1,
            data_sla_distribution=dict(
                Counter(item.data_sla_result or "NOT_RECORDED" for item in ideas)
            ),
            microstructure_distribution=dict(
                Counter(item.microstructure_status or "NOT_RECORDED" for item in ideas)
            ),
            audit_failure_reasons=self._audit_failure_reasons(snapshots),
            classification_distribution=dict(
                Counter(item.final_classification or "UNCLASSIFIED" for item in ideas)
            ),
            setup_distribution=dict(Counter(item.setup or "UNKNOWN" for item in ideas)),
            regime_distribution=dict(Counter(item.market_regime or "UNKNOWN" for item in ideas)),
            legacy_enabled=self.settings.enable_legacy_strategy,
            shadow_enabled=self.settings.intraday_v24_shadow_enabled,
            production_notification_enabled=(
                self.settings.intraday_v24_enabled
                and final_audit_ready
                and kill.allows_new_positions
            ),
            final_audit_ready=final_audit_ready,
            external_context_ready=external_context_ready,
            liquidity_configured=liquidity_configured,
            cost_configured=cost_configured,
            latest_scan_at=scan_state.finished_at if scan_state else None,
            latest_scan_status=(
                "OK"
                if scan_state is not None and scan_state.success is True
                else "ERROR"
                if scan_state is not None and scan_state.success is False
                else "NEVER"
            ),
            latest_scan_candidates=int(scan_result.get("candidates", 0) or 0),
            latest_scan_errors=int(scan_result.get("errors", 0) or 0),
        )
        self.log(status)
        return status

    @staticmethod
    def _audit_failure_reasons(
        snapshots: list[DecisionSnapshotV24],
    ) -> dict[str, int]:
        failures: Counter[str] = Counter()
        for snapshot in snapshots:
            try:
                gates = json.loads(snapshot.gate_results or "{}")
            except (TypeError, json.JSONDecodeError):
                failures["INVALID_GATE_RESULTS"] += 1
                continue
            if not isinstance(gates, dict):
                failures["INVALID_GATE_RESULTS"] += 1
                continue
            final_audit = gates.get("final_audit")
            if isinstance(final_audit, dict) and isinstance(final_audit.get("gates"), dict):
                gates = final_audit["gates"]
            for gate, result in gates.items():
                normalized = result.get("result") if isinstance(result, dict) else result
                if normalized == "FAIL":
                    failures[str(gate)] += 1
        return dict(failures)

    async def _calibration_status(
        self,
        models: list[ModelTradeJournal],
        checked_at: datetime,
        policy_configured: bool,
    ) -> CalibrationStatus:
        if not policy_configured:
            return CalibrationStatus.UNCALIBRATED
        cohorts: dict[str, CalibrationCohort] = {}
        for model in models:
            values = (
                model.calibration_setup,
                model.calibration_direction,
                model.calibration_market_regime,
                model.calibration_trend,
                model.calibration_volatility,
                model.calibration_time_of_day,
                model.calibration_rr_bucket,
                model.calibration_liquidity_state,
                model.calibration_context,
            )
            if model.calibration_group is None or any(value is None for value in values):
                continue
            cohort = CalibrationCohort(*values)  # type: ignore[arg-type]
            cohorts[model.calibration_group] = cohort
        for cohort in cohorts.values():
            assessment = await self.calibration.assess(
                strategy_version=self.settings.intraday_v24_strategy_version,
                cohort=cohort,
                as_of=checked_at,
            )
            if assessment.calibration_status is CalibrationStatus.CALIBRATED:
                return CalibrationStatus.CALIBRATED
        return CalibrationStatus.PRELIMINARY if models else CalibrationStatus.UNCALIBRATED

    @staticmethod
    def log(status: V24RuntimeStatus) -> None:
        payload = {
            "enabled": status.enabled,
            "journal": "AVAILABLE" if status.journal.available else "ERROR",
            "data_sla": status.data_sla.value,
            "risk_budget": status.risk_budget.value,
            "kill_switch": status.kill_switch.state.value,
            "calibration": status.calibration.value,
            "statistical_admission": status.statistical_admission.value,
            "model_trade_count": status.model_trade_count,
            "actual_trade_count": status.actual_trade_count,
            "ambiguous_executions": status.ambiguous_execution_count,
            "uncalibrated_signals": status.uncalibrated_signal_count,
            "data_availability_failures": status.data_availability_failure_count,
            "journal_writes": status.journal_write_count,
            "journal_errors": status.journal_error_count,
            "data_sla_distribution": status.data_sla_distribution,
            "microstructure_distribution": status.microstructure_distribution,
            "audit_failure_reasons": status.audit_failure_reasons,
            "classification_distribution": status.classification_distribution,
            "setup_distribution": status.setup_distribution,
            "regime_distribution": status.regime_distribution,
        }
        logger.info("v24_metrics %s", json.dumps(payload, sort_keys=True))
