from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from aiogram import Dispatcher
from aiogram.methods import EditMessageText
from openai import AsyncOpenAI
from sqlalchemy import select

from app.ai_analyst import SYSTEM_PROMPT, AIAnalysis, AIAnalystService
from app.ai_providers import GeminiProvider, OpenAIProvider, ProviderHealthStatus
from app.bot import BotServices, create_router
from app.config import Settings
from app.db import create_engine_and_session, init_db
from app.domain import InstrumentData
from app.experiments import save_candidate_experiment
from app.models import AIRequestLog
from app.provider_health import (
    OpenAIHealthMonitor,
    ProviderRequestDiagnostics,
    ProviderRuntimeHealth,
)
from app.quality import QualityGate, apply_quality_result
from app.repositories import upsert_instruments
from tests.test_gemini_provider_health import FakeOperations
from tests.test_telegram_context import callback_update, seeded_context
from tests.test_ux_hotfix import FakeIngestion, InteractiveBot, MenuSignals
from tests.test_v2_quality_ai import analysis_body, candidate, english_analysis_body


def openai_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "ai_provider": "openai",
        "openai_api_key": "test-openai-key",
        "ai_model": "gpt-5.6-terra",
        "ai_fallback_model": "gpt-5.6-luna",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def response_payload(
    body: dict[str, object] | None = None,
    *,
    model: str = "gpt-5.6-terra-2026-08-01",
) -> dict[str, object]:
    return {
        "id": "resp_test",
        "status": "completed",
        "model": model,
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps(body or analysis_body(), ensure_ascii=False),
                    }
                ],
            }
        ],
        "usage": {
            "input_tokens": 120,
            "output_tokens": 60,
            "input_tokens_details": {"cached_tokens": 20},
            "output_tokens_details": {"reasoning_tokens": 5},
            "total_tokens": 180,
        },
    }


class FakeOpenAIError(Exception):
    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        code: str = "provider_error",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = {"error": {"code": code, "message": message}}


class FakeResponses:
    def __init__(self, outcomes: object | list[object]) -> None:
        self.outcomes = list(outcomes) if isinstance(outcomes, list) else [outcomes]
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> object:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeOpenAIClient:
    def __init__(self, outcomes: object | list[object]) -> None:
        self.responses = FakeResponses(outcomes)


def provider(
    settings: Settings,
    client: FakeOpenAIClient,
    *,
    model: str | None = None,
    fallback: bool = False,
) -> OpenAIProvider:
    return OpenAIProvider(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        model=model or settings.ai_model,
        timeout_seconds=settings.ai_request_timeout_seconds,
        input_cost_per_million=(
            settings.ai_fallback_input_cost_per_million
            if fallback
            else settings.ai_input_cost_per_million
        ),
        output_cost_per_million=(
            settings.ai_fallback_output_cost_per_million
            if fallback
            else settings.ai_output_cost_per_million
        ),
        client=client,
        is_fallback=fallback,
    )


@pytest.mark.asyncio
async def test_openai_official_sdk_responses_http_contract() -> None:
    settings = openai_settings()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/responses"
        assert request.headers["Authorization"] == "Bearer test-openai-key"
        body = json.loads(request.content)
        assert body["model"] == "gpt-5.6-terra"
        assert body["store"] is False
        assert "tools" not in body
        assert body["text"]["format"]["type"] == "json_schema"
        assert body["text"]["format"]["strict"] is True
        return httpx.Response(
            200,
            request=request,
            json=response_payload(),
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sdk = AsyncOpenAI(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        max_retries=0,
        http_client=http_client,
    )
    try:
        result = await provider(settings, sdk).generate(  # type: ignore[arg-type]
            system_prompt=SYSTEM_PROMPT,
            payload={"ticker": "SBER"},
            schema=AIAnalysis.model_json_schema(),
            schema_name="moex_ai_verdict",
            max_output_tokens=4_096,
        )
    finally:
        await sdk.close()

    assert result.status == "OK"
    assert result.provider == "openai"
    assert result.input_tokens == 120


@pytest.mark.asyncio
async def test_openai_primary_success_uses_responses_structured_output() -> None:
    settings = openai_settings()
    client = FakeOpenAIClient(response_payload())
    quant = candidate()

    review = await AIAnalystService(settings, client=client).review(
        quant, QualityGate(settings).evaluate(quant)
    )

    assert review.approved
    assert review.provider == "openai"
    assert review.model == "gpt-5.6-terra-2026-08-01"
    assert review.input_tokens == 120
    assert review.output_tokens == 60
    assert review.estimated_cost_usd == pytest.approx(0.00096)
    assert review.usage is not None
    request = client.responses.calls[0]
    assert request["model"] == "gpt-5.6-terra"
    assert request["store"] is False
    assert "tools" not in request
    assert request["text"]["format"]["strict"] is True
    assert request["text"]["format"]["schema"]["additionalProperties"] is False
    snapshot = json.loads(request["input"])
    assert snapshot["ticker"] == "SBER"
    assert "candles" not in snapshot
    assert "Never invent" in request["instructions"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [408, 429, 500, 503])
async def test_openai_retryable_primary_failure_uses_fallback_once(status_code: int) -> None:
    settings = openai_settings()
    client = FakeOpenAIClient(
        [
            FakeOpenAIError(status_code, "temporary provider failure"),
            response_payload(model="gpt-5.6-luna-2026-08-01"),
        ]
    )
    quant = candidate()

    review = await AIAnalystService(settings, client=client).review(
        quant, QualityGate(settings).evaluate(quant)
    )

    assert review.status == "OK"
    assert review.fallback_used
    assert review.model == "gpt-5.6-luna-2026-08-01"
    assert [attempt.model for attempt in review.attempts] == [
        "gpt-5.6-terra",
        "gpt-5.6-luna-2026-08-01",
    ]
    assert [attempt.fallback_used for attempt in review.attempts] == [False, True]
    assert len(client.responses.calls) == 2


@pytest.mark.asyncio
async def test_openai_both_models_fail_closed() -> None:
    settings = openai_settings()
    client = FakeOpenAIClient(
        [
            FakeOpenAIError(500, "primary unavailable"),
            FakeOpenAIError(503, "fallback unavailable"),
        ]
    )
    quant = candidate()

    review = await AIAnalystService(settings, client=client).review(
        quant, QualityGate(settings).evaluate(quant)
    )

    assert review.status == "AI_NOT_REVIEWED"
    assert review.analysis.verdict == "WAIT"
    assert review.fallback_used
    assert review.request_count == 2
    assert not review.approved


@pytest.mark.asyncio
async def test_openai_timeout_uses_fallback() -> None:
    settings = openai_settings()
    client = FakeOpenAIClient(
        [
            httpx.ReadTimeout("timed out"),
            response_payload(model="gpt-5.6-luna-2026-08-01"),
        ]
    )
    quant = candidate()

    review = await AIAnalystService(settings, client=client).review(
        quant, QualityGate(settings).evaluate(quant)
    )

    assert review.status == "OK"
    assert review.fallback_used
    assert review.attempts[0].error_code == "TIMEOUT"


@pytest.mark.asyncio
async def test_openai_connection_error_uses_fallback() -> None:
    settings = openai_settings()
    client = FakeOpenAIClient(
        [
            httpx.ConnectError("connection failed"),
            response_payload(model="gpt-5.6-luna-2026-08-01"),
        ]
    )
    quant = candidate()

    review = await AIAnalystService(settings, client=client).review(
        quant, QualityGate(settings).evaluate(quant)
    )

    assert review.status == "OK"
    assert review.fallback_used
    assert review.attempts[0].error_code == "CONNECTION_ERROR"


@pytest.mark.asyncio
async def test_openai_language_mismatch_retries_primary_but_never_fallback() -> None:
    settings = openai_settings()
    client = FakeOpenAIClient(
        [
            response_payload(english_analysis_body()),
            response_payload(),
        ]
    )
    quant = candidate()

    review = await AIAnalystService(settings, client=client).review(
        quant, QualityGate(settings).evaluate(quant)
    )

    assert review.status == "OK"
    assert review.request_count == 2
    assert not review.fallback_used
    assert review.attempts[0].error_code == "LANGUAGE_MISMATCH"
    assert review.attempts[1].retry_stage == "PRIMARY_LANGUAGE_RETRY"
    assert client.responses.calls[1]["model"] == "gpt-5.6-terra"
    assert "LANGUAGE_MISMATCH" in client.responses.calls[1]["instructions"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403, 404])
async def test_openai_permanent_errors_do_not_use_fallback(status_code: int) -> None:
    settings = openai_settings()
    key = settings.openai_api_key
    client = FakeOpenAIClient(FakeOpenAIError(status_code, f"request rejected api_key={key}"))
    quant = candidate()

    review = await AIAnalystService(settings, client=client).review(
        quant, QualityGate(settings).evaluate(quant)
    )

    assert review.status == "AI_NOT_REVIEWED"
    assert review.analysis.verdict == "WAIT"
    assert not review.fallback_used
    assert review.request_count == 1
    assert key not in review.error
    assert key not in review.attempts[0].error
    assert (
        review.attempts[0].error_code
        == {
            401: "AUTHENTICATION_ERROR",
            403: "PERMISSION_DENIED",
            404: "MODEL_NOT_FOUND",
        }[status_code]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        "not-json",
        json.dumps({"verdict": "APPROVE", "score": 80}),
        json.dumps(analysis_body(verdict="UNKNOWN"), ensure_ascii=False),
    ],
)
async def test_openai_invalid_structured_output_fails_closed_without_fallback(body: str) -> None:
    settings = openai_settings()
    malformed = response_payload()
    malformed["output"][0]["content"][0]["text"] = body
    client = FakeOpenAIClient([malformed, response_payload(model="gpt-5.6-luna-2026-08-01")])
    quant = candidate()

    review = await AIAnalystService(settings, client=client).review(
        quant, QualityGate(settings).evaluate(quant)
    )

    assert review.status == "AI_NOT_REVIEWED"
    assert review.analysis.verdict == "WAIT"
    assert review.attempts[0].error_code == "INVALID_STRUCTURED_RESPONSE"
    assert not review.fallback_used
    assert len(client.responses.calls) == 1


@pytest.mark.asyncio
async def test_missing_openai_key_fails_closed_without_request() -> None:
    settings = openai_settings(openai_api_key="")
    client = FakeOpenAIClient(response_payload())
    quant = candidate()

    review = await AIAnalystService(settings, client=client).review(
        quant, QualityGate(settings).evaluate(quant)
    )

    assert review.status == "AI_NOT_REVIEWED"
    assert review.analysis.verdict == "WAIT"
    assert review.attempts[0].error_code == "MISSING_API_KEY"
    assert client.responses.calls == []


@pytest.mark.asyncio
async def test_openai_health_primary_ok() -> None:
    settings = openai_settings()
    monitor = OpenAIHealthMonitor(
        settings,
        primary=provider(settings, FakeOpenAIClient(response_payload({"ok": True}))),
        fallback=provider(
            settings,
            FakeOpenAIClient(response_payload({"ok": True}, model="gpt-5.6-luna")),
            model="gpt-5.6-luna",
            fallback=True,
        ),
    )

    report = await monitor.refresh()

    assert report.api_status == "OK"
    assert report.primary.model_callable
    assert report.primary.model_listed is None
    assert report.fallback.model_callable


@pytest.mark.asyncio
async def test_openai_health_fallback_only_is_degraded() -> None:
    settings = openai_settings()
    monitor = OpenAIHealthMonitor(
        settings,
        primary=provider(settings, FakeOpenAIClient(FakeOpenAIError(503, "unavailable"))),
        fallback=provider(
            settings,
            FakeOpenAIClient(response_payload({"ok": True}, model="gpt-5.6-luna")),
            model="gpt-5.6-luna",
            fallback=True,
        ),
    )

    report = await monitor.refresh()

    assert report.api_status == "DEGRADED"
    assert not report.primary.model_callable
    assert report.fallback.model_callable


@pytest.mark.asyncio
async def test_openai_health_both_unavailable_is_error() -> None:
    settings = openai_settings()
    monitor = OpenAIHealthMonitor(
        settings,
        primary=provider(settings, FakeOpenAIClient(FakeOpenAIError(401, "bad key"))),
        fallback=provider(
            settings,
            FakeOpenAIClient(FakeOpenAIError(401, "bad key")),
            model="gpt-5.6-luna",
            fallback=True,
        ),
    )

    report = await monitor.refresh()

    assert report.api_status == "ERROR"
    assert not report.primary.model_callable
    assert not report.fallback.model_callable


class FakeOpenAIMonitor:
    def __init__(self) -> None:
        checked_at = datetime.now(UTC)
        self.report = ProviderRuntimeHealth(
            provider="openai",
            primary=ProviderHealthStatus(
                provider="openai",
                configured_model="gpt-5.6-terra",
                model_listed=None,
                model_callable=True,
                api_reachable=True,
                status_code=200,
                error_code="",
                error_message="",
                checked_at=checked_at,
            ),
            fallback=ProviderHealthStatus(
                provider="openai",
                configured_model="gpt-5.6-luna",
                model_listed=None,
                model_callable=True,
                api_reachable=True,
                status_code=200,
                error_code="",
                error_message="",
                checked_at=checked_at,
            ),
            api_status="OK",
            checked_at=checked_at,
        )

    async def refresh(self) -> ProviderRuntimeHealth:
        return self.report

    async def request_diagnostics(self) -> ProviderRequestDiagnostics:
        return ProviderRequestDiagnostics(2, 2, 0, 1, datetime.now(UTC), None, None, "", "")


@pytest.mark.asyncio
async def test_status_renders_openai_selected_provider() -> None:
    engine, factory, _ = await seeded_context()
    services = BotServices(
        settings=openai_settings(),
        session_factory=factory,
        ingestion=FakeIngestion(),  # type: ignore[arg-type]
        signals=MenuSignals(),  # type: ignore[arg-type]
        operations=FakeOperations(),  # type: ignore[arg-type]
        ai_health=FakeOpenAIMonitor(),  # type: ignore[arg-type]
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(create_router(services))
    bot = InteractiveBot()

    await dispatcher.feed_update(bot, callback_update("status:ai"))

    edits = [method for method in bot.methods if isinstance(method, EditMessageText)]
    assert edits
    assert "🧠 <b>OpenAI</b>" in edits[-1].text
    assert "gpt-5.6-terra" in edits[-1].text
    assert "Gemini" not in edits[-1].text
    await bot.session.close()
    await engine.dispose()


@pytest.mark.asyncio
async def test_openai_attempts_persist_provider_model_fallback_and_usage() -> None:
    engine, factory = create_engine_and_session("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    settings = openai_settings()
    quant = candidate()
    quality = QualityGate(settings).evaluate(quant)
    apply_quality_result(quant, quality, strategy_version=settings.strategy_version)
    client = FakeOpenAIClient(
        [
            FakeOpenAIError(429, "rate limited", code="rate_limit_error"),
            response_payload(model="gpt-5.6-luna-2026-08-01"),
        ]
    )
    review = await AIAnalystService(settings, client=client).review(quant, quality)

    async with factory() as session, session.begin():
        await upsert_instruments(
            session,
            [InstrumentData("SBER", "TQBR", "Сбербанк", daily_turnover=1e9)],
        )
        experiment = await save_candidate_experiment(
            session,
            quant,
            quality,
            review=review,
            publish_reason="AI_APPROVE",
        )
        experiment_id = experiment.id
    async with factory() as session:
        logs = list(
            await session.scalars(
                select(AIRequestLog)
                .where(AIRequestLog.candidate_id == experiment_id)
                .order_by(AIRequestLog.id)
            )
        )

    assert [(row.provider, row.model, row.fallback_used) for row in logs] == [
        ("openai", "gpt-5.6-terra", False),
        ("openai", "gpt-5.6-luna-2026-08-01", True),
    ]
    assert json.loads(logs[0].usage_json)["error_code"] == "RATE_LIMIT_ERROR"
    assert json.loads(logs[1].usage_json)["total_tokens"] == 180
    await engine.dispose()


@pytest.mark.asyncio
async def test_openai_runtime_never_calls_gemini(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("Gemini must not be called when AI_PROVIDER=openai")

    monkeypatch.setattr(GeminiProvider, "generate", forbidden)
    settings = openai_settings(gemini_api_key="must-remain-unused")
    client = FakeOpenAIClient(response_payload())
    quant = candidate()

    review = await AIAnalystService(settings, client=client).review(
        quant, QualityGate(settings).evaluate(quant)
    )

    assert review.status == "OK"
    assert review.provider == "openai"
