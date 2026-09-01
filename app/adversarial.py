from __future__ import annotations

from dataclasses import dataclass, fields

from app.v24_domain import AdversarialResult


@dataclass(frozen=True, slots=True)
class AdversarialInputs:
    trend_wrong: bool | None
    news_priced_in: bool | None
    entry_late: bool | None
    false_breakout: bool | None
    stop_weak: bool | None
    tp_unrealistic: bool | None
    path_blocked: bool | None
    volume_weak: bool | None
    market_against: bool | None
    sector_against: bool | None
    liquidity_weak: bool | None
    probability_uncalibrated: bool | None
    execution_poor: bool | None
    opportunity_cost_high: bool | None
    corporate_action_risk: bool | None
    microstructure_risk: bool | None
    journal_gap: bool | None
    risk_budget_missing: bool | None
    created_only_for_activity: bool | None


@dataclass(frozen=True, slots=True)
class AdversarialAssessment:
    result: AdversarialResult
    hard_fail_reasons: tuple[str, ...]
    wait_reasons: tuple[str, ...]
    ai_reasons: tuple[str, ...]


HARD_FAIL_FIELDS = frozenset(
    {
        "trend_wrong",
        "entry_late",
        "false_breakout",
        "stop_weak",
        "tp_unrealistic",
        "path_blocked",
        "volume_weak",
        "market_against",
        "sector_against",
        "liquidity_weak",
        "execution_poor",
        "opportunity_cost_high",
        "corporate_action_risk",
        "microstructure_risk",
        "journal_gap",
        "risk_budget_missing",
        "created_only_for_activity",
    }
)
WAIT_FIELDS = frozenset({"news_priced_in", "probability_uncalibrated"})


class AdversarialCheck:
    """Deterministic objections have priority over any AI second opinion."""

    def evaluate(
        self,
        inputs: AdversarialInputs,
        *,
        ai_result: AdversarialResult | None = None,
        ai_reasons: tuple[str, ...] = (),
    ) -> AdversarialAssessment:
        values = {field.name: getattr(inputs, field.name) for field in fields(inputs)}
        hard_fail = tuple(
            sorted(name.upper() for name in HARD_FAIL_FIELDS if values[name] is True)
        )
        wait = [name.upper() for name in WAIT_FIELDS if values[name] is True]
        wait.extend(
            f"{name.upper()}_UNKNOWN"
            for name, value in values.items()
            if value is None
        )
        if hard_fail:
            result = AdversarialResult.FAIL
        elif ai_result is AdversarialResult.FAIL:
            result = AdversarialResult.FAIL
        elif wait or ai_result is AdversarialResult.WAIT:
            result = AdversarialResult.WAIT
        else:
            result = AdversarialResult.PASS
        return AdversarialAssessment(
            result=result,
            hard_fail_reasons=hard_fail,
            wait_reasons=tuple(sorted(wait)),
            ai_reasons=ai_reasons,
        )

    @staticmethod
    def snapshot(assessment: AdversarialAssessment) -> dict[str, object]:
        return {
            "result": assessment.result.value,
            "hard_fail_reasons": list(assessment.hard_fail_reasons),
            "wait_reasons": list(assessment.wait_reasons),
            "ai_reasons": list(assessment.ai_reasons),
        }
