from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from app.config import Settings
from app.db import create_engine_and_session, init_db
from app.domain import GeneratedSignal, IdeaDirection, IdeaHorizon, IdeaStatus, InstrumentData
from app.idea_repository import create_or_update_idea
from app.ideas import (
    TradingIdeaGenerator,
    build_trading_idea,
    calculate_entry_zone,
    idea_material_hash,
)
from app.models import TradingIdea, TradingIdeaEvent
from app.repositories import upsert_instruments

NOW = datetime(2026, 8, 19, 10, tzinfo=UTC)


def signal(
    timeframe: str,
    score: float,
    *,
    price: float = 100,
    atr: float = 2,
    record_id: int | None = None,
) -> GeneratedSignal:
    action = "BUY" if score > 0 else "SELL"
    return GeneratedSignal(
        secid="SBER",
        timeframe=timeframe,
        horizon="intraday",
        action=action,
        technical_score=score,
        total_score=score,
        confidence=80,
        entry_price=price,
        stop_loss=price - 3 if action == "BUY" else price + 3,
        take_profit=price + 6 if action == "BUY" else price - 6,
        risk_pct=1,
        reward_risk_ratio=2,
        rationale=["Тренд подтверждён", "Объём выше среднего"],
        candle_begin=NOW,
        record_id=record_id,
        atr=atr,
        support_levels=[90, 99],
        resistance_levels=[101, 110],
    )


def test_buy_entry_zone_anchors_to_nearby_support() -> None:
    entry_from, entry_to = calculate_entry_zone(
        direction=IdeaDirection.BUY,
        current_price=100,
        atr=2,
        support_levels=[90, 99],
        resistance_levels=[101],
        zone_atr=0.4,
    )
    assert entry_from == pytest.approx(98.6)
    assert entry_to == pytest.approx(99.4)


def test_sell_entry_zone_anchors_to_nearby_resistance() -> None:
    entry_from, entry_to = calculate_entry_zone(
        direction=IdeaDirection.SELL,
        current_price=100,
        atr=2,
        support_levels=[99],
        resistance_levels=[101, 110],
        zone_atr=0.4,
    )
    assert entry_from == pytest.approx(100.6)
    assert entry_to == pytest.approx(101.4)


@pytest.mark.parametrize(
    ("score", "direction", "expected_status"),
    [
        (60.0, IdeaDirection.BUY, IdeaStatus.PENDING_ENTRY),
        (-60.0, IdeaDirection.SELL, IdeaStatus.PENDING_ENTRY),
    ],
)
def test_build_trading_idea_calculates_directional_risk(
    score: float,
    direction: IdeaDirection,
    expected_status: IdeaStatus,
) -> None:
    signals = [signal("15m", score, record_id=1), signal("1h", score), signal("1d", score)]
    idea = build_trading_idea(
        Settings(_env_file=None),
        instrument_name="Сбербанк",
        horizon=IdeaHorizon.INTRADAY_1D,
        signals=signals,
        now=NOW,
    )
    assert idea is not None
    assert idea.direction == direction
    assert idea.status == expected_status
    assert idea.risk_reward_ratio >= 2
    assert idea.expected_return_pct > 0
    assert idea.risk_pct > 0
    assert idea.expires_at > idea.created_at
    if direction == IdeaDirection.BUY:
        assert idea.stop_loss < idea.entry_price_to < idea.take_profit
    else:
        assert idea.take_profit < idea.entry_price_from < idea.stop_loss


def test_weak_or_incomplete_signal_does_not_become_an_idea() -> None:
    settings = Settings(_env_file=None)
    weak = [signal("15m", 10), signal("1h", 10), signal("1d", 10)]
    assert (
        build_trading_idea(
            settings,
            instrument_name="Сбербанк",
            horizon=IdeaHorizon.INTRADAY_1D,
            signals=weak,
            now=NOW,
        )
        is None
    )
    assert (
        build_trading_idea(
            settings,
            instrument_name="Сбербанк",
            horizon=IdeaHorizon.SWING_5D,
            signals=[signal("1h", 70), signal("1d", 70)],
            now=NOW,
        )
        is None
    )


def test_missing_optional_factors_preserve_technical_score() -> None:
    idea = build_trading_idea(
        Settings(_env_file=None),
        instrument_name="Сбербанк",
        horizon=IdeaHorizon.INTRADAY_1D,
        signals=[signal("15m", 60), signal("1h", 60), signal("1d", 60)],
        now=NOW,
    )

    assert idea is not None
    assert idea.technical_score == pytest.approx(60)
    assert idea.fundamental_score == 0
    assert idea.news_score == 0
    assert idea.total_score == pytest.approx(60)


def test_available_fundamental_and_news_factors_use_horizon_weights() -> None:
    idea = build_trading_idea(
        Settings(_env_file=None),
        instrument_name="Сбербанк",
        horizon=IdeaHorizon.POSITION_1M,
        signals=[signal("4h", 20), signal("1d", 20), signal("1w", 20)],
        fundamental_score=100,
        news_score=100,
        now=NOW,
    )

    assert idea is not None
    assert idea.direction == IdeaDirection.BUY
    assert idea.technical_score == pytest.approx(20)
    assert idea.total_score == pytest.approx(52)


@pytest.mark.asyncio
async def test_repository_deduplicates_and_versions_material_updates() -> None:
    engine, factory = create_engine_and_session("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    async with factory() as session, session.begin():
        await upsert_instruments(session, [InstrumentData("SBER", "TQBR", "Сбербанк")])

    data = build_trading_idea(
        Settings(_env_file=None),
        instrument_name="Сбербанк",
        horizon=IdeaHorizon.INTRADAY_1D,
        signals=[signal("15m", 60), signal("1h", 60), signal("1d", 60)],
        now=NOW,
    )
    assert data is not None
    async with factory() as session, session.begin():
        created = await create_or_update_idea(
            session,
            data,
            material_hash=idea_material_hash(data),
            confidence_delta=7.5,
        )
    assert created.created
    assert created.idea.version == 1

    async with factory() as session, session.begin():
        duplicate = await create_or_update_idea(
            session,
            data,
            material_hash=idea_material_hash(data),
            confidence_delta=7.5,
        )
    assert not duplicate.created
    assert not duplicate.materially_changed

    changed_data = replace(data, confidence=data.confidence + 10)
    async with factory() as session, session.begin():
        updated = await create_or_update_idea(
            session,
            changed_data,
            material_hash=idea_material_hash(changed_data),
            confidence_delta=7.5,
        )
    assert updated.materially_changed
    assert updated.idea.version == 2
    async with factory() as session:
        idea_count = await session.scalar(select(func.count()).select_from(TradingIdea))
        event_count = await session.scalar(select(func.count()).select_from(TradingIdeaEvent))
    assert idea_count == 1
    assert event_count == 2
    await engine.dispose()


class StubSignalService:
    async def generate(self, ticker: str, timeframe: str) -> GeneratedSignal:
        return signal(timeframe, 65)


@pytest.mark.asyncio
async def test_generator_uses_one_signal_service_and_persists_candidate() -> None:
    engine, factory = create_engine_and_session("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    async with factory() as session, session.begin():
        await upsert_instruments(session, [InstrumentData("SBER", "TQBR", "Сбербанк")])
    generator = TradingIdeaGenerator(Settings(_env_file=None), factory, StubSignalService())

    result = await generator.generate("sber", IdeaHorizon.INTRADAY_1D)

    assert result is not None
    assert result.created
    assert result.idea.ticker == "SBER"
    assert result.idea.source_signal_id is None
    await engine.dispose()
