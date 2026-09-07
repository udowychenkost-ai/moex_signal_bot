from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai_analyst import AIAnalystService, AIReviewResult
from app.config import Settings
from app.domain import IdeaHorizon, TradingIdeaData
from app.horizons import get_horizon_profile
from app.ideas import TradingIdeaGenerator
from app.ingestion import IngestionService
from app.models import AIRequestLog, TradingIdea
from app.quality import QualityGate, QualityGateResult


@dataclass(frozen=True, slots=True)
class CurrentAIResult:
    candidate: TradingIdeaData | None
    quality: QualityGateResult | None
    review: AIReviewResult | None
    reason: str = ""

    @property
    def completed(self) -> bool:
        return self.candidate is not None and self.review is not None


class OnDemandAIService:
    """Analyze current market state without mutating the historical TradingIdea."""

    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        ingestion: IngestionService,
        ideas: TradingIdeaGenerator,
        analyst: AIAnalystService,
        quality_gate: QualityGate,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.ingestion = ingestion
        self.ideas = ideas
        self.analyst = analyst
        self.quality_gate = quality_gate

    async def _refresh(self, ticker: str, horizon: IdeaHorizon) -> None:
        profile = get_horizon_profile(horizon)
        for timeframe in profile.timeframe_weights:
            await self.ingestion.refresh_ticker(ticker, timeframe)
            if self.settings.market_context_enabled:
                await self.ingestion.sync_market_candles(
                    self.settings.market_benchmark,
                    timeframe,
                )

    async def _save_telemetry(self, review: AIReviewResult) -> None:
        rows = review.attempts or ()
        async with self.session_factory() as session, session.begin():
            if rows:
                for attempt in rows:
                    session.add(
                        AIRequestLog(
                            request_kind="ON_DEMAND_IDEA",
                            provider=attempt.provider,
                            model=attempt.model,
                            status=attempt.status,
                            input_tokens=attempt.input_tokens,
                            output_tokens=attempt.output_tokens,
                            estimated_cost_usd=attempt.estimated_cost_usd,
                            latency_ms=attempt.latency_ms,
                            error=attempt.error,
                            fallback_used=attempt.fallback_used,
                            usage_json=json.dumps(attempt.usage or {}, sort_keys=True),
                            created_at=review.reviewed_at or datetime.now(UTC),
                        )
                    )
            else:
                session.add(
                    AIRequestLog(
                        request_kind="ON_DEMAND_IDEA",
                        provider=review.provider,
                        model=review.model,
                        status=review.status,
                        input_tokens=review.input_tokens,
                        output_tokens=review.output_tokens,
                        estimated_cost_usd=review.estimated_cost_usd,
                        latency_ms=review.latency_ms,
                        error=review.error,
                        fallback_used=review.fallback_used,
                        usage_json=json.dumps(review.usage or {}, sort_keys=True),
                        created_at=review.reviewed_at or datetime.now(UTC),
                    )
                )

    async def analyze(self, idea: TradingIdea) -> CurrentAIResult:
        try:
            horizon = IdeaHorizon(idea.horizon)
        except ValueError:
            return CurrentAIResult(None, None, None, "Неизвестный горизонт старой идеи.")
        await self._refresh(idea.ticker, horizon)
        candidate = await self.ideas.generate_candidate(idea.ticker, horizon)
        if candidate is None:
            return CurrentAIResult(
                None,
                None,
                None,
                "Текущее состояние не формирует отдельный BUY/SELL candidate.",
            )
        quality = self.quality_gate.evaluate(candidate)
        review = await self.analyst.review_current(candidate, quality)
        await self._save_telemetry(review)
        return CurrentAIResult(candidate, quality, review)
