from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, TypeVar

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

RUSSIAN_OUTPUT_INSTRUCTION = """Все текстовые значения JSON, предназначенные для
пользователя, пиши исключительно на русском языке. Не используй английский язык в
rationale, reasons, risks, why_now, invalidation и других описательных полях. Биржевые
тикеры, названия индикаторов и общепринятые обозначения вроде RSI, EMA20, EMA50, R:R,
BUY, SELL можно оставлять без перевода."""

SYSTEM_PROMPT = f"""You are a conservative second-opinion analyst for MOEX paper ideas.
Use ONLY the structured data supplied by the application. Never invent news, financial
figures, price levels, prices, events, forecasts, or missing indicators. Treat every
missing block as unavailable. Quantitative filters have already run: you may reject or
wait, but you must not rescue a weak quantitative signal. Focus on contradictions,
timing, invalidation, and whether the evidence supports this direction now. Keep every
text field concise. Scores are analytical ratings, never calibrated probabilities.
Return only the requested structured result.

{RUSSIAN_OUTPUT_INSTRUCTION}"""

STRUCTURED_RETRY_SUFFIX = """
The previous response was incomplete or failed schema validation. Retry once from the
supplied source data. Return exactly one complete JSON object and no surrounding text.
Include every required field. Keep each scalar text field under 220 characters and each
list item under 120 characters. Never copy or continue the previous partial response."""
LANGUAGE_RETRY_SUFFIX = """
LANGUAGE_MISMATCH: перепиши все пользовательские текстовые значения исключительно
на русском языке. Не используй английские предложения или описания. Тикеры, BUY,
SELL, RSI, EMA/SMA, IMOEX, R:R, числа и другие технические обозначения можно оставить
без перевода. Верни один полный JSON-объект со всеми обязательными полями."""
INVALID_STRUCTURED_RESPONSE = "INVALID_STRUCTURED_RESPONSE"
LANGUAGE_MISMATCH = "LANGUAGE_MISMATCH"
AnalysisModelT = TypeVar("AnalysisModelT", bound=BaseModel)


class AIAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: Literal["STRONG_APPROVE", "APPROVE", "WAIT", "REJECT"]
    score: float = Field(ge=0, le=100)
    analysis_confidence: Literal["LOW", "MEDIUM", "HIGH"]
    bull_case: str = Field(
        min_length=1,
        max_length=700,
        description="Russian language only. Аргументы в пользу сценария.",
    )
    bear_case: str = Field(
        min_length=1,
        max_length=700,
        description="Russian language only. Аргументы против сценария.",
    )
    key_risks: list[ShortItem] = Field(
        min_length=1,
        max_length=3,
        description="Russian language only. Главные риски без английских предложений.",
    )
    why_now: str = Field(
        min_length=1,
        max_length=700,
        description="Russian language only. Почему сценарий актуален сейчас.",
    )
    invalidation_conditions: list[ShortItem] = Field(
        min_length=1,
        max_length=3,
        description="Russian language only. Условия отмены или инвалидации сценария.",
    )
    short_summary: str = Field(
        min_length=1,
        max_length=700,
        description="Russian language only. Краткое пользовательское резюме.",
    )

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

    short_summary: str = Field(
        min_length=1,
        max_length=700,
        description="Russian language only. Краткое пользовательское резюме рынка.",
    )


_LATIN_WORD_RE = re.compile(r"[A-Za-z]+(?:\d+)?")
_CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
# MOEX tickers are short uppercase symbols; indicator tokens may additionally
# contain a numeric period (EMA20, SMA200). Longer uppercase prose must still be
# counted as Latin text instead of being mistaken for a technical abbreviation.
_TECHNICAL_TOKEN_RE = re.compile(r"^(?:[A-Z]{1,5}|[A-Z]{1,5}\d{1,4})$")


def _language_counts(text: str) -> tuple[int, int]:
    cyrillic_chars = len(_CYRILLIC_RE.findall(text))
    latin_prose_chars = sum(
        len(word)
        for word in _LATIN_WORD_RE.findall(text)
        if not _TECHNICAL_TOKEN_RE.fullmatch(word)
    )
    return cyrillic_chars, latin_prose_chars


def _field_is_predominantly_non_russian(text: str) -> bool:
    cyrillic_chars, latin_prose_chars = _language_counts(text)
    if cyrillic_chars == 0 and _LATIN_WORD_RE.search(text):
        return True
    return latin_prose_chars >= 8 and latin_prose_chars > cyrillic_chars


def _language_mismatch_fields(analysis: BaseModel) -> tuple[str, ...]:
    values: list[tuple[str, str]] = []
    if isinstance(analysis, AIAnalysis):
        values.extend(
            [
                ("bull_case", analysis.bull_case),
                ("bear_case", analysis.bear_case),
                ("why_now", analysis.why_now),
                ("short_summary", analysis.short_summary),
            ]
        )
        values.extend(
            (f"key_risks[{index}]", value)
            for index, value in enumerate(analysis.key_risks)
        )
        values.extend(
            (f"invalidation_conditions[{index}]", value)
            for index, value in enumerate(analysis.invalidation_conditions)
        )
    elif isinstance(analysis, MarketAIAnalysis):
        values.append(("short_summary", analysis.short_summary))
    return tuple(name for name, value in values if _field_is_predominantly_non_russian(value))


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
    retry_stage: str = "PRIMARY"
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


def _attempt(
    result: ProviderCallResult,
    *,
    fallback_used: bool,
    retry_stage: str,
) -> AIRequestAttempt:
    usage = dict(result.usage)
    usage["retry_stage"] = retry_stage
    usage["fallback_used"] = fallback_used
    usage["status_code"] = result.status_code
    usage["error_code"] = result.error_code
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
        retry_stage=retry_stage,
        usage=usage,
    )


def _structured_validation_error(error: Exception, *, response_chars: int) -> str:
    issues: list[str] = []
    if isinstance(error, ValidationError):
        for item in error.errors(include_url=False, include_input=False)[:5]:
            location = ".".join(str(part) for part in item.get("loc", ())) or "root"
            issues.append(f"{location}: {item.get('msg', 'validation error')}")
    if not issues:
        issues.append(type(error).__name__)
    detail = "; ".join(issues)
    return (
        f"structured response validation failed: {INVALID_STRUCTURED_RESPONSE}; "
        f"response_chars={response_chars}; {detail}"
    )[:1_000]


def _validate_structured_result(
    result: ProviderCallResult,
    response_model: type[AnalysisModelT],
) -> tuple[AnalysisModelT | None, ProviderCallResult]:
    if result.status != "OK":
        return None, result
    try:
        analysis = response_model.model_validate_json(result.text)
    except (ValueError, ValidationError) as error:
        usage = dict(result.usage)
        usage["structured_response_valid"] = False
        usage["responseChars"] = len(result.text)
        usage["status_code"] = result.status_code
        usage["error_code"] = INVALID_STRUCTURED_RESPONSE
        return None, replace(
            result,
            status="ERROR",
            error=_structured_validation_error(error, response_chars=len(result.text)),
            error_code=INVALID_STRUCTURED_RESPONSE,
            retryable=True,
            usage=usage,
        )
    mismatch_fields = _language_mismatch_fields(analysis)
    if mismatch_fields:
        usage = dict(result.usage)
        usage["structured_response_valid"] = True
        usage["language_valid"] = False
        usage["language_mismatch_fields"] = list(mismatch_fields)
        usage["status_code"] = result.status_code
        usage["error_code"] = LANGUAGE_MISMATCH
        return None, replace(
            result,
            status="ERROR",
            error=(
                f"{LANGUAGE_MISMATCH}: user-facing fields must be Russian; "
                f"fields={','.join(mismatch_fields)}"
            )[:1_000],
            error_code=LANGUAGE_MISMATCH,
            retryable=True,
            usage=usage,
        )
    return analysis, result


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
                "retry_stage": attempt.retry_stage,
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

    async def _call_provider(
        self,
        *,
        provider: AIProvider,
        system_prompt: str,
        payload: dict[str, object],
        schema: dict[str, Any],
        schema_name: str,
        max_output_tokens: int,
    ) -> ProviderCallResult:
        return await provider.generate(
            system_prompt=system_prompt,
            payload=payload,
            schema=schema,
            schema_name=schema_name,
            max_output_tokens=max_output_tokens,
        )

    async def _call_and_validate(
        self,
        *,
        provider: AIProvider,
        response_model: type[AnalysisModelT],
        system_prompt: str,
        payload: dict[str, object],
        schema: dict[str, Any],
        schema_name: str,
        max_output_tokens: int,
        fallback_used: bool,
        retry_stage: str,
    ) -> tuple[AnalysisModelT | None, ProviderCallResult, AIRequestAttempt]:
        raw = await self._call_provider(
            provider=provider,
            system_prompt=system_prompt,
            payload=payload,
            schema=schema,
            schema_name=schema_name,
            max_output_tokens=max_output_tokens,
        )
        analysis, checked = _validate_structured_result(raw, response_model)
        attempt = _attempt(
            checked,
            fallback_used=fallback_used,
            retry_stage=retry_stage,
        )
        if checked.error_code in {INVALID_STRUCTURED_RESPONSE, LANGUAGE_MISMATCH}:
            logger.warning(
                "AI response validation failed provider=%s model=%s stage=%s "
                "error_code=%s finish_reason=%s response_chars=%s",
                checked.provider,
                checked.model,
                retry_stage,
                checked.error_code,
                checked.usage.get("finishReason", "unknown"),
                checked.usage.get("responseChars", 0),
            )
        return analysis, checked, attempt

    async def _generate_structured(
        self,
        *,
        response_model: type[AnalysisModelT],
        system_prompt: str,
        payload: dict[str, object],
        schema: dict[str, Any],
        schema_name: str,
        max_output_tokens: int,
    ) -> tuple[
        AnalysisModelT | None,
        ProviderCallResult,
        tuple[AIRequestAttempt, ...],
    ]:
        analysis, primary, primary_attempt = await self._call_and_validate(
            provider=self.provider,
            response_model=response_model,
            system_prompt=system_prompt,
            payload=payload,
            schema=schema,
            schema_name=schema_name,
            max_output_tokens=max_output_tokens,
            fallback_used=False,
            retry_stage="PRIMARY",
        )
        attempts = [primary_attempt]
        if analysis is not None:
            return analysis, primary, tuple(attempts)

        primary_validation_error = primary.error_code in {
            INVALID_STRUCTURED_RESPONSE,
            LANGUAGE_MISMATCH,
        }
        retry_suffix = (
            LANGUAGE_RETRY_SUFFIX
            if primary.error_code == LANGUAGE_MISMATCH
            else STRUCTURED_RETRY_SUFFIX
        )
        retry_stage = (
            "PRIMARY_LANGUAGE_RETRY"
            if primary.error_code == LANGUAGE_MISMATCH
            else "PRIMARY_STRUCTURED_RETRY"
        )
        if primary_validation_error and self.provider.name == "gemini":
            analysis, retry, retry_attempt = await self._call_and_validate(
                provider=self.provider,
                response_model=response_model,
                system_prompt=f"{system_prompt}\n\n{retry_suffix}",
                payload=payload,
                schema=schema,
                schema_name=schema_name,
                max_output_tokens=max_output_tokens,
                fallback_used=False,
                retry_stage=retry_stage,
            )
            attempts.append(retry_attempt)
            if analysis is not None:
                return analysis, retry, tuple(attempts)
            primary = retry

        should_fallback = bool(
            self.fallback_provider is not None
            and (
                primary_validation_error
                or (len(attempts) == 1 and primary.status != "OK" and primary.retryable)
            )
        )
        if should_fallback and self.fallback_provider is not None:
            fallback_prompt = (
                f"{system_prompt}\n\n{retry_suffix}"
                if primary_validation_error
                else system_prompt
            )
            analysis, fallback, fallback_attempt = await self._call_and_validate(
                provider=self.fallback_provider,
                response_model=response_model,
                system_prompt=fallback_prompt,
                payload=payload,
                schema=schema,
                schema_name=schema_name,
                max_output_tokens=max_output_tokens,
                fallback_used=True,
                retry_stage="FALLBACK",
            )
            attempts.append(fallback_attempt)
            return analysis, fallback, tuple(attempts)
        return None, primary, tuple(attempts)

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
            fallback_used=any(attempt.fallback_used for attempt in attempts),
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
            fallback_used=any(attempt.fallback_used for attempt in attempts),
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

        analysis, final, attempts = await self._generate_structured(
            response_model=AIAnalysis,
            system_prompt=SYSTEM_PROMPT,
            payload=structured_snapshot(candidate, quality),
            schema=AIAnalysis.model_json_schema(),
            schema_name="moex_ai_verdict",
            max_output_tokens=self.settings.ai_max_output_tokens,
        )
        if analysis is None:
            logger.warning(
                "AI review failed for %s: %s", candidate.ticker, _attempt_error(attempts)
            )
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
        analysis, final, attempts = await self._generate_structured(
            response_model=AIAnalysis,
            system_prompt=SYSTEM_PROMPT,
            payload=payload,
            schema=AIAnalysis.model_json_schema(),
            schema_name="moex_ai_verdict",
            max_output_tokens=self.settings.ai_max_output_tokens,
        )
        if analysis is None:
            logger.warning(
                "Current AI review failed for %s: %s",
                candidate.ticker,
                _attempt_error(attempts),
            )
            return self._failed_review(final, attempts)
        return self._review_result(analysis, final, attempts)

    async def summarize_market(self, market_snapshot: dict[str, object]) -> MarketAIReviewResult:
        unavailable = "AI summary unavailable; deterministic market metrics remain available."
        analysis, final, attempts = await self._generate_structured(
            response_model=MarketAIAnalysis,
            system_prompt=(
                "Explain the supplied calculated MOEX market snapshot in 2-4 concise "
                "sentences. Use only supplied values. Do not invent news, events, prices, "
                "fundamentals, or forecasts. Missing data is unavailable.\n\n"
                f"{RUSSIAN_OUTPUT_INSTRUCTION}"
            ),
            payload=market_snapshot,
            schema=MarketAIAnalysis.model_json_schema(),
            schema_name="moex_market_summary",
            max_output_tokens=min(self.settings.ai_max_output_tokens, 1_024),
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
            fallback_used=any(attempt.fallback_used for attempt in attempts),
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
