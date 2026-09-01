"""Add append-only v2.4 risk budget policies.

Revision ID: 20260901_0016
Revises: 20260901_0015
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260901_0016"
down_revision = "20260901_0015"
branch_labels = None
depends_on = None


def _create_immutability_guard() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            """
            CREATE TRIGGER trg_risk_budget_settings_immutable
            BEFORE UPDATE OR DELETE ON risk_budget_settings
            FOR EACH ROW EXECUTE FUNCTION v24_reject_mutation()
            """
        )
    elif dialect == "sqlite":
        op.execute(
            """
            CREATE TRIGGER trg_risk_budget_settings_no_update
            BEFORE UPDATE ON risk_budget_settings
            BEGIN
                SELECT RAISE(ABORT, 'risk_budget_settings is immutable');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_risk_budget_settings_no_delete
            BEFORE DELETE ON risk_budget_settings
            BEGIN
                SELECT RAISE(ABORT, 'risk_budget_settings is append-only');
            END
            """
        )


def _drop_immutability_guard() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_risk_budget_settings_immutable ON risk_budget_settings"
        )
    elif dialect == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS trg_risk_budget_settings_no_update")
        op.execute("DROP TRIGGER IF EXISTS trg_risk_budget_settings_no_delete")


def upgrade() -> None:
    op.create_table(
        "risk_budget_settings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("scope", sa.String(length=64), nullable=False, server_default="GLOBAL"),
        sa.Column("configuration_version", sa.String(length=64), nullable=False),
        sa.Column("working_capital_rub", sa.Float(), nullable=True),
        sa.Column("max_risk_per_trade_pct", sa.Float(), nullable=True),
        sa.Column("max_daily_loss_pct", sa.Float(), nullable=True),
        sa.Column("max_portfolio_heat_pct", sa.Float(), nullable=True),
        sa.Column("max_sector_heat_pct", sa.Float(), nullable=True),
        sa.Column("max_correlated_factor_heat_pct", sa.Float(), nullable=True),
        sa.Column("available_capital_pct", sa.Float(), nullable=True),
        sa.Column("configured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by_telegram_id", sa.BigInteger(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scope", "configuration_version", name="uq_risk_budget_scope_version"),
    )
    op.create_index("ix_risk_budget_effective", "risk_budget_settings", ["scope", "effective_from"])
    _create_immutability_guard()


def downgrade() -> None:
    _drop_immutability_guard()
    op.drop_table("risk_budget_settings")
