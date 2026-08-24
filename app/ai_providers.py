from __future__ import annotations

import json
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Protocol
from urllib.parse import quote

import httpx


class AsyncHTTPClient(Protocol):
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
    retryable: bool = False
    usage: dict[str, Any] = field(default_factory=dict)


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


def _status_code(error: httpx.HTTPStatusError) -> int | None:
    response = getattr(error, "response", None)
    return getattr(response, "status_code", None)


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
        del schema_name  # Gemini does not require a separate schema name.
        if not self.api_key:
            return self._error("GEMINI_API_KEY is not configured")
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
                headers={
                    "x-goog-api-key": self.api_key,
                    "Content-Type": "application/json",
                },
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
            status_code = _status_code(error)
            return self._error(
                error,
                latency_ms=round((perf_counter() - started) * 1_000),
                retryable=status_code in {429, 503, 504},
            )
        except (httpx.TimeoutException, TimeoutError) as error:
            return self._error(
                error,
                latency_ms=round((perf_counter() - started) * 1_000),
                retryable=True,
            )
        except (httpx.HTTPError, ValueError) as error:
            return self._error(error, latency_ms=round((perf_counter() - started) * 1_000))
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
