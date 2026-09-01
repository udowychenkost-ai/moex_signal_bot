from __future__ import annotations

import math
from dataclasses import dataclass

from app.v24_domain import OpportunityRankingMode


@dataclass(frozen=True, slots=True)
class OpportunityCostWeights:
    structural_quality: float
    risk_efficiency: float
    capital_efficiency: float
    holding_time: float
    liquidity: float
    factor_diversification: float
    event_safety: float
    overnight_safety: float

    def normalized(self) -> dict[str, float]:
        values = {
            "structural_quality": self.structural_quality,
            "risk_efficiency": self.risk_efficiency,
            "capital_efficiency": self.capital_efficiency,
            "holding_time": self.holding_time,
            "liquidity": self.liquidity,
            "factor_diversification": self.factor_diversification,
            "event_safety": self.event_safety,
            "overnight_safety": self.overnight_safety,
        }
        if any(not math.isfinite(value) or value < 0 for value in values.values()):
            raise ValueError("Opportunity weights must be finite and non-negative")
        total = sum(values.values())
        if total <= 0:
            raise ValueError("At least one opportunity weight must be positive")
        return {name: value / total for name, value in values.items()}


@dataclass(frozen=True, slots=True)
class OpportunityCandidate:
    candidate_id: str
    structural_quality: float
    risk_rub: float
    capital_rub: float
    expected_holding_hours: float
    liquidity_score: float
    factor_risk_score: float
    event_risk_score: float
    overnight_risk_score: float
    statistically_calibrated: bool = False
    probability_tp_before_sl: float | None = None
    reward_r: float | None = None
    loss_r: float | None = None


@dataclass(frozen=True, slots=True)
class RankedOpportunity:
    candidate_id: str
    rank: int
    mode: OpportunityRankingMode
    score: float
    net_ev_rub: float | None
    ev_per_capital_hour: float | None


class OpportunityCostRanker:
    def __init__(self, weights: OpportunityCostWeights | None) -> None:
        self.weights = weights

    @staticmethod
    def _validate(candidate: OpportunityCandidate) -> None:
        if not candidate.candidate_id:
            raise ValueError("candidate_id is required")
        if not 0 <= candidate.structural_quality <= 100:
            raise ValueError("structural_quality must be in [0, 100]")
        if candidate.risk_rub < 0 or candidate.capital_rub <= 0:
            raise ValueError("risk_rub must be non-negative and capital_rub positive")
        if candidate.expected_holding_hours <= 0:
            raise ValueError("expected_holding_hours must be positive")
        for score in (
            candidate.liquidity_score,
            candidate.factor_risk_score,
            candidate.event_risk_score,
            candidate.overnight_risk_score,
        ):
            if not 0 <= score <= 1:
                raise ValueError("Opportunity component scores must be in [0, 1]")

    @staticmethod
    def _has_valid_ev(candidate: OpportunityCandidate) -> bool:
        return bool(
            candidate.statistically_calibrated
            and candidate.probability_tp_before_sl is not None
            and 0 <= candidate.probability_tp_before_sl <= 1
            and candidate.reward_r is not None
            and candidate.reward_r >= 0
            and candidate.loss_r is not None
            and candidate.loss_r >= 0
        )

    def rank(self, candidates: tuple[OpportunityCandidate, ...]) -> list[RankedOpportunity]:
        if not candidates:
            return []
        for candidate in candidates:
            self._validate(candidate)
        if all(self._has_valid_ev(candidate) for candidate in candidates):
            rows: list[tuple[OpportunityCandidate, float, float]] = []
            for candidate in candidates:
                probability = candidate.probability_tp_before_sl
                reward_r = candidate.reward_r
                loss_r = candidate.loss_r
                assert probability is not None and reward_r is not None and loss_r is not None
                expected_r = probability * reward_r - (1 - probability) * loss_r
                net_ev = expected_r * candidate.risk_rub
                ev_rate = net_ev / candidate.capital_rub / candidate.expected_holding_hours
                rows.append((candidate, net_ev, ev_rate))
            rows.sort(key=lambda row: (row[2], row[1]), reverse=True)
            return [
                RankedOpportunity(
                    candidate_id=candidate.candidate_id,
                    rank=index,
                    mode=OpportunityRankingMode.CALIBRATED_EV,
                    score=ev_rate,
                    net_ev_rub=net_ev,
                    ev_per_capital_hour=ev_rate,
                )
                for index, (candidate, net_ev, ev_rate) in enumerate(rows, start=1)
            ]

        if self.weights is None:
            return [
                RankedOpportunity(
                    candidate_id=candidate.candidate_id,
                    rank=index,
                    mode=OpportunityRankingMode.NOT_RELIABLY_CALCULABLE,
                    score=0,
                    net_ev_rub=None,
                    ev_per_capital_hour=None,
                )
                for index, candidate in enumerate(candidates, start=1)
            ]

        weights = self.weights.normalized()
        max_capital = max(candidate.capital_rub for candidate in candidates)
        rows = []
        for candidate in candidates:
            components = {
                "structural_quality": candidate.structural_quality / 100,
                "risk_efficiency": 1 - min(candidate.risk_rub / candidate.capital_rub, 1),
                "capital_efficiency": 1 - candidate.capital_rub / max_capital,
                "holding_time": 1 / (1 + candidate.expected_holding_hours / 24),
                "liquidity": candidate.liquidity_score,
                "factor_diversification": 1 - candidate.factor_risk_score,
                "event_safety": 1 - candidate.event_risk_score,
                "overnight_safety": 1 - candidate.overnight_risk_score,
            }
            score = sum(weights[name] * value for name, value in components.items())
            rows.append((candidate, score))
        rows.sort(key=lambda row: row[1], reverse=True)
        return [
            RankedOpportunity(
                candidate_id=candidate.candidate_id,
                rank=index,
                mode=OpportunityRankingMode.COLD_START_COMPOSITE,
                score=score,
                net_ev_rub=None,
                ev_per_capital_hour=None,
            )
            for index, (candidate, score) in enumerate(rows, start=1)
        ]
