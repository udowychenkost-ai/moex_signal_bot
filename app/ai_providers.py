from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import perf_counter
from typing import Any, Protocol
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)


class AsyncHTTPClient(Protocol):
    async def get(self, url: str, **kwargs: Any) -> Any: ...

    async def post(self, url: str, **kwargs: Any) -> Any: ...


@dataclass(frozen=True, slots=True)
class ProviderCallResult:
    provider: str
    model: str
    status: str
    text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    latency_ms: int = 0
    error: str = ""
    status_code: int | None = None
    error_code: str = ""
    model_unavailable: bool = False
    retryable: bool = False
    usage: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GeminiHealthStatus:
    provider: str
    configured_model: str
    model_available: bool
    api_reachable: bool
    status_code: int | None
    error_code: str
    error_message: str
    checked_at: datetime


class AIProvider(Protocol):
    name: str
    model: str

    async def generate(
        self,
        *,
        system_prompt: str,
        payload: dict[str, object],
        schema: dict[str, Any],
        schema_name: str,
        max_output_tokens: int,
    ) -> ProviderCallResult: ...


def _openai_response_text(payload: dict[str, Any]) -> str:
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
                refusal = content.get("refusal", "unspecified")
                raise ValueError(f"model refusal: {refusal}")
            text = content.get("text")
            if content.get("type") == "output_text" and isinstance(text, str):
                return text
    raise ValueError("OpenAI response did not contain output_text")


def _gemini_response_text(payload: dict[str, Any]) -> str:
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        block_reason = (payload.get("promptFeedback") or {}).get("blockReason")
        suffix = f": {block_reason}" if block_reason else ""
        raise ValueError(f"Gemini response did not contain candidates{suffix}")
    first = candidates[0]
    if not isinstance(first, dict):
        raise ValueError("Gemini candidate must be a JSON object")
    content = first.get("content") or {}
    parts = content.get("parts") if isinstance(content, dict) else None
    texts = [
        part["text"]
        for part in parts or []
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    ]
    text = "".join(texts).strip()
    if not text:
        finish_reason = first.get("finishReason", "unspecified")
        raise ValueError(f"Gemini response did not contain text: {finish_reason}")
    return text


def _gemini_schema(value: Any) -> Any:
    """Remove JSON Schema keywords unsupported by Gemini structured output."""
    if isinstance(value, list):
        return [_gemini_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    unsupported = {
        "$schema",
        "default",
        "examples",
        "exclusiveMaximum",
        "exclusiveMinimum",
        "maxLength",
        "minLength",
        "multipleOf",
        "pattern",
    }
    return {key: _gemini_schema(item) for key, item in value.items() if key not in unsupported}


def _normalized_model(model: str) -> str:
    value = model.strip()
    return value.removeprefix("models/")


def _normalized_gemini_base_url(base_url: str) -> str:
    value = base_url.rstrip("/")
    return value.removesuffix("/models")


def _google_error(response: Any) -> tuple[int | None, str, str, bool]:
    status_code = getattr(response, "status_code", None)
    payload: dict[str, Any] = {}
    try:
        parsed = response.json()
        if isinstance(parsed, dict):
            payload = parsed
    except Exception:
        pass
    body = payload.get("error")
    body = body if isinstance(body, dict) else {}
    message = str(body.get("message") or "Gemini API request failed").strip()
    google_status = str(body.get("status") or "").strip().upper()
    reason = ""
    details = body.get("details")
    if isinstance(details, list):
        for detail in details:
            if not isinstance(detail, dict):
                continue
            candidate = detail.get("reason")
            if isinstance(candidate, str) and candidate.strip():
                reason = candidate.strip().upper()
                break
    lowered = message.lower()
    model_unavailable = bool(
        status_code == 404
        or "model not found" in lowered
        or "model is not found" in lowered
        or ("model" in lowered and "not supported" in lowered)
    )
    if model_unavailable:
        error_code = "MODEL_NOT_FOUND" if status_code == 404 else "MODEL_UNSUPPORTED"
    else:
        error_code = reason or google_status or (f"HTTP_{status_code}" if status_code else "ERROR")
    return status_code, error_code, message[:1_000], model_unavailable


class OpenAIProvider:
    name = "openai"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout_seconds: float,
        input_cost_per_million: float,
        output_cost_per_million: float,
        client: AsyncHTTPClient | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.input_cost_per_million = input_cost_per_million
        self.output_cost_per_million = output_cost_per_million
        self.client = client

    async def generate(
        self,
        *,
        system_prompt: str,
        payload: dict[str, object],
        schema: dict[str, Any],
        schema_name: str,
        max_output_tokens: int,
    ) -> ProviderCallResult:
        if not self.api_key:
            return self._error("OPENAI_API_KEY is not configured")
        request = {
            "model": self.model,
            "max_output_tokens": max_output_tokens,
            "instructions": system_prompt,
            "input": json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False),
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                }
            },
        }
        started = perf_counter()
        own_client = self.client is None
        client: AsyncHTTPClient = self.client or httpx.AsyncClient(timeout=self.timeout_seconds)
        try:
            response = await client.post(
                f"{self.base_url}/responses",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=request,
            )
            response.raise_for_status()
            response_payload = response.json()
            if not isinstance(response_payload, dict):
                raise ValueError("OpenAI response must be a JSON object")
            usage = response_payload.get("usage") or {}
            usage = usage if isinstance(usage, dict) else {}
            input_tokens = int(usage.get("input_tokens") or 0)
            output_tokens = int(usage.get("output_tokens") or 0)
            cost = (
                input_tokens * self.input_cost_per_million
                + output_tokens * self.output_cost_per_million
            ) / 1_000_000
            exact_model = response_payload.get("model")
            return ProviderCallResult(
                provider=self.name,
                model=exact_model if isinstance(exact_model, str) else self.model,
                status="OK",
                text=_openai_response_text(response_payload),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_cost_usd=round(cost, 8),
                latency_ms=round((perf_counter() - started) * 1_000),
                usage=usage,
            )
        except (httpx.HTTPError, TimeoutError, ValueError) as error:
            retryable = isinstance(error, (httpx.TimeoutException, TimeoutError))
            return self._error(
                error,
                latency_ms=round((perf_counter() - started) * 1_000),
                retryable=retryable,
            )
        except Exception as error:
            return self._error(error, latency_ms=round((perf_counter() - started) * 1_000))
        finally:
            if own_client and isinstance(client, httpx.AsyncClient):
                await client.aclose()

    def _error(
        self,
        error: Exception | str,
        *,
        latency_ms: int = 0,
        retryable: bool = False,
    ) -> ProviderCallResult:
        message = str(error) or type(error).__name__
        return ProviderCallResult(
            provider=self.name,
            model=self.model,
            status="ERROR",
            latency_ms=latency_ms,
            error=message[:2_000],
            retryable=retryable,
        )


class GeminiProvider:
    name = "gemini"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout_seconds: float,
        input_cost_per_million: float,
        output_cost_per_million: float,
        client: AsyncHTTPClient | None = None,
        is_fallback: bool = False,
    ) -> None:
        self.api_key = api_key
        self.base_url = _normalized_gemini_base_url(base_url)
        self.model = _normalized_model(model)
        self.timeout_seconds = timeout_seconds
        self.input_cost_per_million = input_cost_per_million
        self.output_cost_per_million = output_cost_per_million
        self.client = client
        self.is_fallback = is_fallback
        self.last_health: GeminiHealthStatus | None = None

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "x-goog-api-key": self.api_key,
            "Content-Type": "application/json",
        }

    async def check_health(self) -> GeminiHealthStatus:
        """Verify API reachability and model support through the official ListModels API."""
        checked_at = datetime.now(UTC)
        if not self.api_key:
            result = GeminiHealthStatus(
                provider=self.name,
                configured_model=self.model,
                model_available=False,
                api_reachable=False,
                status_code=None,
                error_code="MISSING_API_KEY",
                error_message="GEMINI_API_KEY is not configured",
                checked_at=checked_at,
            )
            self.last_health = result
            return result

        own_client = self.client is None
        client: AsyncHTTPClient = self.client or httpx.AsyncClient(timeout=self.timeout_seconds)
        try:
            response = await client.get(
                f"{self.base_url}/models",
                headers=self._headers,
                params={"pageSize": 1000},
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("Gemini ListModels response must be a JSON object")
            models = payload.get("models")
            models = models if isinstance(models, list) else []
            configured: dict[str, Any] | None = None
            for item in models:
                if not isinstance(item, dict):
                    continue
                name = item.get("name")
                if isinstance(name, str) and _normalized_model(name) == self.model:
                    configured = item
                    break
            methods: list[str] = []
            if configured is not None:
                raw_methods = configured.get("supportedGenerationMethods")
                if not isinstance(raw_methods, list):
                    raw_methods = configured.get("supportedActions")
                if isinstance(raw_methods, list):
                    methods = [str(item) for item in raw_methods]
            available = configured is not None and (not methods or "generateContent" in methods)
            if available:
                error_code = ""
                error_message = ""
            elif configured is None:
                error_code = "MODEL_NOT_FOUND"
                error_message = "Configured model is absent from Gemini ListModels response"
            else:
                error_code = "MODEL_UNSUPPORTED"
                error_message = "Configured model does not support generateContent"
            result = GeminiHealthStatus(
                provider=self.name,
                configured_model=self.model,
                model_available=available,
                api_reachable=True,
                status_code=int(getattr(response, "status_code", 200)),
                error_code=error_code,
                error_message=error_message,
                checked_at=checked_at,
            )
        except httpx.HTTPStatusError as error:
            status_code, error_code, message, _ = _google_error(error.response)
            result = GeminiHealthStatus(
                provider=self.name,
                configured_model=self.model,
                model_available=False,
                api_reachable=True,
                status_code=status_code,
                error_code=error_code,
                error_message=message,
                checked_at=checked_at,
            )
        except (httpx.TimeoutException, TimeoutError) as error:
            result = GeminiHealthStatus(
                provider=self.name,
                configured_model=self.model,
                model_available=False,
                api_reachable=False,
                status_code=None,
                error_code="TIMEOUT",
                error_message=(str(error) or "Gemini ListModels timed out")[:1_000],
                checked_at=checked_at,
            )
        except Exception as error:
            result = GeminiHealthStatus(
                provider=self.name,
                configured_model=self.model,
                model_available=False,
                api_reachable=False,
                status_code=None,
                error_code="NETWORK_ERROR",
                error_message=(str(error) or type(error).__name__)[:1_000],
                checked_at=checked_at,
            )
        finally:
            if own_client and isinstance(client, httpx.AsyncClient):
                await client.aclose()
        self.last_health = result
        return result

    async def generate(
        self,
        *,
        system_prompt: str,
        payload: dict[str, object],
        schema: dict[str, Any],
        schema_name: str,
        max_output_tokens: int,
    ) -> ProviderCallResult:
        del schema_name  # Gemini does not require a separate schema name.
        if not self.api_key:
            return self._error(
                "GEMINI_API_KEY is not configured",
                error_code="MISSING_API_KEY",
            )
        if (
            self.last_health is not None
            and self.last_health.status_code == 200
            and not self.last_health.model_available
        ):
            unavailable_status = 404 if self.last_health.error_code == "MODEL_NOT_FOUND" else 400
            return self._error(
                self.last_health.error_message,
                status_code=unavailable_status,
                error_code=self.last_health.error_code,
                model_unavailable=True,
                retryable=True,
            )
        request = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": json.dumps(
                                payload,
                                ensure_ascii=False,
                                sort_keys=True,
                                allow_nan=False,
                            )
                        }
                    ],
                }
            ],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseJsonSchema": _gemini_schema(schema),
                "maxOutputTokens": max_output_tokens,
                "temperature": 0.1,
            },
        }
        started = perf_counter()
        own_client = self.client is None
        client: AsyncHTTPClient = self.client or httpx.AsyncClient(timeout=self.timeout_seconds)
        try:
            encoded_model = quote(self.model, safe="")
            response = await client.post(
                f"{self.base_url}/models/{encoded_model}:generateContent",
                headers=self._headers,
                json=request,
            )
            response.raise_for_status()
            response_payload = response.json()
            if not isinstance(response_payload, dict):
                raise ValueError("Gemini response must be a JSON object")
            usage = response_payload.get("usageMetadata") or {}
            usage = usage if isinstance(usage, dict) else {}
            input_tokens = int(usage.get("promptTokenCount") or 0)
            output_tokens = int(usage.get("candidatesTokenCount") or 0) + int(
                usage.get("thoughtsTokenCount") or 0
            )
            cost = (
                input_tokens * self.input_cost_per_million
                + output_tokens * self.output_cost_per_million
            ) / 1_000_000
            exact_model = response_payload.get("modelVersion")
            return ProviderCallResult(
                provider=self.name,
                model=exact_model if isinstance(exact_model, str) else self.model,
                status="OK",
                text=_gemini_response_text(response_payload),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_cost_usd=round(cost, 8),
                latency_ms=round((perf_counter() - started) * 1_000),
                usage=usage,
            )
        except httpx.HTTPStatusError as error:
            status_code, error_code, message, model_unavailable = _google_error(error.response)
            return self._error(
                message,
                latency_ms=round((perf_counter() - started) * 1_000),
                status_code=status_code,
                error_code=error_code,
                model_unavailable=model_unavailable,
                retryable=model_unavailable or status_code in {429, 503, 504},
            )
        except (httpx.TimeoutException, TimeoutError) as error:
            return self._error(
                error,
                latency_ms=round((perf_counter() - started) * 1_000),
                error_code="TIMEOUT",
                retryable=True,
            )
        except (httpx.HTTPError, ValueError) as error:
            return self._error(
                error,
                latency_ms=round((perf_counter() - started) * 1_000),
                error_code=(
                    "INVALID_RESPONSE" if isinstance(error, ValueError) else "NETWORK_ERROR"
                ),
            )
        except Exception as error:
            return self._error(
                error,
                latency_ms=round((perf_counter() - started) * 1_000),
                error_code="UNEXPECTED_ERROR",
            )
        finally:
            if own_client and isinstance(client, httpx.AsyncClient):
                await client.aclose()

    def _error(
        self,
        error: Exception | str,
        *,
        latency_ms: int = 0,
        status_code: int | None = None,
        error_code: str = "ERROR",
        model_unavailable: bool = False,
        retryable: bool = False,
    ) -> ProviderCallResult:
        message = str(error) or type(error).__name__
        log_status: int | str = status_code if status_code is not None else error_code
        logger.error(
            "gemini request failed model=%s status=%s error_code=%s fallback=%s",
            self.model,
            log_status,
            error_code,
            str(self.is_fallback).lower(),
        )
        return ProviderCallResult(
            provider=self.name,
            model=self.model,
            status="ERROR",
            latency_ms=latency_ms,
            error=message[:1_000],
            status_code=status_code,
            error_code=error_code,
            model_unavailable=model_unavailable,
            retryable=retryable,
            usage={
                "status_code": status_code,
                "error_code": error_code,
                "error_message": message[:1_000],
            },
        )
