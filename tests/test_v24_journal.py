from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select

from app.db import create_engine_and_session, init_db
from app.journal import (
    ActualTradeConfirmationRequired,
    append_trade_event,
    create_actual_trade,
    create_idea_journal,
    create_model_trade,
    list_trade_events,
)
from app.models import ActualTradeJournal, IdeaJournal, ModelTradeJournal, TradeEventJournal
from app.v24_domain import FillStatus, SampleType, TradeEventType

SIGNAL_TIME = datetime(2026, 9, 1, 8, 30, tzinfo=UTC)


def migration_config(database_url: str) -> Config:
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    return config


async def _database(tmp_path: Path, name: str = "journal.db"):
    database_url = f"sqlite+aiosqlite:///{(tmp_path / name).as_posix()}"
    engine, factory = create_engine_and_session(database_url)
    await init_db(engine)
    return engine, factory


async def test_reentry_gets_new_auditable_trade_id(tmp_path: Path) -> None:
    engine, factory = await _database(tmp_path)
    try:
        async with factory() as session, session.begin():
            first, _ = await create_idea_journal(
                session,
                signal_datetime=SIGNAL_TIME,
                ticker="sber",
                direction="BUY",
            )
            second, _ = await create_idea_journal(
                session,
                signal_datetime=SIGNAL_TIME,
                ticker="SBER",
                direction="LONG",
            )
            short, _ = await create_idea_journal(
                session,
                signal_datetime=SIGNAL_TIME,
                ticker="SBER",
                direction="SELL",
            )

        assert first.trade_id == "20260901-SBER-LONG-01"
        assert second.trade_id == "20260901-SBER-LONG-02"
        assert short.trade_id == "20260901-SBER-SHORT-01"
    finally:
        await engine.dispose()


async def test_trade_id_allocation_is_concurrency_safe(tmp_path: Path) -> None:
    engine, factory = await _database(tmp_path, "concurrent.db")

    async def create_one() -> str:
        async with factory() as session, session.begin():
            idea, _ = await create_idea_journal(
                session,
                signal_datetime=SIGNAL_TIME,
                ticker="GAZP",
                direction="LONG",
            )
            return idea.trade_id

    try:
        trade_ids = await asyncio.gather(*(create_one() for _ in range(8)))
        assert len(set(trade_ids)) == 8
        assert sorted(trade_ids) == [f"20260901-GAZP-LONG-{number:02d}" for number in range(1, 9)]
    finally:
        await engine.dispose()


async def test_model_and_actual_journals_are_separate_and_persist(tmp_path: Path) -> None:
    database_path = tmp_path / "restart.db"
    engine, factory = await _database(tmp_path, database_path.name)
    try:
        async with factory() as session, session.begin():
            idea, snapshot = await create_idea_journal(
                session,
                signal_datetime=SIGNAL_TIME,
                ticker="LKOH",
                direction="SHORT",
                journal_values={
                    "source_set": ["MOEX ISS"],
                    "optimal_entry": 6100.0,
                    "initial_stop": 6200.0,
                    "tp1": 5900.0,
                },
                snapshot_values={"gate_results": {"data_integrity": "PASS"}},
            )
            model = await create_model_trade(
                session,
                trade_id=idea.trade_id,
                sample_type=SampleType.FORWARD,
                fill_status=FillStatus.FILLED,
                calibration_eligible=True,
                values={"model_entry": 6100.0, "model_position_rub": 100_000.0},
            )
            actual = await create_actual_trade(
                session,
                trade_id=idea.trade_id,
                model_trade_id=model.model_trade_id,
                confirmed_by_user=True,
                telegram_id=101,
                confirmation_key="telegram-update-5001",
                actual_entry_time=SIGNAL_TIME,
                actual_entry=6110.0,
                values={"actual_position_shares": 10.0, "model_entry": 6100.0},
            )
            assert snapshot.gate_results == '{"data_integrity":"PASS"}'
            assert actual.actual_trade_id != model.model_trade_id

        await engine.dispose()

        restarted_engine, restarted_factory = create_engine_and_session(
            f"sqlite+aiosqlite:///{database_path.as_posix()}"
        )
        try:
            async with restarted_factory() as session:
                persisted_idea = await session.get(IdeaJournal, idea.trade_id)
                persisted_model = await session.get(ModelTradeJournal, model.model_trade_id)
                persisted_actual = await session.get(ActualTradeJournal, actual.actual_trade_id)
                events = await list_trade_events(session, idea.trade_id)
            assert persisted_idea is not None
            assert persisted_model is not None
            assert persisted_actual is not None
            assert persisted_model.model_position_rub == 100_000.0
            assert persisted_actual.actual_position_shares == 10.0
            assert [event.event_type for event in events] == [TradeEventType.ENTRY.value]
        finally:
            await restarted_engine.dispose()
    finally:
        await engine.dispose()


async def test_actual_trade_requires_explicit_user_confirmation(tmp_path: Path) -> None:
    engine, factory = await _database(tmp_path)
    try:
        async with factory() as session, session.begin():
            idea, _ = await create_idea_journal(
                session,
                signal_datetime=SIGNAL_TIME,
                ticker="ROSN",
                direction="LONG",
            )
            with pytest.raises(ActualTradeConfirmationRequired):
                await create_actual_trade(
                    session,
                    trade_id=idea.trade_id,
                    confirmed_by_user=False,
                    telegram_id=101,
                    confirmation_key="must-not-be-created",
                    actual_entry_time=SIGNAL_TIME,
                    actual_entry=520.0,
                )

            actual_count = await session.scalar(
                select(func.count()).select_from(ActualTradeJournal)
            )
            event_count = await session.scalar(select(func.count()).select_from(TradeEventJournal))
            assert actual_count == 0
            assert event_count == 0
    finally:
        await engine.dispose()


async def test_event_references_cannot_cross_trade_boundaries(tmp_path: Path) -> None:
    engine, factory = await _database(tmp_path)
    try:
        async with factory() as session, session.begin():
            first, _ = await create_idea_journal(
                session,
                signal_datetime=SIGNAL_TIME,
                ticker="SBER",
                direction="LONG",
            )
            second, _ = await create_idea_journal(
                session,
                signal_datetime=SIGNAL_TIME,
                ticker="GAZP",
                direction="LONG",
            )
            model = await create_model_trade(session, trade_id=first.trade_id)
            with pytest.raises(ValueError, match="does not belong"):
                await append_trade_event(
                    session,
                    trade_id=second.trade_id,
                    model_trade_id=model.model_trade_id,
                    event_datetime=SIGNAL_TIME,
                    event_type=TradeEventType.OTHER,
                )
    finally:
        await engine.dispose()


def test_migration_enforces_immutable_snapshot_and_append_only_events(tmp_path: Path) -> None:
    database_path = tmp_path / "immutable.db"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    command.upgrade(migration_config(database_url), "head")

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO idea_journals (
                trade_id, strategy_version, signal_datetime, ticker, direction
            ) VALUES (
                '20260901-SBER-LONG-01', 'intraday_v2_4',
                '2026-09-01 08:30:00', 'SBER', 'LONG'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO decision_snapshots_v24 (
                trade_id, strategy_version, signal_datetime, ticker, direction
            ) VALUES (
                '20260901-SBER-LONG-01', 'intraday_v2_4',
                '2026-09-01 08:30:00', 'SBER', 'LONG'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO trade_event_journal (
                trade_id, event_datetime, event_type
            ) VALUES (
                '20260901-SBER-LONG-01', '2026-09-01 08:31:00', 'OTHER'
            )
            """
        )
        connection.commit()

        for statement in (
            "UPDATE idea_journals SET ticker = 'GAZP'",
            "UPDATE decision_snapshots_v24 SET ticker = 'GAZP'",
            "UPDATE trade_event_journal SET event_type = 'CANCEL'",
            "DELETE FROM trade_event_journal",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement)
