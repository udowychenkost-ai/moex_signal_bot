from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from enum import Enum

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai_analyst import AIReviewResult
from app.domain import IdeaStatus, TradingIdeaData
from app.idea_repository import OPEN_IDEA_STATUSES
from app.idea_tracker import evaluate_idea_candle
from app.models import AIRequestLog, CandidateExperiment, Candle, TradingIdea
from app.observation import aware_utc, completed_candles
from app.quality import QualityGateResult
from app.repositories import get_candles_after


def candidate_experiment_key(candidate: TradingIdeaData, strategy_version: str) -> str:
    value = "|".join(
        (
            strategy_version,
            candidate.ticker.upper(),
            candidate.direction.value,
            candidate.horizon.value,
            aware_utc(candidate.source_candle_begin).isoformat(),
        )
    )
    return hashlib.sha256(value.encode()).hexdigest()


def _json_default(value: object) -> object:
    if isinstance(value, datetime):
        return aware_utc(value).isoformat()
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def _json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
        default=_json_default,
    )


async def candidate_exists(session: AsyncSession, key: str) -> bool:
    return (
        await session.scalar(
            select(CandidateExperiment.id).where(CandidateExperiment.candidate_key == key)
        )
        is not None
    )


def _review_values(review: AIReviewResult | None) -> dict[str, object]:
    if review is None:
        return {
            "ai_verdict": "NOT_REQUESTED",
            "ai_score": None,
            "ai_confidence": None,
            "ai_bull_case": "",
            "ai_bear_case": "",
            "ai_key_risks": "[]",
            "ai_why_now": "",
            "ai_invalidation_conditions": "[]",
            "ai_short_summary": "",
            "ai_provider": None,
            "ai_model": None,
            "ai_reviewed_at": None,
            "ai_status": "NOT_REQUESTED",
            "ai_input_tokens": 0,
            "ai_output_tokens": 0,
            "ai_estimated_cost_usd": 0.0,
            "ai_latency_ms": 0,
            "ai_error": "",
        }
    analysis = review.analysis
    return {
        "ai_verdict": analysis.verdict,
        "ai_score": analysis.ai_score,
        "ai_confidence": analysis.confidence_in_analysis,
        "ai_bull_case": analysis.bull_case,
        "ai_bear_case": analysis.bear_case,
        "ai_key_risks": _json(analysis.key_risks),
        "ai_why_now": analysis.why_now,
        "ai_invalidation_conditions": _json(analysis.invalidation_conditions),
        "ai_short_summary": analysis.short_summary,
        "ai_provider": review.provider,
        "ai_model": review.model,
        "ai_reviewed_at": review.reviewed_at,
        "ai_status": review.status,
        "ai_input_tokens": review.input_tokens,
        "ai_output_tokens": review.output_tokens,
        "ai_estimated_cost_usd": review.estimated_cost_usd,
        "ai_latency_ms": review.latency_ms,
        "ai_error": review.error,
    }


async def save_candidate_experiment(
    session: AsyncSession,
    candidate: TradingIdeaData,
    quality: QualityGateResult,
    *,
    review: AIReviewResult | None = None,
    published: bool = False,
    publish_reason: str = "",
    published_idea_id: int | None = None,
) -> CandidateExperiment:
    key = candidate_experiment_key(candidate, candidate.strategy_version)
    existing = await session.scalar(
        select(CandidateExperiment).where(CandidateExperiment.candidate_key == key)
    )
    if existing is not None:
        return existing
    row = CandidateExperiment(
        candidate_key=key,
        strategy_version=candidate.strategy_version,
        ticker=candidate.ticker.upper(),
        direction=candidate.direction.value,
        horizon=candidate.horizon.value,
        primary_timeframe=candidate.primary_timeframe,
        quant_result=candidate.direction.value,
        quality_gate_result=quality.decision.value,
        final_quality_score=quality.final_quality_score,
        supporting_factors=_json(quality.supporting_factors),
        contradicting_factors=_json(quality.contradicting_factors),
        quality_reasons=_json(quality.reasons),
        confirmation_count=quality.confirmation_count,
        timeframe_confirmations=quality.timeframe_confirmations,
        published=published,
        publish_reason=publish_reason,
        published_idea_id=published_idea_id,
        current_price=candidate.current_price,
        entry_price_from=candidate.entry_price_from,
        entry_price_to=candidate.entry_price_to,
        take_profit=candidate.take_profit,
        stop_loss=candidate.stop_loss,
        risk_reward_ratio=candidate.risk_reward_ratio,
        technical_score=candidate.technical_score,
        fundamental_score=candidate.fundamental_score,
        news_score=candidate.news_score,
        total_score=candidate.total_score,
        signal_strength=candidate.confidence,
        snapshot_json=_json(asdict(candidate)),
        status=IdeaStatus.PENDING_ENTRY.value,
        source_candle_begin=candidate.source_candle_begin,
        decision_at=candidate.created_at,
        expires_at=candidate.expires_at,
        last_evaluated_at=candidate.source_candle_begin,
        **_review_values(review),
    )
    session.add(row)
    await session.flush()
    if review is not None:
        session.add(
            AIRequestLog(
                request_kind="CANDIDATE",
                candidate_id=row.id,
                provider=review.provider,
                model=review.model,
                status=review.status,
                input_tokens=review.input_tokens,
                output_tokens=review.output_tokens,
                estimated_cost_usd=review.estimated_cost_usd,
                latency_ms=review.latency_ms,
                error=review.error,
                created_at=review.reviewed_at or datetime.now(UTC),
            )
        )
    return row


def _materially_changed(open_idea: TradingIdea, candidate: TradingIdeaData, delta: float) -> bool:
    if abs(open_idea.confidence - candidate.confidence) >= delta:
        return True
    for old, new in (
        (open_idea.entry_price_from, candidate.entry_price_from),
        (open_idea.entry_price_to, candidate.entry_price_to),
        (open_idea.take_profit, candidate.take_profit),
        (open_idea.stop_loss, candidate.stop_loss),
    ):
        if abs(new - old) / abs(old or 1.0) >= 0.005:
            return True
    return False


async def cooldown_reason(
    session: AsyncSession,
    candidate: TradingIdeaData,
    *,
    cooldown_hours: int,
    confidence_delta: float,
) -> str | None:
    open_idea = await session.scalar(
        select(TradingIdea)
        .where(
            TradingIdea.ticker == candidate.ticker.upper(),
            TradingIdea.horizon == candidate.horizon.value,
            TradingIdea.direction == candidate.direction.value,
            TradingIdea.status.in_(OPEN_IDEA_STATUSES),
        )
        .order_by(TradingIdea.created_at.desc())
        .limit(1)
    )
    if open_idea is not None:
        return (
            None
            if _materially_changed(open_idea, candidate, confidence_delta)
            else "OPEN_DUPLICATE"
        )
    if cooldown_hours <= 0:
        return None
    cutoff = aware_utc(candidate.created_at) - timedelta(hours=cooldown_hours)
    recent = await session.scalar(
        select(TradingIdea.id)
        .where(
            TradingIdea.ticker == candidate.ticker.upper(),
            TradingIdea.horizon == candidate.horizon.value,
            TradingIdea.direction == candidate.direction.value,
            TradingIdea.created_at >= cutoff,
        )
        .order_by(TradingIdea.created_at.desc())
        .limit(1)
    )
    return "COOLDOWN" if recent is not None else None


async def published_today_count(
    session: AsyncSession,
    *,
    horizon: str,
    strategy_version: str,
    day_start_utc: datetime,
) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(TradingIdea)
            .where(
                TradingIdea.horizon == horizon,
                TradingIdea.strategy_version == strategy_version,
                TradingIdea.created_at >= day_start_utc,
            )
        )
        or 0
    )


def experiment_realized_r(candidate: CandidateExperiment) -> float | None:
    if candidate.activation_price is None or candidate.close_price is None:
        return None
    risk = abs(candidate.activation_price - candidate.stop_loss)
    if risk <= 0:
        return None
    movement = (
        candidate.close_price - candidate.activation_price
        if candidate.direction == "BUY"
        else candidate.activation_price - candidate.close_price
    )
    return movement / risk


class CandidateExperimentTracker:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def track_candidate(self, candidate_id: int, candles: list[object]) -> int:
        transitions_applied = 0
        async with self.session_factory() as session, session.begin():
            candidate = await session.get(CandidateExperiment, candidate_id)
            if candidate is None:
                return 0
            for candle in sorted(candles, key=lambda item: item.begin):
                if candidate.status not in OPEN_IDEA_STATUSES:
                    break
                transitions = evaluate_idea_candle(candidate, candle)
                candidate.current_price = candle.close
                candidate.last_evaluated_at = candle.begin
                for transition in transitions:
                    candidate.status = transition.to_status.value
                    if transition.to_status == IdeaStatus.ACTIVE:
                        candidate.activated_at = transition.occurred_at
                        candidate.activation_price = transition.price
                    elif transition.to_status not in {
                        IdeaStatus.PENDING_ENTRY,
                        IdeaStatus.ACTIVE,
                    }:
                        candidate.closed_at = transition.occurred_at
                        candidate.close_price = transition.price
                        candidate.actual_r = experiment_realized_r(candidate)
                    transitions_applied += 1
        return transitions_applied

    async def track_all(self, *, now: datetime | None = None) -> dict[str, int]:
        current = aware_utc(now or datetime.now(UTC))
        async with self.session_factory() as session:
            open_rows = list(
                await session.scalars(
                    select(CandidateExperiment).where(
                        CandidateExperiment.status.in_(OPEN_IDEA_STATUSES)
                    )
                )
            )
        evaluated = 0
        transitions = 0
        for detached in open_rows:
            after = detached.last_evaluated_at or detached.source_candle_begin
            async with self.session_factory() as session:
                candles: list[Candle] = await get_candles_after(
                    session,
                    detached.ticker,
                    detached.primary_timeframe,
                    after,
                )
            candles = completed_candles(candles, detached.primary_timeframe, now=current)
            evaluated += len(candles)
            transitions += await self.track_candidate(detached.id, candles)
            if detached.status in OPEN_IDEA_STATUSES and aware_utc(detached.expires_at) <= current:
                synthetic = type(
                    "SyntheticCandle",
                    (),
                    {
                        "begin": current,
                        "end": current,
                        "open": detached.current_price,
                        "high": detached.current_price,
                        "low": detached.current_price,
                        "close": detached.current_price,
                    },
                )()
                transitions += await self.track_candidate(detached.id, [synthetic])
        return {"candidate_evaluated": evaluated, "candidate_transitions": transitions}
