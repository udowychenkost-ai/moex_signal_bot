"""Add strategy modes and v2.4 orchestration traceability.

Revision ID: 20260902_0021
Revises: 20260901_0020
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260902_0021"
down_revision = "20260901_0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "telegram_users",
        sa.Column(
            "analysis_mode",
            sa.String(length=24),
            nullable=False,
            server_default="LEGACY_ONLY",
        ),
    )
    op.create_index("ix_telegram_users_analysis_mode", "telegram_users", ["analysis_mode"])
    op.execute("UPDATE telegram_users SET analysis_mode = 'LEGACY_ONLY'")

    op.add_column(
        "trading_ideas",
        sa.Column(
            "strategy_family",
            sa.String(length=24),
            nullable=False,
            server_default="LEGACY",
        ),
    )
    op.create_index("ix_trading_ideas_strategy_family", "trading_ideas", ["strategy_family"])
    op.add_column(
        "candles",
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "market_candles",
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.add_column(
        "idea_journals",
        sa.Column("candidate_key", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "idea_journals",
        sa.Column(
            "strategy_family",
            sa.String(length=24),
            nullable=False,
            server_default="INTRADAY_V24",
        ),
    )
    op.create_index(
        "uq_idea_journal_candidate_key",
        "idea_journals",
        ["candidate_key"],
        unique=True,
    )

    op.add_column(
        "decision_snapshots_v24",
        sa.Column(
            "strategy_family",
            sa.String(length=24),
            nullable=False,
            server_default="INTRADAY_V24",
        ),
    )
    for name in (
        "risk_policy_version",
        "data_sla_policy_version",
        "cost_model_version",
        "calibration_model_version",
    ):
        op.add_column(
            "decision_snapshots_v24",
            sa.Column(name, sa.String(length=64), nullable=True),
        )

    op.create_table(
        "v24_candidate_claims",
        sa.Column("candidate_key", sa.String(length=64), nullable=False),
        sa.Column("strategy_version", sa.String(length=64), nullable=False),
        sa.Column("ticker", sa.String(length=36), nullable=False),
        sa.Column("direction", sa.String(length=8), nullable=False),
        sa.Column("source_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trade_id", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["trade_id"], ["idea_journals.trade_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("candidate_key"),
        sa.UniqueConstraint("trade_id", name="uq_v24_candidate_claim_trade"),
    )
    op.create_index(
        "ix_v24_candidate_claims_strategy_version",
        "v24_candidate_claims",
        ["strategy_version"],
    )
    op.create_index("ix_v24_candidate_claims_ticker", "v24_candidate_claims", ["ticker"])


def downgrade() -> None:
    op.drop_table("v24_candidate_claims")
    for name in (
        "calibration_model_version",
        "cost_model_version",
        "data_sla_policy_version",
        "risk_policy_version",
        "strategy_family",
    ):
        op.drop_column("decision_snapshots_v24", name)
    op.drop_index("uq_idea_journal_candidate_key", table_name="idea_journals")
    op.drop_column("idea_journals", "strategy_family")
    op.drop_column("idea_journals", "candidate_key")
    op.drop_column("market_candles", "fetched_at")
    op.drop_column("candles", "fetched_at")
    op.drop_index("ix_trading_ideas_strategy_family", table_name="trading_ideas")
    op.drop_column("trading_ideas", "strategy_family")
    op.drop_index("ix_telegram_users_analysis_mode", table_name="telegram_users")
    op.drop_column("telegram_users", "analysis_mode")
