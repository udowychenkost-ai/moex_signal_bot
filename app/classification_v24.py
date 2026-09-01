from __future__ import annotations

from dataclasses import dataclass

from app.adversarial import AdversarialAssessment, AdversarialCheck
from app.calibration import CalibrationAssessment
from app.final_audit import AuditAssessment, FinalAuditService
from app.v24_domain import (
    AdversarialResult,
    AuditStatus,
    CalibrationStatus,
    FinalDecision,
    StatisticalAdmissionStatus,
    V24Classification,
)


@dataclass(frozen=True, slots=True)
class ClassificationInputs:
    final_decision: FinalDecision
    audit: AuditAssessment
    adversarial: AdversarialAssessment
    calibration: CalibrationAssessment | None
    setup_strong: bool
    watch_requested: bool = False
    shadow_requested: bool = False
    model_candidate: bool = False


@dataclass(frozen=True, slots=True)
class ClassificationAssessment:
    classification: V24Classification
    reasons: tuple[str, ...]


class V24ClassificationService:
    def classify(self, inputs: ClassificationInputs) -> ClassificationAssessment:
        if inputs.final_decision is FinalDecision.CANCEL:
            return ClassificationAssessment(V24Classification.CANCEL, ("DECISION_CANCEL",))
        if inputs.final_decision is FinalDecision.NO_TRADE:
            return ClassificationAssessment(V24Classification.NO_TRADE, ("DECISION_NO_TRADE",))
        if inputs.adversarial.result is AdversarialResult.FAIL:
            return ClassificationAssessment(
                V24Classification.REJECT,
                ("ADVERSARIAL_FAIL", *inputs.adversarial.hard_fail_reasons),
            )
        if inputs.audit.status is AuditStatus.PASS:
            calibration = inputs.calibration
            if (
                calibration is not None
                and calibration.calibration_status is CalibrationStatus.CALIBRATED
                and calibration.statistical_admission is StatisticalAdmissionStatus.PASS
                and calibration.interval is not None
                and calibration.interval.lower >= 0.70
            ):
                return ClassificationAssessment(
                    V24Classification.STATISTICALLY_QUALIFIED_70,
                    ("AUDIT_PASS", "CALIBRATION_PASS", "WILSON_LOWER_BOUND_AT_LEAST_70"),
                )
            return ClassificationAssessment(
                V24Classification.PRODUCTION_QUALIFIED,
                ("AUDIT_PASS",),
            )
        if inputs.setup_strong:
            return ClassificationAssessment(
                V24Classification.STRUCTURALLY_QUALIFIED,
                ("SETUP_STRONG", "PRODUCTION_PREREQUISITES_INCOMPLETE"),
            )
        if inputs.watch_requested:
            return ClassificationAssessment(V24Classification.WATCH, ("WATCH_REQUESTED",))
        if inputs.shadow_requested:
            return ClassificationAssessment(V24Classification.SHADOW, ("SHADOW_REQUESTED",))
        if inputs.model_candidate:
            return ClassificationAssessment(
                V24Classification.MODEL_CANDIDATE,
                ("MODEL_CANDIDATE_ONLY",),
            )
        return ClassificationAssessment(V24Classification.REJECT, ("AUDIT_FAIL",))


def initial_journal_audit_values(
    *,
    classification: ClassificationAssessment,
    audit: AuditAssessment,
    adversarial: AdversarialAssessment,
) -> tuple[dict[str, object], dict[str, object]]:
    """Fields passed at initial journal creation; no post-hoc snapshot mutation."""

    journal_values: dict[str, object] = {
        "audit_status": audit.status.value,
        "adversarial_result": adversarial.result.value,
        "final_classification": classification.classification.value,
    }
    snapshot_values: dict[str, object] = {
        "gate_results": {
            "final_audit": FinalAuditService.snapshot(audit),
            "adversarial": AdversarialCheck.snapshot(adversarial),
            "classification": {
                "value": classification.classification.value,
                "reasons": list(classification.reasons),
            },
        }
    }
    return journal_values, snapshot_values
