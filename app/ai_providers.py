from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import perf_counter
from typing import Any, Protocol
from urllib.parse import quote

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

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
class ProviderHealthStatus:
    provider: str
    configured_model: str
    model_listed: bool | None
    model_callable: bool
    api_reachable: bool
    status_code: int | None
    error_code: str
    error_message: str
    checked_at: datetime

    @property
    def model_available(self) -> bool:
        """Compatibility alias: availability now requires a successful real call."""
        return self.model_callable


# Public compatibility name retained for the existing Gemini tests and integrations.
GeminiHealthStatus = ProviderHealthStatus


class AIProvider(Protocol):
    name: str
    model: str

    async def check_health(self) -> ProviderHealthStatus: ...

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


def _model_dump(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        result = model_dump(mode="json")
        if isinstance(result, dict):
            return result
    raise ValueError("Provider response must be a JSON object")


_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+"),
    re.compile(r"(?i)([?&](?:key|api_key)=)[^&\s'\"]+"),
    re.compile(r"(?i)(\b(?:api[_ -]?key|token)\s*[:=]\s*)[^\s,;]+"),
)


def sanitize_provider_error(error: Exception | str, *, secret: str = "") -> str:
    """Bound provider errors and remove credentials before logs or persistence."""
    message = str(error) or (type(error).__name__ if isinstance(error, Exception) else "ERROR")
    if secret:
        message = message.replace(secret, "***")
    for pattern in _SECRET_PATTERNS:
        message = pattern.sub(r"\1***", message)
    return " ".join(message.split())[:1_000]


def _openai_error(error: Exception) -> tuple[int | None, str, str, bool]:
    status_code = getattr(error, "status_code", None)
    status_code = int(status_code) if isinstance(status_code, int) else None
    body = getattr(error, "body", None)
    body = body if isinstance(body, dict) else {}
    nested = body.get("error")
    details = nested if isinstance(nested, dict) else body
    raw_code = details.get("code") or details.get("type")
    error_code = str(raw_code or "").strip().upper()
    if isinstance(error, APITimeoutError):
        error_code = "TIMEOUT"
    elif isinstance(error, APIConnectionError):
        error_code = "CONNECTION_ERROR"
    elif status_code == 401:
        error_code = "AUTHENTICATION_ERROR"
    elif status_code == 403:
        error_code = "PERMISSION_DENIED"
    elif status_code == 404:
        error_code = "MODEL_NOT_FOUND"
    elif not error_code and status_code is not None:
        error_code = f"HTTP_{status_code}"
    elif not error_code:
        error_code = "OPENAI_ERROR"
    retryable = bool(
        isinstance(error, (APITimeoutError, APIConnectionError))
        or status_code in {408, 429}
        or (status_code is not None and status_code >= 500)
    )
    return status_code, error_code, str(error), retryable


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


def _gemini_generation_config(
    model: str,
    *,
    schema: dict[str, Any],
    max_output_tokens: int,
) -> dict[str, Any]:
    config: dict[str, Any] = {
        "responseMimeType": "application/json",
        "responseJsonSchema": _gemini_schema(schema),
        "maxOutputTokens": max_output_tokens,
    }
    # Gemini 3.6 supports `minimal` for classification-style generateContent calls.
    # The current Flash-Lite latest alias also resolves to a minimal-capable model.
    if model.startswith("gemini-3.") or model == "gemini-flash-lite-latest":
        config["thinkingConfig"] = {"thinkingLevel": "minimal"}
    return config


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
        client: Any | None = None,
        is_fallback: bool = False,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.input_cost_per_million = input_cost_per_million
        self.output_cost_per_million = output_cost_per_million
        self.client = client
        self.is_fallback = is_fallback
        self.last_health: ProviderHealthStatus | None = None

    def _client(self) -> Any:
        return self.client or AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout_seconds,
            # The application owns the single bounded fallback decision. Hidden SDK
            # retries would make telemetry and request counts misleading.
            max_retries=0,
        )

    async def check_health(self) -> ProviderHealthStatus:
        """Confirm that the configured model can complete a minimal Responses call."""
        checked_at = datetime.now(UTC)
        if not self.api_key:
            result = ProviderHealthStatus(
                provider=self.name,
                configured_model=self.model,
                model_listed=None,
                model_callable=False,
                api_reachable=False,
                status_code=None,
                error_code="MISSING_API_KEY",
                error_message="OPENAI_API_KEY is not configured",
                checked_at=checked_at,
            )
            self.last_health = result
            return result

        probe = await self.generate(
            system_prompt="Return the requested structured result using only the supplied input.",
            payload={"task": "provider_health_probe", "expected": {"ok": True}},
            schema={
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
                "additionalProperties": False,
            },
            schema_name="provider_health_probe",
            max_output_tokens=128,
        )
        callable_model = False
        if probe.status == "OK":
            try:
                parsed = json.loads(probe.text)
                callable_model = isinstance(parsed, dict) and parsed.get("ok") is True
            except (json.JSONDecodeError, TypeError):
                callable_model = False
        result = ProviderHealthStatus(
            provider=self.name,
            configured_model=self.model,
            model_listed=None,
            model_callable=callable_model,
            api_reachable=probe.status_code is not None,
            status_code=probe.status_code,
            error_code=(
                probe.error_code
                if probe.status != "OK"
                else ("INVALID_HEALTH_RESPONSE" if not callable_model else "")
            ),
            error_message=(
                probe.error
                if probe.status != "OK"
                else (
                    "OpenAI health probe returned an invalid result" if not callable_model else ""
                )
            ),
            checked_at=checked_at,
        )
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
        if not self.api_key:
            return self._error(
                "OPENAI_API_KEY is not configured",
                error_code="MISSING_API_KEY",
            )
        started = perf_counter()
        own_client = self.client is None
        client = self._client()
        try:
            response = await client.responses.create(
                model=self.model,
                max_output_tokens=max_output_tokens,
                instructions=system_prompt,
                input=json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False),
                text={
                    "format": {
                        "type": "json_schema",
                        "name": schema_name,
                        "strict": True,
                        "schema": schema,
                    }
                },
                # Candidate snapshots are not persisted by OpenAI and no tools (including
                # web search) are enabled for this second-opinion request.
                store=False,
            )
            response_payload = _model_dump(response)
            status = response_payload.get("status")
            if status not in {None, "completed"}:
                incomplete = response_payload.get("incomplete_details") or {}
                reason = incomplete.get("reason") if isinstance(incomplete, dict) else status
                return self._error(
                    f"OpenAI response was not completed: {reason or status}",
                    latency_ms=round((perf_counter() - started) * 1_000),
                    status_code=200,
                    error_code="INCOMPLETE_RESPONSE",
                )
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
                status_code=200,
                usage=usage,
            )
        except (APIStatusError, APITimeoutError, APIConnectionError) as error:
            status_code, error_code, message, retryable = _openai_error(error)
            return self._error(
                message,
                latency_ms=round((perf_counter() - started) * 1_000),
                status_code=status_code,
                error_code=error_code,
                retryable=retryable,
            )
        except (TimeoutError, httpx.TimeoutException) as error:
            return self._error(
                error,
                latency_ms=round((perf_counter() - started) * 1_000),
                error_code="TIMEOUT",
                retryable=True,
            )
        except (httpx.NetworkError, ConnectionError) as error:
            return self._error(
                error,
                latency_ms=round((perf_counter() - started) * 1_000),
                error_code="CONNECTION_ERROR",
                retryable=True,
            )
        except ValueError as error:
            return self._error(
                error,
                latency_ms=round((perf_counter() - started) * 1_000),
                error_code="INVALID_RESPONSE",
            )
        except Exception as error:
            # Test doubles and alternate transports may expose the same stable
            # status_code/body attributes without subclassing the SDK exceptions.
            status_code, error_code, message, retryable = _openai_error(error)
            return self._error(
                message,
                latency_ms=round((perf_counter() - started) * 1_000),
                status_code=status_code,
                error_code=error_code,
                retryable=retryable,
            )
        finally:
            if own_client:
                await client.close()

    def _error(
        self,
        error: Exception | str,
        *,
        latency_ms: int = 0,
        status_code: int | None = None,
        error_code: str = "ERROR",
        retryable: bool = False,
    ) -> ProviderCallResult:
        message = sanitize_provider_error(error, secret=self.api_key)
        logger.error(
            "openai request failed model=%s status=%s error_code=%s fallback=%s",
            self.model,
            status_code if status_code is not None else error_code,
            error_code,
            str(self.is_fallback).lower(),
        )
        return ProviderCallResult(
            provider=self.name,
            model=self.model,
            status="ERROR",
            latency_ms=latency_ms,
            error=message,
            status_code=status_code,
            error_code=error_code,
            model_unavailable=error_code in {"MODEL_NOT_FOUND", "MODEL_UNSUPPORTED"},
            retryable=retryable,
            usage={
                "status_code": status_code,
                "error_code": error_code,
                "error_message": message,
            },
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
        """Distinguish a listed model from one callable through generateContent."""
        checked_at = datetime.now(UTC)
        if not self.api_key:
            result = GeminiHealthStatus(
                provider=self.name,
                configured_model=self.model,
                model_listed=False,
                model_callable=False,
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
        listed = False
        api_reachable = False
        try:
            list_response = await client.get(
                f"{self.base_url}/models",
                headers=self._headers,
                params={"pageSize": 1000},
            )
            api_reachable = True
            list_response.raise_for_status()
            payload = list_response.json()
            if not isinstance(payload, dict):
                raise ValueError("Gemini ListModels response must be a JSON object")
            models = payload.get("models")
            models = models if isinstance(models, list) else []
            for item in models:
                if not isinstance(item, dict):
                    continue
                name = item.get("name")
                if isinstance(name, str) and _normalized_model(name) == self.model:
                    listed = True
                    break
        except Exception:
            # ListModels is diagnostic only. The capability probe below is authoritative.
            pass

        try:
            encoded_model = quote(self.model, safe="")
            probe_response = await client.post(
                f"{self.base_url}/models/{encoded_model}:generateContent",
                headers=self._headers,
                json={
                    "contents": [
                        {
                            "role": "user",
                            "parts": [{"text": 'Return exactly this JSON: {"ok": true}'}],
                        }
                    ],
                    "generationConfig": _gemini_generation_config(
                        self.model,
                        schema={
                            "type": "object",
                            "properties": {"ok": {"type": "boolean"}},
                            "required": ["ok"],
                            "additionalProperties": False,
                        },
                        max_output_tokens=32,
                    ),
                },
            )
            api_reachable = True
            probe_response.raise_for_status()
            result = GeminiHealthStatus(
                provider=self.name,
                configured_model=self.model,
                model_listed=listed,
                model_callable=True,
                api_reachable=True,
                status_code=int(getattr(probe_response, "status_code", 200)),
                error_code="",
                error_message="",
                checked_at=checked_at,
            )
        except httpx.HTTPStatusError as error:
            status_code, error_code, message, _ = _google_error(error.response)
            result = GeminiHealthStatus(
                provider=self.name,
                configured_model=self.model,
                model_listed=listed,
                model_callable=False,
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
                model_listed=listed,
                model_callable=False,
                api_reachable=api_reachable,
                status_code=None,
                error_code="TIMEOUT",
                error_message=(str(error) or "Gemini generateContent probe timed out")[:1_000],
                checked_at=checked_at,
            )
        except Exception as error:
            result = GeminiHealthStatus(
                provider=self.name,
                configured_model=self.model,
                model_listed=listed,
                model_callable=False,
                api_reachable=api_reachable,
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
            and not self.last_health.model_callable
            and self.last_health.error_code in {"MODEL_NOT_FOUND", "MODEL_UNSUPPORTED"}
        ):
            unavailable_status = self.last_health.status_code or (
                404 if self.last_health.error_code == "MODEL_NOT_FOUND" else 400
            )
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
            "generationConfig": _gemini_generation_config(
                self.model,
                schema=schema,
                max_output_tokens=max_output_tokens,
            ),
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
            raw_usage = response_payload.get("usageMetadata") or {}
            usage = dict(raw_usage) if isinstance(raw_usage, dict) else {}
            input_tokens = int(usage.get("promptTokenCount") or 0)
            output_tokens = int(usage.get("candidatesTokenCount") or 0) + int(
                usage.get("thoughtsTokenCount") or 0
            )
            cost = (
                input_tokens * self.input_cost_per_million
                + output_tokens * self.output_cost_per_million
            ) / 1_000_000
            exact_model = response_payload.get("modelVersion")
            text = _gemini_response_text(response_payload)
            candidates = response_payload.get("candidates")
            first_candidate = candidates[0] if isinstance(candidates, list) and candidates else {}
            if isinstance(first_candidate, dict):
                finish_reason = first_candidate.get("finishReason")
                if isinstance(finish_reason, str) and finish_reason:
                    usage["finishReason"] = finish_reason
            usage["responseChars"] = len(text)
            return ProviderCallResult(
                provider=self.name,
                model=exact_model if isinstance(exact_model, str) else self.model,
                status="OK",
                text=text,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_cost_usd=round(cost, 8),
                latency_ms=round((perf_counter() - started) * 1_000),
                status_code=int(getattr(response, "status_code", 200)),
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
        message = sanitize_provider_error(error, secret=self.api_key)
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
