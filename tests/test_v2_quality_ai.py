from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select

from app.ai_analyst import AIAnalysis, AIAnalystService, AIReviewResult
from app.bot import (
    idea_sections_keyboard,
    main_menu,
    settings_menu_keyboard,
    settings_values_keyboard,
)
from app.config import Settings
from app.db import create_engine_and_session, init_db
from app.domain import IdeaDirection, IdeaHorizon, InstrumentData, QualityGateDecision
from app.experiments import (
    CandidateExperimentTracker,
    cooldown_reason,
    save_candidate_experiment,
)
from app.forward import ForwardReportingService
from app.idea_repository import create_or_update_idea
from app.ideas import build_trading_idea, idea_material_hash
from app.models import (
    AIRequestLog,
    CandidateExperiment,
    IdeaFollow,
    TelegramUser,
    TradingIdeaEvent,
    WatchlistItem,
)
from app.observation import DataFreshnessGuard
from app.operations import OperationalService
from app.quality import QualityGate, apply_quality_result
from app.repositories import ensure_user, update_user_settings, upsert_instruments
from app.scanner import MarketScanner
from tests.test_ideas import NOW, signal


def candidate(
    *,
    direction: IdeaDirection = IdeaDirection.BUY,
    regime_score: float = 35,
    components: dict[str, float] | None = None,
):
    sign = 1 if direction == IdeaDirection.BUY else -1
    component_values = components or {
        "trend": 60 * sign,
        "momentum": 45 * sign,
        "momentum_extreme": 25 * sign,
        "volume": 35 * sign,
        "levels": 40 * sign,
        "volatility": 20 * sign,
        "relative_strength": 50 * sign,
        "market_regime": regime_score,
    }
    signals = [
        signal("5m", 65 * sign),
        signal("15m", 65 * sign),
        signal("1h", 65 * sign),
    ]
    for generated in signals:
        generated.raw_component_scores = component_values
        generated.relevant_indicators = {"volume_ratio": 1.6, "rsi": 54, "adx": 28}
        generated.market_regime = (
            "BULL" if regime_score >= 25 else "BEAR" if regime_score <= -25 else "SIDEWAYS"
        )
        generated.market_regime_score = regime_score
        generated.relative_strength_score = component_values["relative_strength"]
        generated.relative_strength_label = "выше рынка" if sign > 0 else "ниже рынка"
        generated.volume_state = "ELEVATED"
    result = build_trading_idea(
        Settings(_env_file=None),
        instrument_name="Сбербанк",
        horizon=IdeaHorizon.INTRADAY_1D,
        signals=signals,
        now=NOW,
    )
    assert result is not None
    result.daily_turnover = 1_000_000_000
    return result


def test_quality_gate_passes_only_multi_factor_confirmation() -> None:
    result = QualityGate(Settings(_env_file=None)).evaluate(candidate())

    assert result.decision == QualityGateDecision.PASS
    assert result.confirmation_count >= 4
    assert result.timeframe_confirmations == 3
    assert "trend" in result.supporting_factors


def test_rsi_only_candidate_is_not_passed() -> None:
    weak = candidate(
        components={
            "trend": 0,
            "momentum": 40,
            "momentum_extreme": 30,
            "volume": 0,
            "levels": 0,
            "volatility": 0,
            "relative_strength": 0,
            "market_regime": 0,
        }
    )
    result = QualityGate(Settings(_env_file=None)).evaluate(weak)

    assert result.decision == QualityGateDecision.WEAK
    assert result.confirmation_count == 1


def test_conflicting_factors_are_explicit_and_rejected() -> None:
    conflicted = candidate(
        components={
            "trend": 55,
            "momentum": 35,
            "momentum_extreme": 0,
            "volume": -50,
            "levels": -40,
            "volatility": 0,
            "relative_strength": -60,
            "market_regime": -70,
        },
        regime_score=-70,
    )
    result = QualityGate(Settings(_env_file=None)).evaluate(conflicted)

    assert result.decision == QualityGateDecision.REJECT
    assert {"volume", "levels", "relative strength", "market regime"}.issubset(
        result.contradicting_factors
    )


@pytest.mark.parametrize(
    ("direction", "regime"),
    [(IdeaDirection.BUY, -75), (IdeaDirection.SELL, 75)],
)
def test_strong_opposing_market_rejects_ordinary_signal(
    direction: IdeaDirection, regime: float
) -> None:
    sign = 1 if direction == IdeaDirection.BUY else -1
    ordinary = candidate(
        direction=direction,
        regime_score=regime,
        components={
            "trend": 60 * sign,
            "momentum": 40 * sign,
            "momentum_extreme": 15 * sign,
            "volume": 15 * sign,
            "levels": 15 * sign,
            "volatility": 10 * sign,
            "relative_strength": 20 * sign,
            "market_regime": regime,
        },
    )
    result = QualityGate(Settings(_env_file=None, quality_max_conflicts=7)).evaluate(ordinary)

    assert result.decision == QualityGateDecision.REJECT
    assert any("strong market regime" in reason for reason in result.reasons)


class FakeResponse:
    def __init__(self, payload: dict[str, object], status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("POST", "https://provider.invalid/test")
            response = httpx.Response(self.status_code, request=request, json=self.payload)
            response.raise_for_status()

    def json(self) -> dict[str, object]:
        return self.payload


class FakeClient:
    def __init__(
        self,
        payload: dict[str, object] | FakeResponse | Exception | list[object],
    ) -> None:
        self.payloads = list(payload) if isinstance(payload, list) else [payload]
        self.requests: list[dict[str, object]] = []

    async def post(self, url: str, **kwargs: object) -> FakeResponse:
        self.requests.append({"url": url, **kwargs})
        outcome = self.payloads.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, FakeResponse):
            return outcome
        assert isinstance(outcome, dict)
        return FakeResponse(outcome)


def analysis_body(**overrides: object) -> dict[str, object]:
    body = {
        "verdict": "APPROVE",
        "score": 82,
        "analysis_confidence": "HIGH",
        "bull_case": "Trend and volume agree.",
        "bear_case": "Market reversal remains possible.",
        "key_risks": ["Volatility expansion"],
        "why_now": "Price is near confirmed support.",
        "invalidation_conditions": ["Close below stop"],
        "short_summary": "Independent factors support the setup.",
    }
    body.update(overrides)
    return body


def gemini_payload(**overrides: object) -> dict[str, object]:
    body = analysis_body(**overrides)
    return {
        "candidates": [{"content": {"parts": [{"text": json.dumps(body)}]}}],
        "usageMetadata": {
            "promptTokenCount": 100,
            "candidatesTokenCount": 50,
            "thoughtsTokenCount": 5,
            "totalTokenCount": 155,
        },
        "modelVersion": "gemini-3.6-flash",
    }


def openai_payload(**overrides: object) -> dict[str, object]:
    body = analysis_body(**overrides)
    return {
        "output": [{"content": [{"type": "output_text", "text": json.dumps(body)}]}],
        "usage": {"input_tokens": 100, "output_tokens": 50},
        "model": "gpt-5-mini-2026-01-01",
    }


def test_gemini_is_the_default_provider() -> None:
    settings = Settings(_env_file=None)

    assert settings.ai_provider == "gemini"
    assert settings.ai_model == "gemini-3.6-flash"
    assert settings.ai_fallback_model == "gemini-flash-lite-latest"


@pytest.mark.asyncio
async def test_gemini_schema_compact_snapshot_and_usage_telemetry() -> None:
    settings = Settings(_env_file=None, gemini_api_key="test")
    quant = candidate()
    quality = QualityGate(settings).evaluate(quant)
    client = FakeClient(gemini_payload())

    review = await AIAnalystService(settings, client=client).review(quant, quality)

    assert review.approved
    assert review.analysis.score == 82
    assert review.provider == "gemini"
    assert review.model == "gemini-3.6-flash"
    assert review.input_tokens == 100
    assert review.output_tokens == 55
    assert review.usage is not None
    request = client.requests[0]["json"]
    assert isinstance(request, dict)
    assert client.requests[0]["url"].endswith("/models/gemini-3.6-flash:generateContent")
    assert client.requests[0]["headers"]["x-goog-api-key"] == "test"
    config = request["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    schema = config["responseJsonSchema"]
    assert {"score", "analysis_confidence"}.issubset(schema["properties"])
    assert "ai_score" not in schema["properties"]
    snapshot = json.loads(request["contents"][0]["parts"][0]["text"])
    assert snapshot["ticker"] == "SBER"
    assert "candles" not in snapshot
    assert "ohlcv" not in snapshot
    assert "Never invent" in request["systemInstruction"]["parts"][0]["text"]


@pytest.mark.asyncio
async def test_current_gemini_review_reuses_contract_and_includes_quality_context() -> None:
    settings = Settings(_env_file=None, gemini_api_key="test")
    quant = candidate()
    quality = QualityGate(settings).evaluate(quant)
    client = FakeClient(gemini_payload())

    review = await AIAnalystService(settings, client=client).review_current(quant, quality)

    assert review.status == "OK"
    request = client.requests[0]["json"]
    snapshot = json.loads(request["contents"][0]["parts"][0]["text"])
    assert snapshot["quality_gate_result"] == quality.decision.value
    assert snapshot["quality_gate_reasons"] == list(quality.reasons)
    assert "candles" not in snapshot
    assert request["generationConfig"]["responseMimeType"] == "application/json"
    assert "Never invent" in request["systemInstruction"]["parts"][0]["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"candidates": [{"content": {"parts": [{"text": "not-json"}]}}]},
        gemini_payload(verdict="MAYBE"),
    ],
)
async def test_gemini_malformed_or_invalid_enum_fails_closed_without_fallback(
    payload: dict[str, object],
) -> None:
    settings = Settings(_env_file=None, gemini_api_key="test")
    quant = candidate()
    quality = QualityGate(settings).evaluate(quant)

    client = FakeClient(payload)
    review = await AIAnalystService(settings, client=client).review(quant, quality)

    assert review.analysis.verdict == "WAIT"
    assert review.status == "AI_NOT_REVIEWED"
    assert not review.approved
    assert not review.fallback_used
    assert len(client.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "primary_failure",
    [
        httpx.ReadTimeout("timeout"),
        FakeResponse({"error": {"message": "quota"}}, status_code=429),
    ],
)
async def test_gemini_timeout_or_rate_limit_uses_flash_lite_once(
    primary_failure: Exception | FakeResponse,
) -> None:
    settings = Settings(_env_file=None, gemini_api_key="test")
    quant = candidate()
    quality = QualityGate(settings).evaluate(quant)
    fallback_payload = gemini_payload()
    fallback_payload["modelVersion"] = "gemini-flash-lite-latest"
    client = FakeClient([primary_failure, fallback_payload])

    review = await AIAnalystService(settings, client=client).review(quant, quality)

    assert review.approved
    assert review.fallback_used
    assert review.model == "gemini-flash-lite-latest"
    assert review.request_count == 2
    assert review.error_count == 1
    assert len(client.requests) == 2
    assert client.requests[1]["url"].endswith("/models/gemini-flash-lite-latest:generateContent")


@pytest.mark.asyncio
async def test_both_gemini_models_unavailable_is_not_reviewed_wait() -> None:
    settings = Settings(_env_file=None, gemini_api_key="test")
    quant = candidate()
    quality = QualityGate(settings).evaluate(quant)
    client = FakeClient([httpx.ReadTimeout("primary"), httpx.ReadTimeout("fallback")])

    review = await AIAnalystService(settings, client=client).review(quant, quality)

    assert review.analysis.verdict == "WAIT"
    assert review.status == "AI_NOT_REVIEWED"
    assert review.fallback_used
    assert review.model == "gemini-flash-lite-latest"
    assert review.request_count == 2
    assert not review.approved
    assert len(client.requests) == 2


@pytest.mark.asyncio
async def test_openai_provider_remains_available() -> None:
    settings = Settings(
        _env_file=None,
        ai_provider="openai",
        ai_model="gpt-5-mini",
        openai_api_key="test",
    )
    quant = candidate()
    quality = QualityGate(settings).evaluate(quant)
    client = FakeClient(openai_payload())

    review = await AIAnalystService(settings, client=client).review(quant, quality)

    assert review.approved
    assert review.provider == "openai"
    assert review.model == "gpt-5-mini-2026-01-01"
    assert len(client.requests) == 1
    request = client.requests[0]["json"]
    assert request["text"]["format"]["strict"] is True


@pytest.mark.asyncio
async def test_ai_unavailable_fails_closed_without_http_request() -> None:
    settings = Settings(_env_file=None, gemini_api_key="")
    quant = candidate()
    quality = QualityGate(settings).evaluate(quant)

    client = FakeClient(gemini_payload())
    review = await AIAnalystService(settings, client=client).review(quant, quality)

    assert review.analysis.verdict == "WAIT"
    assert review.status == "AI_NOT_REVIEWED"
    assert "GEMINI_API_KEY" in review.error
    assert client.requests == []


@pytest.mark.asyncio
async def test_fallback_attempts_persist_exact_provider_model_and_usage() -> None:
    engine, factory = create_engine_and_session("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    settings = Settings(_env_file=None, gemini_api_key="test")
    quant = candidate()
    quality = QualityGate(settings).evaluate(quant)
    apply_quality_result(quant, quality, strategy_version=settings.strategy_version)
    fallback_payload = gemini_payload()
    fallback_payload["modelVersion"] = "gemini-flash-lite-latest"
    review = await AIAnalystService(
        settings,
        client=FakeClient([httpx.ReadTimeout("primary"), fallback_payload]),
    ).review(quant, quality)

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
        stored = await session.get(CandidateExperiment, experiment_id)
        requests = list(
            await session.scalars(
                select(AIRequestLog)
                .where(AIRequestLog.candidate_id == experiment_id)
                .order_by(AIRequestLog.id)
            )
        )

    assert stored is not None
    assert stored.ai_provider == "gemini"
    assert stored.ai_model == "gemini-flash-lite-latest"
    assert stored.ai_fallback_used
    assert "attempts" in json.loads(stored.ai_usage_json)
    assert [(item.model, item.fallback_used) for item in requests] == [
        ("gemini-3.6-flash", False),
        ("gemini-flash-lite-latest", True),
    ]
    assert json.loads(requests[1].usage_json)["totalTokenCount"] == 155
    await engine.dispose()


@pytest.mark.asyncio
async def test_cooldown_and_experiment_tracking_persistence() -> None:
    engine, factory = create_engine_and_session("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    quant = candidate()
    quality = QualityGate(Settings(_env_file=None)).evaluate(quant)
    apply_quality_result(quant, quality, strategy_version="v2_ai_quality_filter")
    async with factory() as session, session.begin():
        await upsert_instruments(
            session, [InstrumentData("SBER", "TQBR", "Сбербанк", daily_turnover=1e9)]
        )
        created = await create_or_update_idea(
            session,
            quant,
            material_hash=idea_material_hash(quant),
            confidence_delta=7.5,
        )
        await save_candidate_experiment(
            session,
            quant,
            quality,
            published=True,
            publish_reason="PUBLISHED",
            published_idea_id=created.idea.id,
        )
    async with factory() as session:
        reason = await cooldown_reason(session, quant, cooldown_hours=24, confidence_delta=7.5)
        candidates = await session.scalar(select(func.count()).select_from(CandidateExperiment))
        requests = await session.scalar(select(func.count()).select_from(AIRequestLog))
    assert reason == "OPEN_DUPLICATE"
    assert candidates == 1
    assert requests == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_rejected_candidate_lifecycle_records_actual_result() -> None:
    engine, factory = create_engine_and_session("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    quant = candidate()
    quality = QualityGate(Settings(_env_file=None)).evaluate(quant)
    apply_quality_result(quant, quality, strategy_version="v2_ai_quality_filter")
    async with factory() as session, session.begin():
        await upsert_instruments(
            session, [InstrumentData("SBER", "TQBR", "Сбербанк", daily_turnover=1e9)]
        )
        experiment = await save_candidate_experiment(
            session,
            quant,
            quality,
            published=False,
            publish_reason="AI_REJECT",
        )
        experiment_id = experiment.id

    first_begin = quant.source_candle_begin + timedelta(minutes=15)
    activation = SimpleNamespace(
        begin=first_begin,
        end=first_begin + timedelta(minutes=15),
        open=quant.entry_price_to,
        high=quant.entry_price_to,
        low=quant.entry_price_from,
        close=quant.entry_price_to,
    )
    target = SimpleNamespace(
        begin=first_begin + timedelta(minutes=15),
        end=first_begin + timedelta(minutes=30),
        open=quant.entry_price_to,
        high=quant.take_profit,
        low=max(quant.stop_loss + 0.01, quant.entry_price_from),
        close=quant.take_profit,
    )

    transitions = await CandidateExperimentTracker(factory).track_candidate(
        experiment_id, [activation, target]
    )

    async with factory() as session:
        stored = await session.get(CandidateExperiment, experiment_id)
    assert transitions == 2
    assert stored is not None
    assert stored.status == "TP_HIT"
    assert stored.actual_r is not None and stored.actual_r > 0
    await engine.dispose()


class BatchIdeas:
    def __init__(self, factory, candidates):
        self.factory = factory
        self.candidates = candidates

    async def generate_candidate(self, ticker, horizon):
        if horizon != IdeaHorizon.POSITION_1M:
            return None
        return self.candidates[ticker]

    async def persist_candidate(self, quant):
        async with self.factory() as session, session.begin():
            return await create_or_update_idea(
                session,
                quant,
                material_hash=idea_material_hash(quant),
                confidence_delta=7.5,
            )


class ApprovingAI:
    async def review(self, _candidate, _quality):
        return AIReviewResult(
            analysis=AIAnalysis(
                verdict="APPROVE",
                score=80,
                analysis_confidence="HIGH",
                bull_case="Multiple factors agree.",
                bear_case="The setup can still fail.",
                key_risks=["Volatility"],
                why_now="Entry is near a calculated level.",
                invalidation_conditions=["Stop is reached"],
                short_summary="Approved by structured review.",
            ),
            provider="openai",
            model="test-model",
            status="OK",
            input_tokens=10,
            output_tokens=5,
            estimated_cost_usd=0.001,
            latency_ms=5,
            reviewed_at=datetime.now(UTC),
        )


class NoopIngestion:
    async def sync_universe(self):
        return []

    async def sync_all(self):
        return {"candles": 0, "orderbook_levels": 0, "errors": 0}


class NoopTracker:
    async def track_all(self):
        return {"evaluated": 0, "transitions": 0, "expired": 0}


@pytest.mark.asyncio
async def test_v2_scanner_ranks_then_applies_top_n() -> None:
    engine, factory = create_engine_and_session("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    tickers = ("SBER", "GAZP", "LKOH")
    async with factory() as session, session.begin():
        await upsert_instruments(
            session,
            [InstrumentData(ticker, "TQBR", ticker, daily_turnover=1e9) for ticker in tickers],
        )
    base = candidate()
    candidates = {
        ticker: replace(
            base,
            ticker=ticker,
            instrument_name=ticker,
            horizon=IdeaHorizon.POSITION_1M,
            primary_timeframe="15m",
            technical_score=score,
            total_score=score,
            confidence=min(95, 50 + score * 0.45),
            expires_at=NOW + timedelta(days=30),
            observation_mode="PAPER",
        )
        for ticker, score in zip(tickers, (80.0, 70.0, 60.0), strict=True)
    }
    settings = Settings(
        _env_file=None,
        quality_ai_candidates_per_horizon=3,
        position_max_new_ideas_per_scan=2,
        position_max_new_ideas_per_day=2,
    )
    scanner = MarketScanner(
        factory,
        NoopIngestion(),
        BatchIdeas(factory, candidates),
        NoopTracker(),
        settings=settings,
        quality_gate=QualityGate(settings),
        ai_analyst=ApprovingAI(),
    )

    result = await scanner.scan_ideas()

    assert result["ideas_created"] == 2
    assert result["rank_suppressed"] == 1
    async with factory() as session:
        experiments = list(
            await session.scalars(
                select(CandidateExperiment).order_by(CandidateExperiment.final_quality_score.desc())
            )
        )
        request_count = await session.scalar(select(func.count()).select_from(AIRequestLog))
    assert len(experiments) == 3
    assert request_count == 3
    assert [item.published for item in experiments] == [True, True, False]
    assert experiments[-1].publish_reason == "TOP_N_LIMIT"
    await engine.dispose()


class RecordingBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, chat_id, text, **_kwargs):
        self.messages.append((chat_id, text))


@pytest.mark.asyncio
async def test_notification_preferences_suppress_disabled_event_type() -> None:
    engine, factory = create_engine_and_session("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    settings = Settings(_env_file=None)
    quant = candidate()
    async with factory() as session, session.begin():
        await upsert_instruments(
            session, [InstrumentData("SBER", "TQBR", "Сбербанк", daily_turnover=1e9)]
        )
        user = await ensure_user(session, 77, "owner", "15m", 1, "strong", "all", 60)
        user.created_at = NOW - timedelta(minutes=1)
        user.notify_new_idea = False
        await create_or_update_idea(
            session,
            quant,
            material_hash=idea_material_hash(quant),
            confidence_delta=7.5,
        )
    operations = OperationalService(settings, factory, DataFreshnessGuard(settings, factory))
    reporting = ForwardReportingService(settings, factory, operations)
    bot = RecordingBot()

    result = await reporting.dispatch_notifications(bot, now=NOW)

    assert result["sent"] == 0
    assert bot.messages == []
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(("watch_notifications", "expected_sent"), ((True, 1), (False, 0)))
async def test_watch_notifications_apply_only_when_enabled(
    watch_notifications: bool,
    expected_sent: int,
) -> None:
    engine, factory = create_engine_and_session("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    settings = Settings(_env_file=None)
    quant = candidate()
    async with factory() as session, session.begin():
        await upsert_instruments(
            session, [InstrumentData("SBER", "TQBR", "Сбербанк", daily_turnover=1e9)]
        )
        user = await ensure_user(session, 77, "owner", "15m", 1, "strong", "all", 60)
        user.created_at = NOW - timedelta(minutes=2)
        user.notify_new_idea = False
        user.notify_watchlist = watch_notifications
        session.add(
            WatchlistItem(
                telegram_id=user.telegram_id,
                secid="SBER",
                created_at=NOW - timedelta(minutes=1),
            )
        )
        await create_or_update_idea(
            session,
            quant,
            material_hash=idea_material_hash(quant),
            confidence_delta=7.5,
        )
    operations = OperationalService(settings, factory, DataFreshnessGuard(settings, factory))
    reporting = ForwardReportingService(settings, factory, operations)
    bot = RecordingBot()

    result = await reporting.dispatch_notifications(bot, now=NOW)

    assert result["sent"] == expected_sent
    assert len(bot.messages) == expected_sent
    await engine.dispose()


@pytest.mark.asyncio
async def test_follow_subscription_delivers_only_future_lifecycle_event() -> None:
    engine, factory = create_engine_and_session("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    settings = Settings(_env_file=None)
    quant = candidate()
    async with factory() as session, session.begin():
        await upsert_instruments(
            session, [InstrumentData("SBER", "TQBR", "Сбербанк", daily_turnover=1e9)]
        )
        user = await ensure_user(session, 77, "owner", "15m", 1, "strong", "all", 60)
        user.created_at = NOW - timedelta(minutes=1)
        user.notify_new_idea = False
        user.notify_activation = False
        created = await create_or_update_idea(
            session,
            quant,
            material_hash=idea_material_hash(quant),
            confidence_delta=7.5,
        )
        created.idea.status = "ACTIVE"
        created.idea.activated_at = NOW + timedelta(minutes=2)
        created.idea.activation_price = created.idea.entry_price_from
        session.add(
            IdeaFollow(
                telegram_id=user.telegram_id,
                idea_id=created.idea.id,
                created_at=NOW + timedelta(minutes=1),
            )
        )
        session.add(
            TradingIdeaEvent(
                idea_id=created.idea.id,
                event_type="ACTIVATED",
                from_status="PENDING_ENTRY",
                to_status="ACTIVE",
                price=created.idea.activation_price,
                details="entry zone reached",
                occurred_at=NOW + timedelta(minutes=2),
            )
        )
    operations = OperationalService(settings, factory, DataFreshnessGuard(settings, factory))
    reporting = ForwardReportingService(settings, factory, operations)
    bot = RecordingBot()

    result = await reporting.dispatch_notifications(bot, now=NOW + timedelta(minutes=3))

    assert result["sent"] == 1
    assert len(bot.messages) == 1
    assert "IDEA ACTIVATED" in bot.messages[0][1]
    await engine.dispose()


@pytest.mark.asyncio
async def test_button_ux_and_settings_persistence() -> None:
    menu_labels = [button.text for row in main_menu().keyboard for button in row]
    assert menu_labels == [
        "🔥 Лучшие идеи",
        "👁 Отслеживаемые",
        "📊 Активные идеи",
        "📒 Результаты сигналов",
        "🌍 Рынок сейчас",
        "🔎 Проверить акцию",
        "📈 Статистика",
        "⚙️ Настройки",
        "🩺 Система",
    ]
    settings_labels = [
        button.text for row in settings_menu_keyboard().inline_keyboard for button in row
    ]
    assert any("AI filter" in label for label in settings_labels)
    assert any("Watch notifications" in label for label in settings_labels)
    section_callbacks = [
        button.callback_data for row in idea_sections_keyboard(42).inline_keyboard for button in row
    ]
    assert section_callbacks == [
        "idea_ai:42",
        "idea_tech:42",
        "idea_fund:42",
        "idea_market:42",
        "idea_history:42",
        "best:menu",
    ]

    engine, factory = create_engine_and_session("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    async with factory() as session, session.begin():
        user = await ensure_user(session, 1, "owner", "15m", 1, "strong", "all", 70)
        await update_user_settings(
            session,
            1,
            ai_filter_enabled=False,
            notify_sl=False,
            notify_daily_summary=False,
            notify_watchlist=False,
            minimum_confidence=85,
        )
    async with factory() as session:
        user = await session.get(TelegramUser, 1)
        assert user is not None
        assert not user.ai_filter_enabled
        assert not user.notify_sl
        assert not user.notify_daily_summary
        assert not user.notify_watchlist
        assert user.minimum_confidence == 85
        current_labels = [
            button.text for row in settings_menu_keyboard(user).inline_keyboard for button in row
        ]
        assert "📨 Отчёты: Только сильные ✅" in current_labels
        assert "⏱ Горизонт: Все ✅" in current_labels
        assert "🎯 Min strength: 85+ ✅" in current_labels
        assert "🧠 AI filter: OFF" in current_labels
        assert "🔔 Watch notifications: OFF" in current_labels
        callbacks = [
            button.callback_data
            for row in settings_values_keyboard("notifications", user).inline_keyboard
            for button in row
        ]
        assert "toggle_notify:sl" in callbacks
        assert "settings:menu" in callbacks
    await engine.dispose()
