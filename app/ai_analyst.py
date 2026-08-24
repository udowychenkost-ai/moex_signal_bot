from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from typing import Annotated, Any, Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

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
    ai_score: float = Field(ge=0, le=100)
    confidence_in_analysis: Literal["LOW", "MEDIUM", "HIGH"]
    bull_case: str = Field(min_length=1, max_length=700)
    bear_case: str = Field(min_length=1, max_length=700)
    key_risks: list[ShortItem] = Field(min_length=1, max_length=3)
    why_now: str = Field(min_length=1, max_length=700)
    invalidation_conditions: list[ShortItem] = Field(min_length=1, max_length=3)
    short_summary: str = Field(min_length=1, max_length=700)


class MarketAIAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    short_summary: str = Field(min_length=1, max_length=700)


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

    @property
    def approved(self) -> bool:
        return self.analysis.verdict in {
            AIVerdict.STRONG_APPROVE.value,
            AIVerdict.APPROVE.value,
        }


class AsyncHTTPClient(Protocol):
    async def post(self, url: str, **kwargs: Any) -> Any: ...


def _failure_analysis(reason: str) -> AIAnalysis:
    return AIAnalysis(
        verdict=AIVerdict.WAIT.value,
        ai_score=0,
        confidence_in_analysis=AIConfidence.LOW.value,
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


def _response_text(payload: dict[str, Any]) -> str:
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct
    for output in payload.get("output", []):
        if not isinstance(output, dict):
            continue
        for content in output.get("content", []):
            if not isinstance(content, dict):
                continue
            if content.get("type") == "refusal":
                raise ValueError(f"model refusal: {content.get('refusal', 'unspecified')}")
            text = content.get("text")
            if content.get("type") == "output_text" and isinstance(text, str):
                return text
    raise ValueError("OpenAI response did not contain output_text")


class AIAnalystService:
    """OpenAI Responses API adapter with strict schema and fail-closed semantics."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: AsyncHTTPClient | None = None,
    ) -> None:
        self.settings = settings
        self.client = client

    def _failure(self, error: Exception | str, latency_ms: int = 0) -> AIReviewResult:
        message = str(error) or type(error).__name__
        return AIReviewResult(
            analysis=_failure_analysis(message),
            provider=self.settings.ai_provider,
            model=self.settings.ai_model,
            status="ERROR",
            input_tokens=0,
            output_tokens=0,
            estimated_cost_usd=0.0,
            latency_ms=latency_ms,
            error=message[:2_000],
            reviewed_at=datetime.now(UTC),
        )

    async def review(
        self,
        candidate: TradingIdeaData,
        quality: QualityGateResult,
    ) -> AIReviewResult:
        if quality.decision.value != "PASS":
            return self._failure("AI must not review a non-PASS quantitative candidate")
        if not self.settings.ai_filter_enabled:
            return self._failure("AI filter is disabled")
        if not self.settings.openai_api_key:
            return self._failure("OPENAI_API_KEY is not configured")

        schema = AIAnalysis.model_json_schema()
        request = {
            "model": self.settings.ai_model,
            "max_output_tokens": self.settings.ai_max_output_tokens,
            "instructions": SYSTEM_PROMPT,
            "input": json.dumps(
                structured_snapshot(candidate, quality),
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
            ),
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "moex_ai_verdict",
                    "strict": True,
                    "schema": schema,
                }
            },
        }
        started = perf_counter()
        own_client = self.client is None
        client: AsyncHTTPClient = self.client or httpx.AsyncClient(
            timeout=self.settings.ai_request_timeout_seconds
        )
        try:
            response = await client.post(
                f"{self.settings.openai_base_url.rstrip('/')}/responses",
                headers={
                    "Authorization": f"Bearer {self.settings.openai_api_key}",
                    "Content-Type": "application/json",
                },
                json=request,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("OpenAI response must be a JSON object")
            analysis = AIAnalysis.model_validate_json(_response_text(payload))
            usage = payload.get("usage") or {}
            input_tokens = int(usage.get("input_tokens") or 0)
            output_tokens = int(usage.get("output_tokens") or 0)
            cost = (
                input_tokens * self.settings.ai_input_cost_per_million
                + output_tokens * self.settings.ai_output_cost_per_million
            ) / 1_000_000
            return AIReviewResult(
                analysis=analysis,
                provider=self.settings.ai_provider,
                model=self.settings.ai_model,
                status="OK",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_cost_usd=round(cost, 8),
                latency_ms=round((perf_counter() - started) * 1_000),
                reviewed_at=datetime.now(UTC),
            )
        except (httpx.HTTPError, TimeoutError, ValueError, ValidationError) as error:
            latency = round((perf_counter() - started) * 1_000)
            logger.warning("AI review failed for %s: %s", candidate.ticker, error)
            return self._failure(error, latency)
        except Exception as error:
            latency = round((perf_counter() - started) * 1_000)
            logger.exception("Unexpected AI review failure for %s", candidate.ticker)
            return self._failure(error, latency)
        finally:
            if own_client and isinstance(client, httpx.AsyncClient):
                await client.aclose()

    async def summarize_market(self, market_snapshot: dict[str, object]) -> MarketAIReviewResult:
        unavailable = "AI summary unavailable; deterministic market metrics remain available."
        if not self.settings.openai_api_key:
            return MarketAIReviewResult(
                summary=unavailable,
                provider=self.settings.ai_provider,
                model=self.settings.ai_model,
                status="ERROR",
                input_tokens=0,
                output_tokens=0,
                estimated_cost_usd=0,
                latency_ms=0,
                error="OPENAI_API_KEY is not configured",
                reviewed_at=datetime.now(UTC),
            )
        request = {
            "model": self.settings.ai_model,
            "max_output_tokens": min(self.settings.ai_max_output_tokens, 300),
            "instructions": (
                "Explain the supplied calculated MOEX market snapshot in 2-4 concise "
                "sentences. Use only supplied values. Do not invent news, events, prices, "
                "fundamentals, or forecasts. Missing data is unavailable."
            ),
            "input": json.dumps(
                market_snapshot, ensure_ascii=False, sort_keys=True, allow_nan=False
            ),
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "moex_market_summary",
                    "strict": True,
                    "schema": MarketAIAnalysis.model_json_schema(),
                }
            },
        }
        started = perf_counter()
        own_client = self.client is None
        client: AsyncHTTPClient = self.client or httpx.AsyncClient(
            timeout=self.settings.ai_request_timeout_seconds
        )
        try:
            response = await client.post(
                f"{self.settings.openai_base_url.rstrip('/')}/responses",
                headers={
                    "Authorization": f"Bearer {self.settings.openai_api_key}",
                    "Content-Type": "application/json",
                },
                json=request,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("OpenAI response must be a JSON object")
            analysis = MarketAIAnalysis.model_validate_json(_response_text(payload))
            usage = payload.get("usage") or {}
            input_tokens = int(usage.get("input_tokens") or 0)
            output_tokens = int(usage.get("output_tokens") or 0)
            cost = (
                input_tokens * self.settings.ai_input_cost_per_million
                + output_tokens * self.settings.ai_output_cost_per_million
            ) / 1_000_000
            return MarketAIReviewResult(
                summary=analysis.short_summary,
                provider=self.settings.ai_provider,
                model=self.settings.ai_model,
                status="OK",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_cost_usd=round(cost, 8),
                latency_ms=round((perf_counter() - started) * 1_000),
                reviewed_at=datetime.now(UTC),
            )
        except Exception as error:
            logger.warning("AI market summary failed: %s", error)
            return MarketAIReviewResult(
                summary=unavailable,
                provider=self.settings.ai_provider,
                model=self.settings.ai_model,
                status="ERROR",
                input_tokens=0,
                output_tokens=0,
                estimated_cost_usd=0,
                latency_ms=round((perf_counter() - started) * 1_000),
                error=str(error)[:2_000],
                reviewed_at=datetime.now(UTC),
            )
        finally:
            if own_client and isinstance(client, httpx.AsyncClient):
                await client.aclose()


def apply_ai_review(candidate: TradingIdeaData, review: AIReviewResult) -> TradingIdeaData:
    analysis = review.analysis
    candidate.ai_verdict = analysis.verdict
    candidate.ai_score = analysis.ai_score
    candidate.ai_confidence = analysis.confidence_in_analysis
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
