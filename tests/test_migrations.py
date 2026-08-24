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
        candidate_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(candidate_experiments)")
        }
        ai_request_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(ai_request_logs)")
        }
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
    assert {"candidate_experiments", "ai_request_logs"}.issubset(tables)
    assert {"ai_fallback_used", "ai_usage_json"}.issubset(candidate_columns)
    assert {"fallback_used", "usage_json"}.issubset(ai_request_columns)
    assert {"ai_filter_enabled", "notify_sl", "notify_daily_summary"}.issubset(user_columns)
    assert {"strategy_version", "quality_gate_result", "ai_verdict"}.issubset(idea_columns)
    assert revision == ("20260824_0011",)


def test_v2_upgrade_preserves_existing_trading_idea_as_v1(tmp_path: Path) -> None:
    database_path = tmp_path / "existing-v1.db"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    config = migration_config(database_url)
    command.upgrade(config, "20260820_0009")
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO instruments (
                secid, board_id, instrument_type, short_name, lot_size,
                echelon, is_active, updated_at, sector
            ) VALUES ('SBER', 'TQBR', 'stock', 'Сбербанк', 10, 1, 1,
                      '2026-08-20 10:00:00', 'Financials')
            """
        )
        connection.execute(
            """
            INSERT INTO trading_ideas (
                ticker, instrument_name, direction, horizon, primary_timeframe,
                entry_price_from, entry_price_to, current_price, take_profit,
                stop_loss, confidence, expected_return_pct, risk_pct,
                risk_reward_ratio, rationale, invalidation_reason, status,
                source_timeframes, source_candle_begin, material_hash, version,
                created_at, updated_at, expires_at, last_evaluated_at,
                technical_score, fundamental_score, news_score, total_score
            ) VALUES (
                'SBER', 'Сбербанк', 'BUY', 'POSITION_1M', '1d',
                250, 255, 257, 280, 240, 75, 9, 6, 2,
                'baseline rationale', 'close below stop', 'PENDING_ENTRY',
                '1d,1w', '2026-08-20 00:00:00', 'legacy-hash', 1,
                '2026-08-20 10:00:00', '2026-08-20 10:00:00',
                '2026-09-20 10:00:00', '2026-08-20 00:00:00',
                50, 0, 0, 50
            )
            """
        )
        connection.commit()

    command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            """
            SELECT ticker, rationale, strategy_version, quality_gate_result,
                   ai_verdict
            FROM trading_ideas
            """
        ).fetchone()
    assert row == (
        "SBER",
        "baseline rationale",
        "v1",
        "LEGACY",
        "NOT_REQUESTED",
    )


async def test_gemini_telemetry_upgrade_preserves_existing_v2_log(tmp_path: Path) -> None:
    database_path = tmp_path / "existing-v2.db"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    config = migration_config(database_url)
    await asyncio.to_thread(command.upgrade, config, "20260824_0010")
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO ai_request_logs (
                request_kind, candidate_id, provider, model, status,
                input_tokens, output_tokens, estimated_cost_usd, latency_ms,
                error, created_at
            ) VALUES (
                'CANDIDATE', NULL, 'openai', 'gpt-5-mini', 'OK',
                10, 5, 0.001, 25, '', '2026-08-24 10:00:00'
            )
            """
        )
        connection.execute("DROP TABLE alembic_version")
        connection.commit()

    await migrate_database(database_url)

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            """
            SELECT provider, model, input_tokens, fallback_used, usage_json
            FROM ai_request_logs
            """
        ).fetchone()
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert row == ("openai", "gpt-5-mini", 10, 0, "{}")
    assert revision == ("20260824_0011",)


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
    assert revision == ("20260824_0011",)


async def test_auto_migration_creates_fresh_database(tmp_path: Path) -> None:
    database_path = tmp_path / "fresh.db"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"

    await migrate_database(database_url)

    with sqlite3.connect(database_path) as connection:
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert revision == ("20260824_0011",)


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
    assert revision == ("20260824_0011",)


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
    assert revision == ("20260824_0011",)


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
    assert revision == ("20260824_0011",)
