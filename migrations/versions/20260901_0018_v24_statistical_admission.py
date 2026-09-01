"""Add append-only v2.4 statistical admission policy.

Revision ID: 20260901_0018
Revises: 20260901_0017
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260901_0018"
down_revision = "20260901_0017"
branch_labels = None
depends_on = None


def _create_immutability_guard() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            """
            CREATE TRIGGER trg_statistical_admission_settings_immutable
            BEFORE UPDATE OR DELETE ON statistical_admission_settings
            FOR EACH ROW EXECUTE FUNCTION v24_reject_mutation()
            """
        )
    elif dialect == "sqlite":
        op.execute(
            """
            CREATE TRIGGER trg_statistical_admission_settings_no_update
            BEFORE UPDATE ON statistical_admission_settings
            BEGIN
                SELECT RAISE(ABORT, 'statistical_admission_settings is immutable');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_statistical_admission_settings_no_delete
            BEFORE DELETE ON statistical_admission_settings
            BEGIN
                SELECT RAISE(ABORT, 'statistical_admission_settings is append-only');
            END
            """
        )


def _drop_immutability_guard() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_statistical_admission_settings_immutable "
            "ON statistical_admission_settings"
        )
    elif dialect == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS trg_statistical_admission_settings_no_update")
        op.execute("DROP TRIGGER IF EXISTS trg_statistical_admission_settings_no_delete")


def upgrade() -> None:
    op.add_column(
        "trade_event_journal",
        sa.Column("costs_rub", sa.Float(), nullable=True),
    )
    for name, length in (
        ("calibration_setup", 40),
        ("calibration_direction", 8),
        ("calibration_market_regime", 32),
        ("calibration_trend", 32),
        ("calibration_volatility", 24),
        ("calibration_time_of_day", 24),
        ("calibration_rr_bucket", 24),
        ("calibration_liquidity_state", 24),
        ("calibration_context", 32),
    ):
        op.add_column(
            "model_trade_journals",
            sa.Column(name, sa.String(length=length), nullable=True),
        )
    op.create_index(
        "ix_model_trade_calibration_cohort",
        "model_trade_journals",
        ["strategy_version", "calibration_group", "sample_type"],
    )
    op.create_table(
        "statistical_admission_settings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("strategy_version", sa.String(length=64), nullable=False),
        sa.Column("configuration_version", sa.String(length=64), nullable=False),
        sa.Column("min_oos_trades", sa.Integer(), nullable=False),
        sa.Column("min_forward_trades", sa.Integer(), nullable=False),
        sa.Column("max_confidence_interval_width", sa.Float(), nullable=False),
        sa.Column("min_total_comparable_trades", sa.Integer(), nullable=True),
        sa.Column("degradation_min_recent_trades", sa.Integer(), nullable=True),
        sa.Column("degradation_min_history_trades", sa.Integer(), nullable=True),
        sa.Column("degradation_expectancy_drop_r", sa.Float(), nullable=True),
        sa.Column("degradation_win_rate_drop", sa.Float(), nullable=True),
        sa.Column("configured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by_telegram_id", sa.BigInteger(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.CheckConstraint("min_oos_trades > 0", name="ck_stat_admission_min_oos"),
        sa.CheckConstraint("min_forward_trades > 0", name="ck_stat_admission_min_forward"),
        sa.CheckConstraint(
            "max_confidence_interval_width > 0 "
            "AND max_confidence_interval_width <= 1",
            name="ck_stat_admission_ci_width",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "strategy_version",
            "configuration_version",
            name="uq_statistical_admission_strategy_version",
        ),
    )
    op.create_index(
        "ix_statistical_admission_effective",
        "statistical_admission_settings",
        ["strategy_version", "effective_from"],
    )
    _create_immutability_guard()


def downgrade() -> None:
    _drop_immutability_guard()
    op.drop_table("statistical_admission_settings")
    op.drop_column("trade_event_journal", "costs_rub")
    op.drop_index("ix_model_trade_calibration_cohort", table_name="model_trade_journals")
    for name in (
        "calibration_context",
        "calibration_liquidity_state",
        "calibration_rr_bucket",
        "calibration_time_of_day",
        "calibration_volatility",
        "calibration_trend",
        "calibration_market_regime",
        "calibration_direction",
        "calibration_setup",
    ):
        op.drop_column("model_trade_journals", name)
