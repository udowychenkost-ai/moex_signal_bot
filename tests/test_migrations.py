from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config


def test_alembic_upgrade_creates_trading_idea_schema(tmp_path: Path, monkeypatch) -> None:
    database_path = tmp_path / "migration.db"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    config = Config("alembic.ini")

    command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        user_columns = {row[1] for row in connection.execute("PRAGMA table_info(telegram_users)")}
        idea_columns = {row[1] for row in connection.execute("PRAGMA table_info(trading_ideas)")}
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()

    assert {
        "instruments",
        "candles",
        "signals",
        "trading_ideas",
        "trading_idea_events",
        "idea_notifications",
    }.issubset(tables)
    assert {"report_frequency", "idea_horizon", "minimum_confidence"}.issubset(user_columns)
    assert {
        "entry_price_from",
        "entry_price_to",
        "take_profit",
        "stop_loss",
        "source_signal_id",
        "version",
    }.issubset(idea_columns)
    assert revision == ("20260819_0002",)
