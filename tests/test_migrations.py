from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
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
        orderbook_columns = {
            row[1]: row for row in connection.execute("PRAGMA table_info(order_book_levels)")
        }
        idea_journal_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(idea_journals)")
        }
        model_journal_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(model_trade_journals)")
        }
        actual_journal_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(actual_trade_journals)")
        }
        event_journal_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(trade_event_journal)")
        }
        risk_budget_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(risk_budget_settings)")
        }
        admission_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(statistical_admission_settings)")
        }
        kill_switch_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(kill_switch_events)")
        }
        daily_v24_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(daily_journal_summaries_v24)")
        }
        outbox_v24_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(v24_notification_outbox)")
        }
        decision_v24_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(decision_snapshots_v24)")
        }
        candle_columns = {row[1] for row in connection.execute("PRAGMA table_info(candles)")}
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
        "trade_id_sequences",
        "idea_journals",
        "decision_snapshots_v24",
        "model_trade_journals",
        "actual_trade_journals",
        "trade_event_journal",
        "context_records_v24",
        "risk_budget_settings",
        "statistical_admission_settings",
        "kill_switch_events",
        "journal_health_probes",
        "daily_journal_summaries_v24",
        "v24_notification_outbox",
        "v24_candidate_claims",
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
    assert "idea_follows" in tables
    assert {"ai_fallback_used", "ai_usage_json"}.issubset(candidate_columns)
    assert {"fallback_used", "usage_json"}.issubset(ai_request_columns)
    assert {"ai_filter_enabled", "notify_sl", "notify_daily_summary"}.issubset(user_columns)
    assert "notify_watchlist" in user_columns
    assert "analysis_mode" in user_columns
    assert "strategy_family" in idea_columns
    assert "fetched_at" in candle_columns
    assert {"strategy_version", "quality_gate_result", "ai_verdict"}.issubset(idea_columns)
    assert orderbook_columns["quantity"][3] == 0
    assert {
        "trade_id",
        "strategy_version",
        "signal_datetime",
        "risk_budget_status",
        "final_decision",
    }.issubset(idea_journal_columns)
    assert {"candidate_key", "strategy_family"}.issubset(idea_journal_columns)
    assert {
        "strategy_family",
        "risk_policy_version",
        "data_sla_policy_version",
        "cost_model_version",
        "calibration_model_version",
    }.issubset(decision_v24_columns)
    assert {"model_trade_id", "sample_type", "model_fill_status", "result_r"}.issubset(
        model_journal_columns
    )
    assert {
        "calibration_setup",
        "calibration_market_regime",
        "calibration_volatility",
        "calibration_time_of_day",
        "calibration_liquidity_state",
    }.issubset(model_journal_columns)
    assert {
        "actual_trade_id",
        "confirmation_key",
        "confirmed_by_telegram_id",
        "result_r",
    }.issubset(actual_journal_columns)
    assert {
        "event_id",
        "event_type",
        "stop_before",
        "stop_after",
        "confirmed_by_telegram_id",
        "confirmation_key",
        "costs_rub",
    }.issubset(event_journal_columns)
    assert {
        "configuration_version",
        "working_capital_rub",
        "max_risk_per_trade_pct",
        "max_daily_loss_pct",
        "max_portfolio_heat_pct",
        "max_sector_heat_pct",
        "max_correlated_factor_heat_pct",
    }.issubset(risk_budget_columns)
    assert {
        "configuration_version",
        "min_oos_trades",
        "min_forward_trades",
        "max_confidence_interval_width",
        "min_total_comparable_trades",
        "degradation_expectancy_drop_r",
    }.issubset(admission_columns)
    assert {
        "state",
        "reasons",
        "source",
        "triggered_at",
        "created_by_telegram_id",
    }.issubset(kill_switch_columns)
    assert {
        "summary_date",
        "model_net_pl_rub",
        "actual_net_pl_rub",
        "data_issues",
        "lessons",
    }.issubset(daily_v24_columns)
    assert {
        "notification_key",
        "notification_type",
        "payload",
        "status",
        "attempt_count",
    }.issubset(outbox_v24_columns)
    assert revision == ("20260902_0021",)


def test_orderbook_nullable_depth_upgrade_preserves_existing_rows(tmp_path: Path) -> None:
    database_path = tmp_path / "existing-orderbook.db"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    config = migration_config(database_url)
    command.upgrade(config, "20260824_0012")
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO instruments (
                secid, board_id, instrument_type, short_name, lot_size,
                echelon, is_active, updated_at
            ) VALUES ('SBER', 'TQBR', 'stock', 'Сбербанк', 10, 1, 1,
                      '2026-08-27 10:00:00')
            """
        )
        connection.execute(
            """
            INSERT INTO order_book_levels (
                secid, board_id, snapshot_at, side, level, price, quantity
            ) VALUES ('SBER', 'TQBR', '2026-08-27 10:00:00', 'B', 1, 268.20, 125)
            """
        )
        connection.commit()

    command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT secid, side, price, quantity FROM order_book_levels"
        ).fetchone()
        columns = {
            item[1]: item for item in connection.execute("PRAGMA table_info(order_book_levels)")
        }
    assert row == ("SBER", "B", 268.2, 125.0)
    assert columns["quantity"][3] == 0


def test_v24_reporting_upgrade_preserves_previous_head_and_summary_is_immutable(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "v24-reporting-upgrade.db"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    config = migration_config(database_url)
    command.upgrade(config, "20260901_0019")
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO idea_journals (
                trade_id, strategy_version, signal_datetime, ticker, direction, created_at
            ) VALUES (
                '20260901-SBER-LONG-01', 'intraday_v2_4',
                '2026-09-01 09:00:00', 'SBER', 'LONG', '2026-09-01 09:00:00'
            )
            """
        )
        connection.commit()
    command.upgrade(config, "head")
    with sqlite3.connect(database_path) as connection:
        preserved = connection.execute(
            "SELECT ticker, strategy_version, strategy_family FROM idea_journals"
        ).fetchone()
        connection.execute(
            """
            INSERT INTO daily_journal_summaries_v24 (
                summary_date, strategy_version, ideas_issued,
                model_trades_opened, model_trades_closed, actual_trades_confirmed
            ) VALUES ('2026-09-01', 'intraday_v2_4', 1, 0, 0, 0)
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE daily_journal_summaries_v24 SET ideas_issued = 2")
    assert preserved == ("SBER", "intraday_v2_4", "INTRADAY_V24")


def test_actual_event_confirmation_upgrade_preserves_events_and_append_only_guard(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "existing-v24-events.db"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    config = migration_config(database_url)
    command.upgrade(config, "20260901_0016")
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
            INSERT INTO trade_event_journal (
                trade_id, event_datetime, event_type, reason
            ) VALUES (
                '20260901-SBER-LONG-01', '2026-09-01 08:31:00',
                'OTHER', 'legacy event'
            )
            """
        )
        connection.commit()

    command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        event = connection.execute(
            """
            SELECT event_type, reason, confirmed_by_telegram_id, confirmation_key
            FROM trade_event_journal
            """
        ).fetchone()
        assert event == ("OTHER", "legacy event", None, None)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE trade_event_journal SET event_type = 'CANCEL'")


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
                   ai_verdict, strategy_family
            FROM trading_ideas
            """
        ).fetchone()
    assert row == (
        "SBER",
        "baseline rationale",
        "v1",
        "LEGACY",
        "NOT_REQUESTED",
        "LEGACY",
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
    assert revision == ("20260902_0021",)


async def test_context_ux_upgrade_preserves_previous_head_users(tmp_path: Path) -> None:
    database_path = tmp_path / "existing-v2-previous-head.db"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    config = migration_config(database_url)
    await asyncio.to_thread(command.upgrade, config, "20260824_0011")
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO telegram_users (
                telegram_id, username, risk_profile, risk_per_trade_pct,
                default_timeframe, echelon_filter, is_active, created_at
            ) VALUES (
                101, 'existing', 'balanced', 1.0, '15m', 'all', 1,
                '2026-08-24 10:00:00'
            )
            """
        )
        connection.execute("DROP TABLE alembic_version")
        connection.commit()

    await migrate_database(database_url)

    with sqlite3.connect(database_path) as connection:
        user = connection.execute(
            "SELECT telegram_id, username, notify_watchlist, analysis_mode FROM telegram_users"
        ).fetchone()
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert user == (101, "existing", 1, "LEGACY_ONLY")
    assert "idea_follows" in tables
    assert revision == ("20260902_0021",)


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
    assert revision == ("20260902_0021",)


async def test_auto_migration_creates_fresh_database(tmp_path: Path) -> None:
    database_path = tmp_path / "fresh.db"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"

    await migrate_database(database_url)

    with sqlite3.connect(database_path) as connection:
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert revision == ("20260902_0021",)


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
    assert revision == ("20260902_0021",)


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
    assert revision == ("20260902_0021",)


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
    assert revision == ("20260902_0021",)
