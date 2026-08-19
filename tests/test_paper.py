from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.config import Settings
from app.db import create_engine_and_session, init_db
from app.domain import IdeaHorizon, IdeaStatus, InstrumentData
from app.idea_repository import create_or_update_idea
from app.ideas import build_trading_idea, idea_material_hash
from app.models import PaperTrade, TradingIdea
from app.paper import PaperTradingService, format_paper_summary
from app.repositories import upsert_instruments
from tests.test_ideas import NOW, signal


@pytest.mark.asyncio
async def test_paper_trade_follows_published_idea_lifecycle_idempotently() -> None:
    engine, factory = create_engine_and_session("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    async with factory() as session, session.begin():
        await upsert_instruments(
            session,
            [InstrumentData("SBER", "TQBR", "Сбербанк", lot_size=10)],
        )
    candidate = build_trading_idea(
        Settings(_env_file=None),
        instrument_name="Сбербанк",
        horizon=IdeaHorizon.INTRADAY_1D,
        signals=[signal("15m", 60), signal("1h", 60), signal("1d", 60)],
        now=NOW,
    )
    assert candidate is not None
    active = replace(
        candidate,
        status=IdeaStatus.ACTIVE,
        activated_at=NOW,
        activation_price=candidate.entry_price_to,
    )
    async with factory() as session, session.begin():
        created = await create_or_update_idea(
            session,
            active,
            material_hash=idea_material_hash(active),
            confidence_delta=7.5,
        )
        idea_id = created.idea.id

    settings = Settings(
        _env_file=None,
        paper_account_size=100_000,
        default_risk_per_trade_pct=1,
        paper_commission_pct=0.05,
        paper_buy_slippage_bps=10,
        paper_sell_slippage_bps=20,
    )
    paper = PaperTradingService(settings, factory)
    opened = await paper.sync_idea(idea_id)
    duplicate = await paper.sync_idea(idea_id)

    assert opened is not None
    assert opened.status == "OPEN"
    assert opened.units % 10 == 0
    assert duplicate is not None
    assert duplicate.id == opened.id

    async with factory() as session, session.begin():
        idea = await session.get(TradingIdea, idea_id)
        assert idea is not None
        idea.status = IdeaStatus.TP_HIT.value
        idea.close_price = idea.take_profit
        idea.closed_at = NOW + timedelta(hours=4)
        idea.close_reason = "Достигнут Take Profit"
    closed = await paper.sync_idea(idea_id)
    summary = await paper.summary()

    assert closed is not None
    assert closed.status == "CLOSED"
    assert closed.net_pnl > 0
    assert closed.commission > 0
    assert closed.slippage > 0
    assert closed.entry_fill_price > closed.entry_price
    assert closed.exit_fill_price < closed.exit_price
    assert closed.r_multiple > 0
    assert summary.closed_trades == 1
    assert summary.wins == 1
    assert summary.net_pnl == pytest.approx(closed.net_pnl)
    assert "Paper trading" in format_paper_summary(summary)
    async with factory() as session:
        count = await session.scalar(select(func.count()).select_from(PaperTrade))
    assert count == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_pending_idea_does_not_open_paper_position() -> None:
    engine, factory = create_engine_and_session("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    service = PaperTradingService(Settings(_env_file=None), factory)
    assert await service.sync_idea(999) is None
    await engine.dispose()
