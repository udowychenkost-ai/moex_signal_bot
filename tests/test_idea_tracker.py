from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.config import Settings
from app.db import create_engine_and_session, init_db
from app.domain import CandleData, IdeaDirection, IdeaHorizon, IdeaStatus, InstrumentData
from app.idea_repository import create_or_update_idea
from app.idea_tracker import IdeaTracker, evaluate_idea_candle
from app.ideas import build_trading_idea, idea_material_hash
from app.models import TradingIdea, TradingIdeaEvent
from app.repositories import upsert_candles, upsert_instruments
from tests.test_ideas import NOW, signal


def trackable(
    *,
    direction: IdeaDirection = IdeaDirection.BUY,
    status: IdeaStatus = IdeaStatus.PENDING_ENTRY,
    expires_at: datetime | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        direction=direction.value,
        status=status.value,
        entry_price_from=99.0,
        entry_price_to=100.0,
        take_profit=106.0 if direction == IdeaDirection.BUY else 94.0,
        stop_loss=96.0 if direction == IdeaDirection.BUY else 104.0,
        expires_at=expires_at or NOW + timedelta(days=1),
    )


def candle(
    *,
    opened: float,
    high: float,
    low: float,
    close: float,
    begin: datetime | None = None,
) -> SimpleNamespace:
    timestamp = begin or NOW + timedelta(minutes=15)
    return SimpleNamespace(
        begin=timestamp,
        end=timestamp + timedelta(minutes=15),
        open=opened,
        high=high,
        low=low,
        close=close,
    )


def test_pending_buy_activates_only_after_zone_touch() -> None:
    idea = trackable()
    assert evaluate_idea_candle(idea, candle(opened=102, high=103, low=101, close=102)) == []
    transitions = evaluate_idea_candle(idea, candle(opened=102, high=103, low=99.5, close=101))
    assert [item.to_status for item in transitions] == [IdeaStatus.ACTIVE]
    assert transitions[0].price == 100


def test_target_without_entry_is_invalidated_not_counted_as_win() -> None:
    transitions = evaluate_idea_candle(
        trackable(), candle(opened=102, high=107, low=101, close=106)
    )
    assert len(transitions) == 1
    assert transitions[0].to_status == IdeaStatus.INVALIDATED
    assert transitions[0].event_type == "ENTRY_MISSED"


def test_active_candle_with_tp_and_sl_uses_conservative_stop_first() -> None:
    transitions = evaluate_idea_candle(
        trackable(status=IdeaStatus.ACTIVE),
        candle(opened=100, high=107, low=95, close=103),
    )
    assert len(transitions) == 1
    assert transitions[0].to_status == IdeaStatus.SL_HIT
    assert transitions[0].price == 96


def test_sell_activation_and_target_are_directional() -> None:
    idea = trackable(direction=IdeaDirection.SELL)
    activation = evaluate_idea_candle(idea, candle(opened=99, high=100.5, low=98, close=99))
    assert [item.to_status for item in activation] == [IdeaStatus.ACTIVE]
    idea.status = IdeaStatus.ACTIVE.value
    closed = evaluate_idea_candle(idea, candle(opened=99, high=100, low=93, close=94))
    assert closed[0].to_status == IdeaStatus.TP_HIT
    assert closed[0].price == 94


def test_expiry_precedes_price_events_after_deadline() -> None:
    expires = NOW + timedelta(hours=1)
    transitions = evaluate_idea_candle(
        trackable(status=IdeaStatus.ACTIVE, expires_at=expires),
        candle(
            opened=100,
            high=110,
            low=90,
            close=101,
            begin=expires + timedelta(minutes=1),
        ),
    )
    assert transitions[0].to_status == IdeaStatus.EXPIRED


def test_candle_straddling_expiry_cannot_claim_post_deadline_target() -> None:
    expires = NOW + timedelta(hours=1)
    transitions = evaluate_idea_candle(
        trackable(status=IdeaStatus.ACTIVE, expires_at=expires),
        candle(
            opened=100,
            high=110,
            low=99,
            close=108,
            begin=expires - timedelta(minutes=10),
        ),
    )

    assert len(transitions) == 1
    assert transitions[0].to_status == IdeaStatus.EXPIRED


@pytest.mark.asyncio
async def test_tracker_persists_activation_once_and_advances_progress() -> None:
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
    assert data.status == IdeaStatus.PENDING_ENTRY
    async with factory() as session, session.begin():
        result = await create_or_update_idea(
            session,
            data,
            material_hash=idea_material_hash(data),
            confidence_delta=7.5,
        )
        idea_id = result.idea.id

    next_begin = NOW + timedelta(minutes=15)
    market_candle = CandleData(
        "SBER",
        "TQBR",
        "15m",
        next_begin,
        next_begin + timedelta(minutes=15),
        102,
        103,
        99,
        101,
        1000,
        100_000,
    )
    async with factory() as session, session.begin():
        await upsert_candles(session, [market_candle])

    tracker = IdeaTracker(factory)
    first = await tracker.track_all(now=next_begin)
    second = await tracker.track_all(now=next_begin)

    assert first["transitions"] == 1
    assert second["transitions"] == 0
    assert second["evaluated"] == 0
    async with factory() as session:
        stored = await session.get(TradingIdea, idea_id)
        event_count = await session.scalar(select(func.count()).select_from(TradingIdeaEvent))
    assert stored is not None
    assert stored.status == IdeaStatus.ACTIVE.value
    assert stored.activated_at is not None
    assert event_count == 2
    await engine.dispose()
