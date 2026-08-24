from __future__ import annotations

from datetime import UTC, datetime

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import EditMessageText, SendMessage
from aiogram.types import Chat, Message, Update, User
from sqlalchemy import select

from app.ai_analyst import AIAnalysis, AIReviewResult
from app.ai_ux import (
    AI_VERDICT_LABELS,
    format_current_ai_analysis,
    format_historical_ai_analysis,
)
from app.bot import BotServices, create_router, main_menu
from app.config import Settings
from app.models import AIRequestLog, TradingIdea, TradingIdeaSnapshot
from app.on_demand_ai import CurrentAIResult, OnDemandAIService
from app.quality import QualityGate
from app.telegram_ui import idea_context_keyboard
from tests.test_ideas import signal
from tests.test_telegram_context import callback_update, seeded_context
from tests.test_v2_quality_ai import candidate


def successful_review() -> AIReviewResult:
    return AIReviewResult(
        analysis=AIAnalysis(
            verdict="APPROVE",
            score=82,
            analysis_confidence="HIGH",
            bull_case="Тренд и относительная сила подтверждают сценарий.",
            bear_case="Объём пока не экстремальный.",
            why_now="Цена удерживает расчётную область поддержки.",
            key_risks=["Резкое ухудшение рынка", "Потеря поддержки"],
            invalidation_conditions=["Закрытие ниже стоп-уровня"],
            short_summary="Сценарий подтверждён с контролируемым риском.",
        ),
        provider="gemini",
        model="gemini-2.5-flash",
        status="OK",
        input_tokens=100,
        output_tokens=50,
        estimated_cost_usd=0.001,
        latency_ms=120,
        reviewed_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_missing_historical_ai_review_is_compact_and_has_current_action() -> None:
    engine, factory, idea_id = await seeded_context()
    async with factory() as session:
        idea = await session.get(TradingIdea, idea_id)
        assert idea is not None
        text = format_historical_ai_analysis(idea)
        keyboard = idea_context_keyboard(idea, watched=False, followed=False)

    assert "AI-анализ для этой идеи не выполнялся" in text
    assert "Возможные причины" in text
    assert "NOT_REQUESTED" not in text
    assert "AI_NOT_REVIEWED" not in text
    assert "n/a/100" not in text
    assert "Почему интересно" not in text
    assert "Что против" not in text
    assert any(
        button.text == "🧠 Проанализировать сейчас"
        and button.callback_data == f"idea_ai_now:{idea_id}"
        for row in keyboard.inline_keyboard
        for button in row
    )
    await engine.dispose()


@pytest.mark.asyncio
async def test_real_gemini_review_uses_russian_labels_and_nonempty_sections() -> None:
    engine, factory, idea_id = await seeded_context()
    async with factory() as session, session.begin():
        idea = await session.get(TradingIdea, idea_id)
        assert idea is not None
        review = successful_review()
        idea.ai_verdict = review.analysis.verdict
        idea.ai_score = review.analysis.score
        idea.ai_provider = review.provider
        idea.ai_reviewed_at = review.reviewed_at
        idea.ai_bull_case = review.analysis.bull_case
        idea.ai_bear_case = review.analysis.bear_case
        idea.ai_why_now = review.analysis.why_now
        idea.ai_key_risks = '["Резкое ухудшение рынка"]'
        idea.ai_invalidation_conditions = '["Закрытие ниже стоп-уровня"]'
        text = format_historical_ai_analysis(idea)
        keyboard = idea_context_keyboard(idea, watched=False, followed=False)

    assert "🧠 <b>Gemini</b>" in text
    assert "Вердикт: <b>Подтверждено</b>" in text
    assert "AI-оценка: <b>82/100</b>" in text
    assert all(
        label in text
        for label in (
            "Почему интересно:",
            "Что против:",
            "Почему сейчас:",
            "Главные риски:",
            "Условия отмены:",
        )
    )
    assert "APPROVE" not in text
    assert not any(
        button.callback_data == f"idea_ai_now:{idea_id}"
        for row in keyboard.inline_keyboard
        for button in row
    )
    assert AI_VERDICT_LABELS == {
        "STRONG_APPROVE": "Сильное подтверждение",
        "APPROVE": "Подтверждено",
        "WAIT": "Подождать",
        "REJECT": "Отклонено",
    }
    await engine.dispose()


class FakeIngestion:
    def __init__(self) -> None:
        self.refreshed: list[tuple[str, str]] = []

    async def refresh_ticker(self, ticker: str, timeframe: str) -> int:
        self.refreshed.append((ticker, timeframe))
        return 1

    async def sync_market_candles(self, symbol: str, timeframe: str) -> int:
        del symbol, timeframe
        return 1


class FakeIdeas:
    def __init__(self, current_candidate) -> None:
        self.current_candidate = current_candidate

    async def generate_candidate(self, ticker, horizon):
        del ticker, horizon
        return self.current_candidate


class FakeAnalyst:
    def __init__(self, review: AIReviewResult) -> None:
        self.review = review
        self.calls = 0

    async def review_current(self, current_candidate, quality):
        del current_candidate, quality
        self.calls += 1
        return self.review


@pytest.mark.asyncio
async def test_on_demand_analysis_preserves_idea_and_immutable_snapshot() -> None:
    engine, factory, idea_id = await seeded_context()
    current_candidate = candidate()
    review = successful_review()
    ingestion = FakeIngestion()
    analyst = FakeAnalyst(review)
    settings = Settings(_env_file=None, market_context_enabled=False)
    service = OnDemandAIService(
        settings,
        factory,
        ingestion,  # type: ignore[arg-type]
        FakeIdeas(current_candidate),  # type: ignore[arg-type]
        analyst,  # type: ignore[arg-type]
        QualityGate(settings),
    )
    async with factory() as session:
        idea = await session.get(TradingIdea, idea_id)
        snapshot = await session.get(TradingIdeaSnapshot, idea_id)
        assert idea is not None
        assert snapshot is not None
        before_idea = (idea.ai_verdict, idea.ai_score, idea.ai_provider, idea.updated_at)
        before_snapshot = (
            snapshot.ai_verdict,
            snapshot.ai_score,
            snapshot.ai_provider,
            snapshot.total_score,
        )

    result = await service.analyze(idea)

    assert result.completed
    assert analyst.calls == 1
    assert ingestion.refreshed
    async with factory() as session:
        stored = await session.get(TradingIdea, idea_id)
        snapshot = await session.get(TradingIdeaSnapshot, idea_id)
        logs = list(
            await session.scalars(
                select(AIRequestLog).where(AIRequestLog.request_kind == "ON_DEMAND_IDEA")
            )
        )
    assert stored is not None and snapshot is not None
    assert (
        stored.ai_verdict,
        stored.ai_score,
        stored.ai_provider,
        stored.updated_at,
    ) == before_idea
    assert (
        snapshot.ai_verdict,
        snapshot.ai_score,
        snapshot.ai_provider,
        snapshot.total_score,
    ) == before_snapshot
    assert len(logs) == 1
    assert logs[0].provider == "gemini"
    assert "AI-анализ выполнен сейчас" in format_current_ai_analysis(review, current_candidate)
    await engine.dispose()


class InteractiveBot(Bot):
    def __init__(self) -> None:
        super().__init__("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefgh")
        self.methods: list[object] = []
        self.message_id = 100

    async def __call__(self, method, request_timeout=None):
        del request_timeout
        self.methods.append(method)
        if isinstance(method, SendMessage):
            self.message_id += 1
            return Message(
                message_id=self.message_id,
                date=datetime.now(UTC),
                chat=Chat(id=int(method.chat_id), type="private"),
                text=method.text,
            ).as_(self)
        return True


class MenuSignals:
    async def generate(self, secid, timeframe, *, risk_per_trade_pct):
        del secid, risk_per_trade_pct
        return signal(timeframe, 65)


def message_update(text: str, *, update_id: int, reply_text: str | None = None) -> Update:
    user = User(id=101, is_bot=False, first_name="Tester", username="first")
    chat = Chat(id=101, type="private")
    reply = (
        Message(
            message_id=update_id,
            date=datetime.now(UTC),
            chat=chat,
            from_user=user,
            text=reply_text,
        )
        if reply_text is not None
        else None
    )
    return Update(
        update_id=update_id,
        message=Message(
            message_id=update_id + 100,
            date=datetime.now(UTC),
            chat=chat,
            from_user=user,
            text=text,
            reply_to_message=reply,
        ),
    )


@pytest.mark.asyncio
async def test_reply_keyboard_routes_watchlist_results_and_ticker_check() -> None:
    engine, factory, _ = await seeded_context()
    services = BotServices(
        settings=Settings(_env_file=None),
        session_factory=factory,
        ingestion=FakeIngestion(),  # type: ignore[arg-type]
        signals=MenuSignals(),  # type: ignore[arg-type]
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(create_router(services))
    bot = InteractiveBot()

    await dispatcher.feed_update(bot, message_update("👁 Отслеживаемые", update_id=1))
    await dispatcher.feed_update(bot, message_update("📒 Результаты сигналов", update_id=2))
    await dispatcher.feed_update(bot, message_update("🔎 Проверить акцию", update_id=3))
    await dispatcher.feed_update(
        bot,
        message_update(
            "SBER",
            update_id=4,
            reply_text="🔎 Проверить акцию\n\nВведите тикер MOEX, например SBER.",
        ),
    )

    sent = [method for method in bot.methods if isinstance(method, SendMessage)]
    edits = [method for method in bot.methods if isinstance(method, EditMessageText)]
    assert any("Watchlist" in method.text for method in sent), [
        (type(method).__name__, getattr(method, "text", "")) for method in bot.methods
    ]
    assert any("Результаты сигналов" in method.text for method in sent)
    assert any("Введите тикер MOEX" in method.text for method in sent)
    assert edits and "BUY · SBER" in edits[-1].text
    assert [button.text for row in main_menu().keyboard for button in row][-1] == "🩺 Система"
    await bot.session.close()
    await engine.dispose()


class FakeOnDemand:
    async def analyze(self, idea: TradingIdea) -> CurrentAIResult:
        del idea
        current_candidate = candidate()
        quality = QualityGate(Settings(_env_file=None)).evaluate(current_candidate)
        return CurrentAIResult(current_candidate, quality, successful_review())


@pytest.mark.asyncio
async def test_analyze_now_callback_edits_card_without_writing_historical_verdict() -> None:
    engine, factory, idea_id = await seeded_context()
    services = BotServices(
        settings=Settings(_env_file=None),
        session_factory=factory,
        ingestion=FakeIngestion(),  # type: ignore[arg-type]
        signals=MenuSignals(),  # type: ignore[arg-type]
        on_demand_ai=FakeOnDemand(),  # type: ignore[arg-type]
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(create_router(services))
    bot = InteractiveBot()

    await dispatcher.feed_update(bot, callback_update(f"idea_ai_now:{idea_id}"))

    edits = [method for method in bot.methods if isinstance(method, EditMessageText)]
    assert edits
    assert "AI-анализ выполнен сейчас" in edits[-1].text
    async with factory() as session:
        idea = await session.get(TradingIdea, idea_id)
    assert idea is not None
    assert idea.ai_verdict == "NOT_REQUESTED"
    assert idea.ai_score is None
    await bot.session.close()
    await engine.dispose()
