from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from app.ai_providers import (
    AIProvider,
    AsyncHTTPClient,
    GeminiProvider,
    OpenAIProvider,
    ProviderCallResult,
)
from app.config import Settings
from app.domain import AIConfidence, AIVerdict, TradingIdeaData
from app.quality import QualityGateResult

logger = logging.getLogger(__name__)
ShortItem = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]

SYSTEM_PROMPT = """You are a conservative second-opinion analyst for MOEX paper ideas.
Use ONLY the structured data supplied by the application. Never invent news, financial
figures, price levels, prices, events, forecasts, or missing indicators. Treat every
missing block as unavailable. Quantitative filters have already run: you may reject or
wait, but you must not rescue a weak quantitative signal. Focus on contradictions,
timing, invalidation, and whether the evidence supports this direction now. Keep every
text field concise. Scores are analytical ratings, never calibrated probabilities.
Return only the requested structured result."""


class AIAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: Literal["STRONG_APPROVE", "APPROVE", "WAIT", "REJECT"]
    score: float = Field(ge=0, le=100)
    analysis_confidence: Literal["LOW", "MEDIUM", "HIGH"]
    bull_case: str = Field(min_length=1, max_length=700)
    bear_case: str = Field(min_length=1, max_length=700)
    key_risks: list[ShortItem] = Field(min_length=1, max_length=3)
    why_now: str = Field(min_length=1, max_length=700)
    invalidation_conditions: list[ShortItem] = Field(min_length=1, max_length=3)
    short_summary: str = Field(min_length=1, max_length=700)

    @property
    def ai_score(self) -> float:
        """Internal V2 compatibility alias; provider schema uses `score`."""
        return self.score

    @property
    def confidence_in_analysis(self) -> str:
        """Internal V2 compatibility alias; provider schema uses `analysis_confidence`."""
        return self.analysis_confidence


class MarketAIAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    short_summary: str = Field(min_length=1, max_length=700)


@dataclass(frozen=True, slots=True)
class AIRequestAttempt:
    provider: str
    model: str
    status: str
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float
    latency_ms: int
    error: str = ""
    status_code: int | None = None
    error_code: str = ""
    model_unavailable: bool = False
    fallback_used: bool = False
    usage: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class MarketAIReviewResult:
    summary: str
    provider: str
    model: str
    status: str
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float
    latency_ms: int
    error: str = ""
    reviewed_at: datetime | None = None
    fallback_used: bool = False
    usage: dict[str, Any] | None = None
    attempts: tuple[AIRequestAttempt, ...] = ()


@dataclass(frozen=True, slots=True)
class AIReviewResult:
    analysis: AIAnalysis
    provider: str
    model: str
    status: str
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float
    latency_ms: int
    error: str = ""
    reviewed_at: datetime | None = None
    fallback_used: bool = False
    usage: dict[str, Any] | None = None
    attempts: tuple[AIRequestAttempt, ...] = ()

    @property
    def approved(self) -> bool:
        return self.status == "OK" and self.analysis.verdict in {
            AIVerdict.STRONG_APPROVE.value,
            AIVerdict.APPROVE.value,
        }

    @property
    def request_count(self) -> int:
        return len(self.attempts) or int(self.status == "OK")

    @property
    def error_count(self) -> int:
        return sum(attempt.status != "OK" for attempt in self.attempts) or int(self.status != "OK")


def _failure_analysis(reason: str) -> AIAnalysis:
    return AIAnalysis(
        verdict=AIVerdict.WAIT.value,
        score=0,
        analysis_confidence=AIConfidence.LOW.value,
        bull_case="Недоступно: AI review не завершён.",
        bear_case="Недоступно: AI review не завершён.",
        key_risks=[reason[:300]],
        why_now="Недоступно: требуется успешный structured AI review.",
        invalidation_conditions=["Не публиковать идею без AI second opinion."],
        short_summary="AI review недоступен; безопасный вердикт WAIT.",
    )


def structured_snapshot(
    candidate: TradingIdeaData,
    quality: QualityGateResult,
) -> dict[str, object]:
    components = candidate.factor_scores.get("technical_components", {})
    if not isinstance(components, dict):
        components = {}
    primary = candidate.relevant_indicators.get(candidate.primary_timeframe, {})
    if not isinstance(primary, dict):
        primary = {}
    return {
        "ticker": candidate.ticker,
        "direction": candidate.direction.value,
        "horizon": candidate.horizon.value,
        "current_price": candidate.current_price,
        "entry_zone": [candidate.entry_price_from, candidate.entry_price_to],
        "take_profit": candidate.take_profit,
        "stop_loss": candidate.stop_loss,
        "reward_risk_ratio": candidate.risk_reward_ratio,
        "technical_score": candidate.technical_score,
        "total_score": candidate.total_score,
        "trend_score": components.get("trend"),
        "momentum_score": components.get("momentum"),
        "momentum_extreme": candidate.momentum_extreme_score,
        "volume_score": candidate.volume_score,
        "levels_score": components.get("levels"),
        "volatility_score": components.get("volatility"),
        "relative_strength": candidate.relative_strength_score,
        "market_regime": candidate.regime or "unavailable",
        "imoex_trend_score": candidate.market_regime_score,
        "market_volatility": candidate.market_volatility or "unavailable",
        "fundamental_score": (
            candidate.fundamental_score if candidate.fundamental_publications else None
        ),
        "fundamental_components": (
            candidate.fundamental_components if candidate.fundamental_publications else {}
        ),
        "supporting_factors": list(quality.supporting_factors),
        "contradicting_factors": list(quality.contradicting_factors),
        "confirmation_count": quality.confirmation_count,
        "timeframe_confirmations": quality.timeframe_confirmations,
        "key_indicators": primary,
        "deterministic_rationale": candidate.rationale,
        "unavailable_blocks": [
            name
            for name, available in (
                ("fundamental", bool(candidate.fundamental_publications)),
                ("news", bool(candidate.news_score)),
                ("market_regime", bool(candidate.regime)),
            )
            if not available
        ],
    }


def _attempt(result: ProviderCallResult, *, fallback_used: bool) -> AIRequestAttempt:
    return AIRequestAttempt(
        provider=result.provider,
        model=result.model,
        status=result.status,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        estimated_cost_usd=result.estimated_cost_usd,
        latency_ms=result.latency_ms,
        error=result.error,
        status_code=result.status_code,
        error_code=result.error_code,
        model_unavailable=result.model_unavailable,
        fallback_used=fallback_used,
        usage=result.usage,
    )


def _attempt_error(attempts: tuple[AIRequestAttempt, ...]) -> str:
    return " | ".join(
        f"{attempt.provider}/{attempt.model}: {attempt.error}"
        for attempt in attempts
        if attempt.error
    )[:2_000]


def _attempt_usage(attempts: tuple[AIRequestAttempt, ...]) -> dict[str, Any]:
    return {
        "attempts": [
            {
                "provider": attempt.provider,
                "model": attempt.model,
                "fallback_used": attempt.fallback_used,
                "status_code": attempt.status_code,
                "error_code": attempt.error_code,
                "model_unavailable": attempt.model_unavailable,
                "usage": attempt.usage or {},
            }
            for attempt in attempts
        ]
    }


class AIAnalystService:
    """Provider-neutral structured AI second opinion with fail-closed semantics."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: AsyncHTTPClient | None = None,
        provider: AIProvider | None = None,
        fallback_provider: AIProvider | None = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.provider = provider or self._build_provider(settings.ai_model, fallback=False)
        self.fallback_provider = fallback_provider
        if (
            self.fallback_provider is None
            and settings.ai_provider == "gemini"
            and settings.ai_fallback_model != settings.ai_model
        ):
            self.fallback_provider = self._build_provider(
                settings.ai_fallback_model,
                fallback=True,
            )

    def _build_provider(self, model: str, *, fallback: bool) -> AIProvider:
        if self.settings.ai_provider == "gemini":
            return GeminiProvider(
                api_key=self.settings.gemini_api_key,
                base_url=self.settings.gemini_base_url,
                model=model,
                timeout_seconds=self.settings.ai_request_timeout_seconds,
                input_cost_per_million=(
                    self.settings.ai_fallback_input_cost_per_million
                    if fallback
                    else self.settings.ai_input_cost_per_million
                ),
                output_cost_per_million=(
                    self.settings.ai_fallback_output_cost_per_million
                    if fallback
                    else self.settings.ai_output_cost_per_million
                ),
                client=self.client,
                is_fallback=fallback,
            )
        return OpenAIProvider(
            api_key=self.settings.openai_api_key,
            base_url=self.settings.openai_base_url,
            model=model,
            timeout_seconds=self.settings.ai_request_timeout_seconds,
            input_cost_per_million=self.settings.ai_input_cost_per_million,
            output_cost_per_million=self.settings.ai_output_cost_per_million,
            client=self.client,
        )

    def _configured_failure(self, error: Exception | str) -> AIReviewResult:
        message = str(error) or type(error).__name__
        return AIReviewResult(
            analysis=_failure_analysis(message),
            provider=self.provider.name,
            model=self.provider.model,
            status="AI_NOT_REVIEWED",
            input_tokens=0,
            output_tokens=0,
            estimated_cost_usd=0.0,
            latency_ms=0,
            error=message[:2_000],
            reviewed_at=datetime.now(UTC),
        )

    async def _generate(
        self,
        *,
        system_prompt: str,
        payload: dict[str, object],
        schema: dict[str, Any],
        schema_name: str,
        max_output_tokens: int,
    ) -> tuple[ProviderCallResult, tuple[AIRequestAttempt, ...]]:
        primary = await self.provider.generate(
            system_prompt=system_prompt,
            payload=payload,
            schema=schema,
            schema_name=schema_name,
            max_output_tokens=max_output_tokens,
        )
        attempts = [_attempt(primary, fallback_used=False)]
        final = primary
        if primary.status != "OK" and primary.retryable and self.fallback_provider is not None:
            final = await self.fallback_provider.generate(
                system_prompt=system_prompt,
                payload=payload,
                schema=schema,
                schema_name=schema_name,
                max_output_tokens=max_output_tokens,
            )
            attempts.append(_attempt(final, fallback_used=True))
        return final, tuple(attempts)

    def _review_result(
        self,
        analysis: AIAnalysis,
        final: ProviderCallResult,
        attempts: tuple[AIRequestAttempt, ...],
    ) -> AIReviewResult:
        return AIReviewResult(
            analysis=analysis,
            provider=final.provider,
            model=final.model,
            status="OK",
            input_tokens=sum(attempt.input_tokens for attempt in attempts),
            output_tokens=sum(attempt.output_tokens for attempt in attempts),
            estimated_cost_usd=round(sum(attempt.estimated_cost_usd for attempt in attempts), 8),
            latency_ms=sum(attempt.latency_ms for attempt in attempts),
            error=_attempt_error(attempts),
            reviewed_at=datetime.now(UTC),
            fallback_used=len(attempts) > 1,
            usage=_attempt_usage(attempts),
            attempts=attempts,
        )

    def _failed_review(
        self,
        final: ProviderCallResult,
        attempts: tuple[AIRequestAttempt, ...],
    ) -> AIReviewResult:
        message = _attempt_error(attempts) or final.error or "AI provider unavailable"
        return AIReviewResult(
            analysis=_failure_analysis(message),
            provider=final.provider,
            model=final.model,
            status="AI_NOT_REVIEWED",
            input_tokens=sum(attempt.input_tokens for attempt in attempts),
            output_tokens=sum(attempt.output_tokens for attempt in attempts),
            estimated_cost_usd=round(sum(attempt.estimated_cost_usd for attempt in attempts), 8),
            latency_ms=sum(attempt.latency_ms for attempt in attempts),
            error=message[:2_000],
            reviewed_at=datetime.now(UTC),
            fallback_used=len(attempts) > 1,
            usage=_attempt_usage(attempts),
            attempts=attempts,
        )

    async def review(
        self,
        candidate: TradingIdeaData,
        quality: QualityGateResult,
    ) -> AIReviewResult:
        if quality.decision.value != "PASS":
            return self._configured_failure("AI must not review a non-PASS quantitative candidate")
        if not self.settings.ai_filter_enabled:
            return self._configured_failure("AI filter is disabled")

        final, attempts = await self._generate(
            system_prompt=SYSTEM_PROMPT,
            payload=structured_snapshot(candidate, quality),
            schema=AIAnalysis.model_json_schema(),
            schema_name="moex_ai_verdict",
            max_output_tokens=self.settings.ai_max_output_tokens,
        )
        if final.status != "OK":
            logger.warning(
                "AI review failed for %s: %s", candidate.ticker, _attempt_error(attempts)
            )
            return self._failed_review(final, attempts)
        try:
            analysis = AIAnalysis.model_validate_json(final.text)
        except (ValueError, ValidationError) as error:
            invalid = replace(
                attempts[-1],
                status="ERROR",
                error=f"structured response validation failed: {error}"[:2_000],
            )
            attempts = (*attempts[:-1], invalid)
            logger.warning("AI review schema validation failed for %s: %s", candidate.ticker, error)
            return self._failed_review(final, attempts)
        return self._review_result(analysis, final, attempts)

    async def review_current(
        self,
        candidate: TradingIdeaData,
        quality: QualityGateResult,
    ) -> AIReviewResult:
        """Use the normal provider contract for an explicit, non-publishing current review."""
        payload = structured_snapshot(candidate, quality)
        payload.update(
            {
                "quality_gate_result": quality.decision.value,
                "quality_gate_reasons": list(quality.reasons),
            }
        )
        final, attempts = await self._generate(
            system_prompt=SYSTEM_PROMPT,
            payload=payload,
            schema=AIAnalysis.model_json_schema(),
            schema_name="moex_ai_verdict",
            max_output_tokens=self.settings.ai_max_output_tokens,
        )
        if final.status != "OK":
            logger.warning(
                "Current AI review failed for %s: %s",
                candidate.ticker,
                _attempt_error(attempts),
            )
            return self._failed_review(final, attempts)
        try:
            analysis = AIAnalysis.model_validate_json(final.text)
        except (ValueError, ValidationError) as error:
            invalid = replace(
                attempts[-1],
                status="ERROR",
                error=f"structured response validation failed: {error}"[:2_000],
            )
            attempts = (*attempts[:-1], invalid)
            logger.warning(
                "Current AI review schema validation failed for %s: %s",
                candidate.ticker,
                error,
            )
            return self._failed_review(final, attempts)
        return self._review_result(analysis, final, attempts)

    async def summarize_market(self, market_snapshot: dict[str, object]) -> MarketAIReviewResult:
        unavailable = "AI summary unavailable; deterministic market metrics remain available."
        final, attempts = await self._generate(
            system_prompt=(
                "Explain the supplied calculated MOEX market snapshot in 2-4 concise "
                "sentences. Use only supplied values. Do not invent news, events, prices, "
                "fundamentals, or forecasts. Missing data is unavailable."
            ),
            payload=market_snapshot,
            schema=MarketAIAnalysis.model_json_schema(),
            schema_name="moex_market_summary",
            max_output_tokens=min(self.settings.ai_max_output_tokens, 300),
        )
        analysis: MarketAIAnalysis | None = None
        if final.status == "OK":
            try:
                analysis = MarketAIAnalysis.model_validate_json(final.text)
            except (ValueError, ValidationError) as error:
                attempts = (
                    *attempts[:-1],
                    replace(
                        attempts[-1],
                        status="ERROR",
                        error=f"structured response validation failed: {error}"[:2_000],
                    ),
                )
        status = "OK" if analysis is not None else "AI_NOT_REVIEWED"
        error = _attempt_error(attempts)
        return MarketAIReviewResult(
            summary=analysis.short_summary if analysis is not None else unavailable,
            provider=final.provider,
            model=final.model,
            status=status,
            input_tokens=sum(attempt.input_tokens for attempt in attempts),
            output_tokens=sum(attempt.output_tokens for attempt in attempts),
            estimated_cost_usd=round(sum(attempt.estimated_cost_usd for attempt in attempts), 8),
            latency_ms=sum(attempt.latency_ms for attempt in attempts),
            error=error,
            reviewed_at=datetime.now(UTC),
            fallback_used=len(attempts) > 1,
            usage=_attempt_usage(attempts),
            attempts=attempts,
        )


def apply_ai_review(candidate: TradingIdeaData, review: AIReviewResult) -> TradingIdeaData:
    analysis = review.analysis
    candidate.ai_verdict = analysis.verdict
    candidate.ai_score = analysis.score
    candidate.ai_confidence = analysis.analysis_confidence
    candidate.ai_bull_case = analysis.bull_case
    candidate.ai_bear_case = analysis.bear_case
    candidate.ai_key_risks = list(analysis.key_risks)
    candidate.ai_why_now = analysis.why_now
    candidate.ai_invalidation_conditions = list(analysis.invalidation_conditions)
    candidate.ai_short_summary = analysis.short_summary
    candidate.ai_provider = review.provider
    candidate.ai_model = review.model
    candidate.ai_reviewed_at = review.reviewed_at
    return candidate
