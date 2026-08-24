from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from app.db import create_engine_and_session

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASELINE_REVISION = "20260819_0001"
HEAD_REVISION = "20260824_0012"
BASELINE_TABLES = {
    "instruments",
    "candles",
    "order_book_levels",
    "telegram_users",
    "watchlist_items",
    "signals",
}


@dataclass(frozen=True, slots=True)
class SchemaSnapshot:
    tables: frozenset[str]
    instrument_columns: frozenset[str]
    idea_columns: frozenset[str]
    snapshot_columns: frozenset[str]
    paper_columns: frozenset[str]
    user_columns: frozenset[str]
    candidate_columns: frozenset[str]
    ai_request_columns: frozenset[str]


def _alembic_config(database_url: str) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.attributes["database_url"] = database_url
    return config


async def _schema_snapshot(database_url: str) -> SchemaSnapshot:
    engine, _ = create_engine_and_session(database_url)
    try:
        async with engine.connect() as connection:

            def inspect_schema(sync_connection: object) -> SchemaSnapshot:
                inspector = inspect(sync_connection)
                tables = frozenset(inspector.get_table_names())
                idea_columns = (
                    frozenset(column["name"] for column in inspector.get_columns("trading_ideas"))
                    if "trading_ideas" in tables
                    else frozenset()
                )
                instrument_columns = (
                    frozenset(column["name"] for column in inspector.get_columns("instruments"))
                    if "instruments" in tables
                    else frozenset()
                )
                snapshot_columns = (
                    frozenset(
                        column["name"] for column in inspector.get_columns("trading_idea_snapshots")
                    )
                    if "trading_idea_snapshots" in tables
                    else frozenset()
                )
                paper_columns = (
                    frozenset(column["name"] for column in inspector.get_columns("paper_trades"))
                    if "paper_trades" in tables
                    else frozenset()
                )
                user_columns = (
                    frozenset(column["name"] for column in inspector.get_columns("telegram_users"))
                    if "telegram_users" in tables
                    else frozenset()
                )
                candidate_columns = (
                    frozenset(
                        column["name"] for column in inspector.get_columns("candidate_experiments")
                    )
                    if "candidate_experiments" in tables
                    else frozenset()
                )
                ai_request_columns = (
                    frozenset(column["name"] for column in inspector.get_columns("ai_request_logs"))
                    if "ai_request_logs" in tables
                    else frozenset()
                )
                return SchemaSnapshot(
                    tables=tables,
                    instrument_columns=instrument_columns,
                    idea_columns=idea_columns,
                    snapshot_columns=snapshot_columns,
                    paper_columns=paper_columns,
                    user_columns=user_columns,
                    candidate_columns=candidate_columns,
                    ai_request_columns=ai_request_columns,
                )

            return await connection.run_sync(inspect_schema)
    finally:
        await engine.dispose()


def _legacy_revision(snapshot: SchemaSnapshot) -> str | None:
    if not snapshot.tables:
        return None
    missing_baseline = BASELINE_TABLES - snapshot.tables
    if missing_baseline:
        missing = ", ".join(sorted(missing_baseline))
        raise RuntimeError(
            "Database has an unversioned, incomplete application schema; "
            f"missing baseline tables: {missing}"
        )
    if "trading_ideas" not in snapshot.tables:
        return BASELINE_REVISION

    columns = snapshot.idea_columns
    factor_columns = {"technical_score", "fundamental_score", "news_score", "total_score"}
    execution_columns = {"entry_fill_price", "exit_fill_price", "slippage"}
    context_idea_columns = {
        "market_regime",
        "market_volatility",
        "market_regime_score",
        "relative_strength_score",
        "relative_strength_label",
        "volume_score",
        "volume_state",
        "momentum_extreme_score",
        "fundamental_label",
    }
    context_snapshot_columns = {
        "market_volatility",
        "market_regime_score",
        "relative_strength_score",
        "volume_score",
        "momentum_extreme_score",
        "fundamental_components",
        "fundamental_publications",
    }
    v2_user_columns = {
        "ai_filter_enabled",
        "notify_new_idea",
        "notify_activation",
        "notify_tp",
        "notify_sl",
        "notify_expiry",
        "notify_daily_summary",
    }
    if (
        {"candidate_experiments", "ai_request_logs"}.issubset(snapshot.tables)
        and {"strategy_version", "quality_gate_result", "ai_verdict"}.issubset(columns)
        and v2_user_columns.issubset(snapshot.user_columns)
    ):
        if {"ai_fallback_used", "ai_usage_json"}.issubset(snapshot.candidate_columns) and {
            "fallback_used",
            "usage_json",
        }.issubset(snapshot.ai_request_columns):
            if "idea_follows" in snapshot.tables and "notify_watchlist" in snapshot.user_columns:
                return HEAD_REVISION
            return "20260824_0011"
        return "20260824_0010"
    if (
        {"market_candles", "fundamental_reports"}.issubset(snapshot.tables)
        and "sector" in snapshot.instrument_columns
        and context_idea_columns.issubset(columns)
        and context_snapshot_columns.issubset(snapshot.snapshot_columns)
    ):
        return "20260820_0009"
    if {"trading_idea_snapshots", "forward_notifications", "job_run_states"}.issubset(
        snapshot.tables
    ) and "observation_mode" in columns:
        return "20260820_0008"
    if (
        "paper_trades" in snapshot.tables
        and factor_columns.issubset(columns)
        and execution_columns.issubset(snapshot.paper_columns)
    ):
        return "20260819_0007"
    if "paper_trades" in snapshot.tables and factor_columns.issubset(columns):
        return "20260819_0006"
    if "paper_trades" in snapshot.tables:
        return "20260819_0005"
    if "activation_price" in columns:
        return "20260819_0004"
    if "last_evaluated_at" in columns:
        return "20260819_0003"
    return "20260819_0002"


def _upgrade(database_url: str, legacy_revision: str | None) -> None:
    config = _alembic_config(database_url)
    if legacy_revision is not None:
        logger.info("Adopting legacy database schema at revision %s", legacy_revision)
        command.stamp(config, legacy_revision)
    command.upgrade(config, "head")


async def migrate_database(database_url: str) -> None:
    """Upgrade a fresh, versioned, or recognized legacy database to Alembic head."""
    snapshot = await _schema_snapshot(database_url)
    legacy_revision = (
        _legacy_revision(snapshot) if "alembic_version" not in snapshot.tables else None
    )
    await asyncio.to_thread(_upgrade, database_url, legacy_revision)
