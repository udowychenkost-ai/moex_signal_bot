from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import httpx
import pytest
from aiogram import Dispatcher
from aiogram.methods import EditMessageText

from app.ai_analyst import AIAnalystService
from app.ai_providers import GeminiHealthStatus, GeminiProvider
from app.bot import BotServices, create_router, format_scan_funnel
from app.config import Settings
from app.db import create_engine_and_session, init_db
from app.models import AIRequestLog
from app.provider_health import (
    GeminiHealthMonitor,
    GeminiRequestDiagnostics,
    GeminiRuntimeHealth,
    format_gemini_diagnostics,
)
from app.quality import QualityGate
from app.scheduler import ScheduledJobs
from tests.test_telegram_context import callback_update, seeded_context
from tests.test_ux_hotfix import FakeIngestion, InteractiveBot, MenuSignals
from tests.test_v2_quality_ai import candidate, gemini_payload, gemini_settings


def gemini_provider(
    settings: Settings,
    client: httpx.AsyncClient,
    *,
    model: str | None = None,
    fallback: bool = False,
) -> GeminiProvider:
    return GeminiProvider(
        api_key=settings.gemini_api_key,
        base_url=settings.gemini_base_url,
        model=model or settings.ai_model,
        timeout_seconds=settings.ai_request_timeout_seconds,
        input_cost_per_million=0,
        output_cost_per_million=0,
        client=client,
        is_fallback=fallback,
    )


async def generate(provider: GeminiProvider):
    return await provider.generate(
        system_prompt="Use only supplied data.",
        payload={"ticker": "SBER"},
        schema={
            "type": "object",
            "properties": {"verdict": {"type": "string"}},
            "required": ["verdict"],
        },
        schema_name="verdict",
        max_output_tokens=100,
    )


def google_error(status_code: int, status: str, message: str, *, reason: str = ""):
    details = [{"reason": reason}] if reason else []
    return {
        "error": {
            "code": status_code,
            "status": status,
            "message": message,
            "details": details,
        }
    }


@pytest.mark.asyncio
async def test_real_http_contract_primary_gemini_success() -> None:
    settings = gemini_settings(gemini_api_key="secret-test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1beta/models/gemini-3.6-flash:generateContent"
        assert request.headers["x-goog-api-key"] == "secret-test-key"
        body = json.loads(request.content)
        assert body["contents"][0]["role"] == "user"
        assert body["systemInstruction"]["parts"][0]["text"]
        config = body["generationConfig"]
        assert config["responseMimeType"] == "application/json"
        assert config["responseJsonSchema"]["type"] == "object"
        assert config["thinkingConfig"] == {"thinkingLevel": "minimal"}
        assert "temperature" not in config
        return httpx.Response(200, request=request, json=gemini_payload())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await generate(gemini_provider(settings, client))

    assert result.status == "OK"
    assert result.model == "gemini-3.6-flash"


@pytest.mark.asyncio
async def test_base_url_and_model_resource_prefix_are_normalized_once() -> None:
    settings = gemini_settings(gemini_api_key="test")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1beta/models/gemini-3.6-flash:generateContent"
        return httpx.Response(200, request=request, json=gemini_payload())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GeminiProvider(
            api_key="test",
            base_url=f"{settings.gemini_base_url}/models",
            model="models/gemini-3.6-flash",
            timeout_seconds=20,
            input_cost_per_million=0,
            output_cost_per_million=0,
            client=client,
        )
        result = await generate(provider)

    assert result.status == "OK"


@pytest.mark.asyncio
async def test_primary_model_404_is_classified_as_fallback_eligible() -> None:
    settings = gemini_settings(gemini_api_key="test")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            request=request,
            json=google_error(
                404,
                "NOT_FOUND",
                "models/gemini-3.6-flash is not found for API version v1beta",
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await generate(gemini_provider(settings, client))

    assert result.status_code == 404
    assert result.error_code == "MODEL_NOT_FOUND"
    assert result.model_unavailable
    assert result.retryable
    assert "test" not in result.error


@pytest.mark.asyncio
async def test_model_404_uses_flash_lite_once_and_fallback_succeeds() -> None:
    settings = gemini_settings(gemini_api_key="test")
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if "flash-lite" not in request.url.path:
            return httpx.Response(
                404,
                request=request,
                json=google_error(404, "NOT_FOUND", "model is not found"),
            )
        payload = gemini_payload()
        payload["modelVersion"] = "gemini-flash-lite-latest"
        return httpx.Response(200, request=request, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        quant = candidate()
        review = await AIAnalystService(settings, client=client).review(
            quant,
            QualityGate(settings).evaluate(quant),
        )

    assert review.status == "OK"
    assert review.fallback_used
    assert review.model == "gemini-flash-lite-latest"
    assert len(paths) == 2
    assert review.attempts[0].error_code == "MODEL_NOT_FOUND"


@pytest.mark.asyncio
async def test_both_models_404_fail_closed_without_more_retries() -> None:
    settings = gemini_settings(gemini_api_key="test")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            404,
            request=request,
            json=google_error(404, "NOT_FOUND", "configured model is not found"),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        quant = candidate()
        review = await AIAnalystService(settings, client=client).review(
            quant,
            QualityGate(settings).evaluate(quant),
        )

    assert review.status == "AI_NOT_REVIEWED"
    assert review.analysis.verdict == "WAIT"
    assert review.fallback_used
    assert calls == 2


@pytest.mark.asyncio
async def test_invalid_key_is_safe_and_does_not_retry_fallback() -> None:
    settings = gemini_settings(gemini_api_key="invalid-secret")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            400,
            request=request,
            json=google_error(
                400,
                "INVALID_ARGUMENT",
                "API key not valid. Please pass a valid API key.",
                reason="API_KEY_INVALID",
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await generate(gemini_provider(settings, client))

    assert result.error_code == "API_KEY_INVALID"
    assert not result.retryable
    assert calls == 1
    assert "invalid-secret" not in result.error


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["429", "timeout"])
async def test_rate_limit_and_timeout_remain_fallback_eligible(failure: str) -> None:
    settings = gemini_settings(gemini_api_key="test")

    def handler(request: httpx.Request) -> httpx.Response:
        if failure == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(
            429,
            request=request,
            json=google_error(429, "RESOURCE_EXHAUSTED", "Rate limit exceeded"),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await generate(gemini_provider(settings, client))

    assert result.retryable
    assert result.error_code == ("TIMEOUT" if failure == "timeout" else "RESOURCE_EXHAUSTED")


@pytest.mark.asyncio
async def test_invalid_structured_json_remains_provider_success_but_analysis_fails_closed() -> None:
    settings = gemini_settings(gemini_api_key="test")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            request=request,
            json={"candidates": [{"content": {"parts": [{"text": "not-json"}]}}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        quant = candidate()
        review = await AIAnalystService(settings, client=client).review(
            quant,
            QualityGate(settings).evaluate(quant),
        )

    assert review.status == "AI_NOT_REVIEWED"
    assert review.analysis.verdict == "WAIT"
    assert review.request_count == 3
    assert review.fallback_used
    assert all(attempt.error_code == "INVALID_STRUCTURED_RESPONSE" for attempt in review.attempts)
    assert calls == 3


@pytest.mark.asyncio
async def test_provider_health_uses_list_models_and_validates_generate_content() -> None:
    settings = gemini_settings(gemini_api_key="health-key")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-goog-api-key"] == "health-key"
        if request.method == "GET":
            assert request.url.path == "/v1beta/models"
            assert request.url.params["pageSize"] == "1000"
            return httpx.Response(
                200,
                request=request,
                json={
                    "models": [
                        {
                            "name": "models/gemini-3.6-flash",
                            "supportedGenerationMethods": ["generateContent"],
                        }
                    ]
                },
            )

        assert request.method == "POST"
        assert request.url.path == "/v1beta/models/gemini-3.6-flash:generateContent"
        body = json.loads(request.content)
        assert body["generationConfig"]["responseMimeType"] == "application/json"
        assert body["generationConfig"]["maxOutputTokens"] == 32
        assert body["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "minimal"}
        return httpx.Response(200, request=request, json={"candidates": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        health = await gemini_provider(settings, client).check_health()

    assert health.api_reachable
    assert health.model_listed
    assert health.model_callable
    assert health.model_available
    assert health.status_code == 200


@pytest.mark.asyncio
async def test_listed_legacy_model_with_generate_content_404_is_not_available() -> None:
    settings = gemini_settings(gemini_api_key="health-key")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                request=request,
                json={
                    "models": [
                        {
                            "name": "models/gemini-2.5-flash",
                            "supportedGenerationMethods": ["generateContent"],
                        }
                    ]
                },
            )
        assert request.url.path == "/v1beta/models/gemini-2.5-flash:generateContent"
        return httpx.Response(
            404,
            request=request,
            json=google_error(
                404,
                "NOT_FOUND",
                (
                    "This model models/gemini-2.5-flash is no longer available "
                    "to new users. Please update your code to use "
                    "models/gemini-3.6-flash."
                ),
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        health = await gemini_provider(
            settings,
            client,
            model="gemini-2.5-flash",
        ).check_health()

    assert health.model_listed
    assert not health.model_callable
    assert not health.model_available
    assert health.api_reachable
    assert health.status_code == 404
    assert health.error_code == "MODEL_NOT_FOUND"


@pytest.mark.asyncio
async def test_startup_validation_marks_primary_missing_fallback_available_degraded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = gemini_settings(gemini_api_key="test")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                request=request,
                json={
                    "models": [
                        {
                            "name": "models/gemini-3.6-flash",
                            "supportedGenerationMethods": ["generateContent"],
                        },
                        {
                            "name": "models/gemini-flash-lite-latest",
                            "supportedGenerationMethods": ["generateContent"],
                        },
                    ]
                },
            )
        if request.url.path.endswith("/gemini-3.6-flash:generateContent"):
            return httpx.Response(
                404,
                request=request,
                json=google_error(404, "NOT_FOUND", "primary is unavailable"),
            )
        assert request.url.path.endswith("/gemini-flash-lite-latest:generateContent")
        return httpx.Response(200, request=request, json={"candidates": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monitor = GeminiHealthMonitor(settings, client=client)
        with caplog.at_level(logging.WARNING):
            report = await monitor.validate_startup()

    assert report.api_status == "DEGRADED"
    assert report.primary.model_listed
    assert not report.primary.model_callable
    assert not report.primary.model_available
    assert report.fallback.model_listed
    assert report.fallback.model_callable
    assert report.fallback.model_available
    assert "DEGRADED" in caplog.text


@pytest.mark.asyncio
async def test_health_reported_primary_unavailable_skips_primary_generate_and_uses_fallback() -> (
    None
):
    settings = gemini_settings(gemini_api_key="test")
    post_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                request=request,
                json={
                    "models": [
                        {
                            "name": "models/gemini-3.6-flash",
                            "supportedGenerationMethods": ["generateContent"],
                        },
                        {
                            "name": "models/gemini-flash-lite-latest",
                            "supportedGenerationMethods": ["generateContent"],
                        },
                    ]
                },
            )
        post_paths.append(request.url.path)
        if request.url.path.endswith("/gemini-3.6-flash:generateContent"):
            return httpx.Response(
                404,
                request=request,
                json=google_error(404, "NOT_FOUND", "primary is unavailable"),
            )
        payload = gemini_payload()
        payload["modelVersion"] = "gemini-flash-lite-latest"
        return httpx.Response(200, request=request, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monitor = GeminiHealthMonitor(settings, client=client)
        await monitor.refresh()
        post_paths.clear()
        quant = candidate()
        review = await AIAnalystService(
            settings,
            provider=monitor.primary,
            fallback_provider=monitor.fallback,
        ).review(quant, QualityGate(settings).evaluate(quant))

    assert review.status == "OK"
    assert review.fallback_used
    assert post_paths == ["/v1beta/models/gemini-flash-lite-latest:generateContent"]
    assert review.attempts[0].status_code == 404


@pytest.mark.asyncio
async def test_telegram_gemini_diagnostics_are_compact_and_include_error_code() -> None:
    engine, factory = create_engine_and_session("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    settings = gemini_settings(gemini_api_key="test")
    now = datetime.now(UTC)
    async with factory() as session, session.begin():
        session.add_all(
            [
                AIRequestLog(
                    request_kind="CANDIDATE",
                    provider="gemini",
                    model=settings.ai_model,
                    status="ERROR",
                    error=(
                        "Client error '404 Not Found' for url "
                        "'https://generativelanguage.googleapis.com/v1beta/models/"
                        "gemini-2.5-flash:generateContent?key=secret-value'"
                    ),
                    created_at=now,
                ),
                AIRequestLog(
                    request_kind="CANDIDATE",
                    provider="gemini",
                    model=settings.ai_fallback_model,
                    status="OK",
                    fallback_used=True,
                    created_at=now,
                ),
            ]
        )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                request=request,
                json={
                    "models": [
                        {
                            "name": f"models/{settings.ai_model}",
                            "supportedGenerationMethods": ["generateContent"],
                        },
                        {
                            "name": f"models/{settings.ai_fallback_model}",
                            "supportedGenerationMethods": ["generateContent"],
                        },
                    ]
                },
            )
        return httpx.Response(200, request=request, json={"candidates": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monitor = GeminiHealthMonitor(settings, factory, client=client)
        text = format_gemini_diagnostics(
            await monitor.refresh(),
            await monitor.request_diagnostics(now=now),
        )

    assert "API: <b>OK</b>" in text
    assert "LISTED: <b>YES</b>" in text
    assert "CALLABLE: <b>YES</b>" in text
    assert "Requests today: <b>2</b>" in text
    assert "Fallback used: <b>1</b>" in text
    assert "404 MODEL_NOT_FOUND" in text
    assert "secret-value" not in text
    assert "for url" not in text.lower()
    await engine.dispose()


class FakeOperations:
    async def status(self):
        return object()


class FakeGeminiMonitor:
    def __init__(self) -> None:
        checked_at = datetime.now(UTC)
        self.report = GeminiRuntimeHealth(
            provider="gemini",
            primary=GeminiHealthStatus(
                provider="gemini",
                configured_model="gemini-3.6-flash",
                model_listed=True,
                model_callable=False,
                api_reachable=True,
                status_code=404,
                error_code="MODEL_NOT_FOUND",
                error_message="Configured model is not callable",
                checked_at=checked_at,
            ),
            fallback=GeminiHealthStatus(
                provider="gemini",
                configured_model="gemini-flash-lite-latest",
                model_listed=True,
                model_callable=True,
                api_reachable=True,
                status_code=200,
                error_code="",
                error_message="",
                checked_at=checked_at,
            ),
            api_status="DEGRADED",
            checked_at=checked_at,
        )

    async def refresh(self):
        return self.report

    async def request_diagnostics(self):
        return GeminiRequestDiagnostics(
            3,
            1,
            2,
            1,
            datetime.now(UTC),
            datetime.now(UTC),
            404,
            "MODEL_NOT_FOUND",
            "Configured model is absent",
        )


@pytest.mark.asyncio
async def test_gemini_status_callback_renders_live_provider_diagnostics() -> None:
    engine, factory, _ = await seeded_context()
    services = BotServices(
        settings=Settings(_env_file=None),
        session_factory=factory,
        ingestion=FakeIngestion(),  # type: ignore[arg-type]
        signals=MenuSignals(),  # type: ignore[arg-type]
        operations=FakeOperations(),  # type: ignore[arg-type]
        gemini_health=FakeGeminiMonitor(),  # type: ignore[arg-type]
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(create_router(services))
    bot = InteractiveBot()

    await dispatcher.feed_update(bot, callback_update("status:gemini"))

    edits = [method for method in bot.methods if isinstance(method, EditMessageText)]
    assert edits
    assert "API: <b>DEGRADED</b>" in edits[-1].text
    assert "LISTED: <b>YES</b>" in edits[-1].text
    assert "CALLABLE: <b>NO</b>" in edits[-1].text
    assert "CALLABLE: <b>YES</b>" in edits[-1].text
    assert "404 MODEL_NOT_FOUND" in edits[-1].text
    await bot.session.close()
    await engine.dispose()


def test_scan_funnel_contains_all_requested_stages_and_rejection_reasons() -> None:
    text = format_scan_funnel(
        {
            "checked_instruments": 100,
            "quant_candidates": 12,
            "quality_pass": 4,
            "quality_weak": 3,
            "quality_reject": 5,
            "ai_approve": 1,
            "ai_strong_approve": 1,
            "ai_wait": 1,
            "ai_rejected": 0,
            "ai_errors": 1,
            "published": 2,
            "top_rejection_reasons": [{"reason": "Low liquidity", "count": 4}],
        }
    )

    assert "Checked instruments: <b>100</b>" in text
    assert "STRONG_APPROVE: <b>1</b>" in text
    assert "ERROR/NOT_REVIEWED: <b>1</b>" in text
    assert "Low liquidity — <b>4</b>" in text


class FunnelScanner:
    async def scan_ideas(self):
        return {
            "checked_instruments": 20,
            "quant_candidates": 3,
            "quality_pass": 1,
            "quality_weak": 1,
            "quality_reject": 1,
            "ai_approve": 0,
            "ai_strong_approve": 0,
            "ai_wait": 0,
            "ai_rejected": 0,
            "ai_errors": 1,
            "ai_not_reviewed": 0,
            "published": 0,
        }


@pytest.mark.asyncio
async def test_scan_runtime_log_has_single_compact_funnel_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    jobs = ScheduledJobs(Settings(_env_file=None), FunnelScanner(), object(), object())

    with caplog.at_level(logging.INFO):
        await jobs.scan_market()

    assert (
        "scan completed checked=20 candidates=3 pass=1 weak=1 reject=1 "
        "ai_approve=0 ai_wait=0 ai_reject=0 ai_error=1 published=0"
    ) in caplog.text
