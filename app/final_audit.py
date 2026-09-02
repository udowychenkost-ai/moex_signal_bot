from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from app.v24_domain import AuditGate, AuditGateResult, AuditStatus


@dataclass(frozen=True, slots=True)
class AuditAssessment:
    status: AuditStatus
    gates: dict[str, str]
    failed_gates: tuple[AuditGate, ...]
    reasons: tuple[str, ...]


class FinalAuditService:
    """All mandatory gates must pass; no score can compensate for a failure."""

    def evaluate(
        self,
        gate_results: Mapping[AuditGate | str, AuditGateResult | str],
    ) -> AuditAssessment:
        normalized: dict[AuditGate, AuditGateResult] = {}
        reasons: list[str] = []
        for raw_gate, raw_result in gate_results.items():
            try:
                gate = raw_gate if isinstance(raw_gate, AuditGate) else AuditGate(raw_gate)
                result = (
                    raw_result
                    if isinstance(raw_result, AuditGateResult)
                    else AuditGateResult(raw_result)
                )
            except ValueError as error:
                raise ValueError(
                    f"Unknown final-audit gate/result: {raw_gate}={raw_result}"
                ) from error
            normalized[gate] = result

        failed: list[AuditGate] = []
        for gate in AuditGate:
            result = normalized.get(gate)
            if result is None:
                failed.append(gate)
                reasons.append(f"{gate.value}:MISSING")
            elif result is AuditGateResult.FAIL:
                failed.append(gate)
                reasons.append(f"{gate.value}:FAIL")
            elif result is AuditGateResult.NOT_REQUIRED and gate is not AuditGate.CALIBRATION:
                failed.append(gate)
                reasons.append(f"{gate.value}:NOT_REQUIRED_NOT_ALLOWED")

        ordered = {
            gate.value: normalized.get(gate, AuditGateResult.FAIL).value for gate in AuditGate
        }
        return AuditAssessment(
            status=AuditStatus.FAIL if failed else AuditStatus.PASS,
            gates=ordered,
            failed_gates=tuple(failed),
            reasons=tuple(reasons),
        )

    @staticmethod
    def snapshot(assessment: AuditAssessment) -> dict[str, object]:
        return {
            "status": assessment.status.value,
            "gates": assessment.gates,
            "failed_gates": [gate.value for gate in assessment.failed_gates],
            "reasons": list(assessment.reasons),
        }
