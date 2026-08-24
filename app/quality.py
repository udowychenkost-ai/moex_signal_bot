from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings
from app.domain import IdeaDirection, QualityGateDecision, TradingIdeaData

FACTOR_LABELS = {
    "trend": "trend",
    "momentum": "momentum",
    "volume": "volume",
    "levels": "levels",
    "market_regime": "market regime",
    "relative_strength": "relative strength",
    "fundamental": "fundamental",
}


@dataclass(frozen=True, slots=True)
class QualityGateResult:
    decision: QualityGateDecision
    final_quality_score: float
    supporting_factors: tuple[str, ...]
    contradicting_factors: tuple[str, ...]
    confirmation_count: int
    timeframe_confirmations: int
    reasons: tuple[str, ...]


def _bounded(value: float) -> float:
    return max(0.0, min(100.0, value))


def _primary_indicators(candidate: TradingIdeaData) -> dict[str, object]:
    value = candidate.relevant_indicators.get(candidate.primary_timeframe, {})
    return value if isinstance(value, dict) else {}


def _float(value: object) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    return None


class QualityGate:
    """Deterministic fail-closed filter between quant scoring and the AI review."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def evaluate(
        self,
        candidate: TradingIdeaData,
        *,
        data_fresh: bool = True,
    ) -> QualityGateResult:
        sign = 1.0 if candidate.direction == IdeaDirection.BUY else -1.0
        directional_technical = candidate.technical_score * sign
        directional_total = candidate.total_score * sign
        components = candidate.factor_scores.get("technical_components", {})
        if not isinstance(components, dict):
            components = {}

        directional: dict[str, float] = {
            name: float(components.get(name, 0.0)) * sign
            for name in (
                "trend",
                "momentum",
                "volume",
                "levels",
                "market_regime",
                "relative_strength",
            )
        }
        if candidate.fundamental_publications:
            directional["fundamental"] = candidate.fundamental_score * sign

        supporting = tuple(
            FACTOR_LABELS[name]
            for name, score in directional.items()
            if score >= self.settings.quality_confirmation_score
        )
        contradicting = tuple(
            FACTOR_LABELS[name]
            for name, score in directional.items()
            if score <= -self.settings.quality_conflict_score
        )
        timeframe_scores = candidate.factor_scores.get("timeframe_scores", {})
        if not isinstance(timeframe_scores, dict):
            timeframe_scores = {}
        timeframe_confirmations = sum(
            float(score) * sign >= self.settings.signal_threshold
            for score in timeframe_scores.values()
            if isinstance(score, int | float)
        )

        volume_ratio = _float(_primary_indicators(candidate).get("volume_ratio"))
        volume_adequate = (
            volume_ratio is not None
            and volume_ratio >= self.settings.quality_min_volume_ratio
            and directional.get("volume", 0.0) >= 0
        )
        liquidity_known = candidate.daily_turnover is not None
        liquidity_ok = bool(
            liquidity_known
            and candidate.daily_turnover is not None
            and candidate.daily_turnover >= self.settings.quality_min_daily_turnover
        )
        opposing_regime = candidate.market_regime_score * sign
        strongly_opposed = opposing_regime <= -self.settings.quality_strong_regime_score
        override = self.settings.quality_regime_override_score
        regime_override = all(
            (
                directional.get("relative_strength", 0.0) >= override,
                directional.get("volume", 0.0) >= override / 2,
                directional.get("levels", 0.0) >= override / 2,
                directional.get("momentum", 0.0) >= override / 2
                or float(candidate.momentum_extreme_score) * sign >= override / 2,
            )
        )

        hard_reasons: list[str] = []
        weak_reasons: list[str] = []
        if not data_fresh:
            hard_reasons.append("decision data is stale")
        if directional_technical < self.settings.quality_min_technical_score:
            hard_reasons.append(
                f"technical strength {directional_technical:.1f} below "
                f"{self.settings.quality_min_technical_score:.1f}"
            )
        if directional_total < self.settings.quality_min_total_score:
            hard_reasons.append(
                f"total strength {directional_total:.1f} below "
                f"{self.settings.quality_min_total_score:.1f}"
            )
        if candidate.risk_reward_ratio < self.settings.minimum_reward_risk_ratio:
            hard_reasons.append("reward:risk is below the system minimum")
        if liquidity_known and not liquidity_ok:
            hard_reasons.append("daily turnover is below the liquidity floor")
        elif not liquidity_known:
            weak_reasons.append("liquidity is unavailable")
        if timeframe_confirmations < self.settings.quality_min_timeframe_confirmations:
            hard_reasons.append(f"only {timeframe_confirmations} confirming timeframe(s)")
        if len(contradicting) > self.settings.quality_max_conflicts:
            hard_reasons.append("too many independent contradicting factors")
        if strongly_opposed and not regime_override:
            side = "BUY" if candidate.direction == IdeaDirection.BUY else "SELL"
            hard_reasons.append(f"{side} conflicts with a strong market regime")
        required_confirmations = self.settings.minimum_confirmations(candidate.horizon)
        if len(supporting) < required_confirmations:
            weak_reasons.append(f"only {len(supporting)} independent confirmation(s)")
        if not volume_adequate:
            weak_reasons.append("volume does not adequately confirm the direction")

        quality_score = _bounded(
            directional_total * 0.35
            + directional_technical * 0.25
            + len(supporting) * 5
            + timeframe_confirmations * 4
            + min(candidate.risk_reward_ratio, 4.0) * 4
            + (5 if liquidity_ok else 0)
            - len(contradicting) * 8
            - (15 if strongly_opposed and not regime_override else 0)
        )
        if hard_reasons:
            decision = QualityGateDecision.REJECT
            reasons = hard_reasons + weak_reasons
        elif weak_reasons:
            decision = QualityGateDecision.WEAK
            reasons = weak_reasons
        else:
            decision = QualityGateDecision.PASS
            reasons = ["all quantitative quality requirements passed"]
        return QualityGateResult(
            decision=decision,
            final_quality_score=round(quality_score, 2),
            supporting_factors=supporting,
            contradicting_factors=contradicting,
            confirmation_count=len(supporting),
            timeframe_confirmations=timeframe_confirmations,
            reasons=tuple(reasons),
        )


def apply_quality_result(
    candidate: TradingIdeaData,
    result: QualityGateResult,
    *,
    strategy_version: str,
) -> TradingIdeaData:
    candidate.quality_gate_result = result.decision.value
    candidate.final_quality_score = result.final_quality_score
    candidate.supporting_factors = list(result.supporting_factors)
    candidate.contradicting_factors = list(result.contradicting_factors)
    candidate.confirmation_count = result.confirmation_count
    candidate.strategy_version = strategy_version
    return candidate
