from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config

from app.migrations import migrate_database


def migration_config(database_url: str) -> Config:
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    return config


def test_alembic_upgrade_creates_trading_idea_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "migration.db"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    config = migration_config(database_url)

    command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        user_columns = {row[1] for row in connection.execute("PRAGMA table_info(telegram_users)")}
        idea_columns = {row[1] for row in connection.execute("PRAGMA table_info(trading_ideas)")}
        paper_columns = {row[1] for row in connection.execute("PRAGMA table_info(paper_trades)")}
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()

    assert {
        "instruments",
        "candles",
        "signals",
        "trading_ideas",
        "trading_idea_events",
        "idea_notifications",
        "paper_trades",
        "trading_idea_snapshots",
        "forward_notifications",
        "job_run_states",
        "market_candles",
        "fundamental_reports",
    }.issubset(tables)
    assert {"report_frequency", "idea_horizon", "minimum_confidence"}.issubset(user_columns)
    assert {
        "entry_price_from",
        "entry_price_to",
        "take_profit",
        "stop_loss",
        "source_signal_id",
        "version",
        "last_evaluated_at",
        "activation_price",
        "technical_score",
        "fundamental_score",
        "news_score",
        "total_score",
        "observation_mode",
        "market_regime",
        "relative_strength_score",
        "volume_score",
        "momentum_extreme_score",
    }.issubset(idea_columns)
    assert {"entry_fill_price", "exit_fill_price", "slippage"}.issubset(paper_columns)
    assert revision == ("20260820_0009",)


async def test_auto_migration_adopts_unversioned_legacy_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy.db"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    await asyncio.to_thread(command.upgrade, migration_config(database_url), "20260819_0001")
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TABLE alembic_version")

    await migrate_database(database_url)

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert "trading_ideas" in tables
    assert "paper_trades" in tables
    assert revision == ("20260820_0009",)


async def test_auto_migration_creates_fresh_database(tmp_path: Path) -> None:
    database_path = tmp_path / "fresh.db"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"

    await migrate_database(database_url)

    with sqlite3.connect(database_path) as connection:
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert revision == ("20260820_0009",)


async def test_auto_migration_adopts_unversioned_previous_head(tmp_path: Path) -> None:
    database_path = tmp_path / "previous-head.db"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    await asyncio.to_thread(command.upgrade, migration_config(database_url), "20260819_0006")
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TABLE alembic_version")

    await migrate_database(database_url)

    with sqlite3.connect(database_path) as connection:
        paper_columns = {row[1] for row in connection.execute("PRAGMA table_info(paper_trades)")}
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert {"entry_fill_price", "exit_fill_price", "slippage"}.issubset(paper_columns)
    assert revision == ("20260820_0009",)


async def test_auto_migration_upgrades_unversioned_0007_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "unversioned-0007.db"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    await asyncio.to_thread(command.upgrade, migration_config(database_url), "20260819_0007")
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TABLE alembic_version")

    await migrate_database(database_url)

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert {"trading_idea_snapshots", "forward_notifications", "job_run_states"}.issubset(tables)
    assert revision == ("20260820_0009",)


async def test_auto_migration_upgrades_unversioned_0008_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "unversioned-0008.db"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    await asyncio.to_thread(command.upgrade, migration_config(database_url), "20260820_0008")
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TABLE alembic_version")

    await migrate_database(database_url)

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert {"market_candles", "fundamental_reports"}.issubset(tables)
    assert revision == ("20260820_0009",)
