"""Add V2 quality gate, AI experiment tracking, and Telegram preferences.

Revision ID: 20260824_0010
Revises: 20260820_0009
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260824_0010"
down_revision = "20260820_0009"
branch_labels = None
depends_on = None


def _preference_columns() -> tuple[sa.Column, ...]:
    return (
        sa.Column("ai_filter_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("notify_new_idea", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("notify_activation", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("notify_tp", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("notify_sl", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("notify_expiry", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("notify_daily_summary", sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def _v2_columns(*, snapshot: bool = False) -> tuple[sa.Column, ...]:
    columns: tuple[sa.Column, ...] = (
        sa.Column(
            "quality_gate_result", sa.String(length=16), nullable=False, server_default="LEGACY"
        ),
        sa.Column("final_quality_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("supporting_factors", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("contradicting_factors", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("confirmation_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("strategy_version", sa.String(length=64), nullable=False, server_default="v1"),
        sa.Column(
            "ai_verdict", sa.String(length=24), nullable=False, server_default="NOT_REQUESTED"
        ),
        sa.Column("ai_score", sa.Float(), nullable=True),
        sa.Column("ai_confidence", sa.String(length=16), nullable=True),
        sa.Column("ai_bull_case", sa.Text(), nullable=False, server_default=""),
        sa.Column("ai_bear_case", sa.Text(), nullable=False, server_default=""),
        sa.Column("ai_key_risks", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("ai_why_now", sa.Text(), nullable=False, server_default=""),
        sa.Column("ai_invalidation_conditions", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("ai_short_summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("ai_provider", sa.String(length=32), nullable=True),
        sa.Column("ai_model", sa.String(length=64), nullable=True),
        sa.Column("ai_reviewed_at", sa.DateTime(timezone=True), nullable=True),
    )
    return columns


def upgrade() -> None:
    with op.batch_alter_table("telegram_users") as batch:
        for column in _preference_columns():
            batch.add_column(column)

    with op.batch_alter_table("trading_ideas") as batch:
        for column in _v2_columns():
            batch.add_column(column)
        batch.create_index("ix_trading_ideas_quality_gate_result", ["quality_gate_result"])
        batch.create_index("ix_trading_ideas_strategy_version", ["strategy_version"])
        batch.create_index("ix_trading_ideas_ai_verdict", ["ai_verdict"])

    with op.batch_alter_table("trading_idea_snapshots") as batch:
        for column in _v2_columns(snapshot=True):
            batch.add_column(column)
        batch.create_index("ix_trading_idea_snapshots_strategy_version", ["strategy_version"])

    op.create_table(
        "candidate_experiments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("candidate_key", sa.String(length=64), nullable=False),
        sa.Column("strategy_version", sa.String(length=64), nullable=False),
        sa.Column("ticker", sa.String(length=36), nullable=False),
        sa.Column("direction", sa.String(length=8), nullable=False),
        sa.Column("horizon", sa.String(length=16), nullable=False),
        sa.Column("primary_timeframe", sa.String(length=8), nullable=False),
        sa.Column("quant_result", sa.String(length=16), nullable=False),
        sa.Column("quality_gate_result", sa.String(length=16), nullable=False),
        sa.Column("final_quality_score", sa.Float(), nullable=False),
        sa.Column("supporting_factors", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("contradicting_factors", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("quality_reasons", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("confirmation_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("timeframe_confirmations", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "ai_verdict", sa.String(length=24), nullable=False, server_default="NOT_REQUESTED"
        ),
        sa.Column("ai_score", sa.Float(), nullable=True),
        sa.Column("ai_confidence", sa.String(length=16), nullable=True),
        sa.Column("ai_bull_case", sa.Text(), nullable=False, server_default=""),
        sa.Column("ai_bear_case", sa.Text(), nullable=False, server_default=""),
        sa.Column("ai_key_risks", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("ai_why_now", sa.Text(), nullable=False, server_default=""),
        sa.Column("ai_invalidation_conditions", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("ai_short_summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("ai_provider", sa.String(length=32), nullable=True),
        sa.Column("ai_model", sa.String(length=64), nullable=True),
        sa.Column("ai_reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "ai_status", sa.String(length=16), nullable=False, server_default="NOT_REQUESTED"
        ),
        sa.Column("ai_input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ai_output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ai_estimated_cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("ai_latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ai_error", sa.Text(), nullable=False, server_default=""),
        sa.Column("published", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("publish_reason", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("published_idea_id", sa.Integer(), nullable=True),
        sa.Column("current_price", sa.Float(), nullable=False),
        sa.Column("entry_price_from", sa.Float(), nullable=False),
        sa.Column("entry_price_to", sa.Float(), nullable=False),
        sa.Column("take_profit", sa.Float(), nullable=False),
        sa.Column("stop_loss", sa.Float(), nullable=False),
        sa.Column("risk_reward_ratio", sa.Float(), nullable=False),
        sa.Column("technical_score", sa.Float(), nullable=False),
        sa.Column("fundamental_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("news_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("total_score", sa.Float(), nullable=False),
        sa.Column("signal_strength", sa.Float(), nullable=False),
        sa.Column("snapshot_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("source_candle_begin", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decision_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("activation_price", sa.Float(), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("close_price", sa.Float(), nullable=True),
        sa.Column("actual_r", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["ticker"], ["instruments.secid"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["published_idea_id"], ["trading_ideas.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("candidate_key", name="uq_candidate_experiment_key"),
    )
    op.create_index(
        "ix_candidate_experiments_strategy_version", "candidate_experiments", ["strategy_version"]
    )
    op.create_index("ix_candidate_experiments_ticker", "candidate_experiments", ["ticker"])
    op.create_index("ix_candidate_experiments_horizon", "candidate_experiments", ["horizon"])
    op.create_index(
        "ix_candidate_experiments_quality_gate_result",
        "candidate_experiments",
        ["quality_gate_result"],
    )
    op.create_index("ix_candidate_experiments_ai_verdict", "candidate_experiments", ["ai_verdict"])
    op.create_index("ix_candidate_experiments_published", "candidate_experiments", ["published"])
    op.create_index(
        "ix_candidate_experiments_published_idea_id", "candidate_experiments", ["published_idea_id"]
    )
    op.create_index("ix_candidate_experiments_status", "candidate_experiments", ["status"])
    op.create_index(
        "ix_candidate_experiments_decision_at", "candidate_experiments", ["decision_at"]
    )
    op.create_index("ix_candidate_experiments_expires_at", "candidate_experiments", ["expires_at"])
    op.create_index(
        "ix_candidate_experiment_cohort",
        "candidate_experiments",
        ["strategy_version", "horizon", "ai_verdict"],
    )
    op.create_index(
        "ix_candidate_experiment_open",
        "candidate_experiments",
        ["ticker", "primary_timeframe", "status"],
    )

    op.create_table(
        "ai_request_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("request_kind", sa.String(length=32), nullable=False, server_default="CANDIDATE"),
        sa.Column("candidate_id", sa.Integer(), nullable=True),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("estimated_cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["candidate_experiments.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ai_request_logs_candidate_id", "ai_request_logs", ["candidate_id"])
    op.create_index("ix_ai_request_created", "ai_request_logs", ["request_kind", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_ai_request_created", table_name="ai_request_logs")
    op.drop_index("ix_ai_request_logs_candidate_id", table_name="ai_request_logs")
    op.drop_table("ai_request_logs")
    for name in (
        "ix_candidate_experiment_open",
        "ix_candidate_experiment_cohort",
        "ix_candidate_experiments_expires_at",
        "ix_candidate_experiments_decision_at",
        "ix_candidate_experiments_status",
        "ix_candidate_experiments_published_idea_id",
        "ix_candidate_experiments_published",
        "ix_candidate_experiments_ai_verdict",
        "ix_candidate_experiments_quality_gate_result",
        "ix_candidate_experiments_horizon",
        "ix_candidate_experiments_ticker",
        "ix_candidate_experiments_strategy_version",
    ):
        op.drop_index(name, table_name="candidate_experiments")
    op.drop_table("candidate_experiments")

    with op.batch_alter_table("trading_idea_snapshots") as batch:
        batch.drop_index("ix_trading_idea_snapshots_strategy_version")
        for column in reversed(_v2_columns(snapshot=True)):
            batch.drop_column(column.name)
    with op.batch_alter_table("trading_ideas") as batch:
        batch.drop_index("ix_trading_ideas_ai_verdict")
        batch.drop_index("ix_trading_ideas_strategy_version")
        batch.drop_index("ix_trading_ideas_quality_gate_result")
        for column in reversed(_v2_columns()):
            batch.drop_column(column.name)
    with op.batch_alter_table("telegram_users") as batch:
        for column in reversed(_preference_columns()):
            batch.drop_column(column.name)
