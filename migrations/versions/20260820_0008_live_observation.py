"""Add live observation state, immutable snapshots and notification outbox.

Revision ID: 20260820_0008
Revises: 20260819_0007
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260820_0008"
down_revision = "20260819_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("trading_ideas") as batch:
        batch.add_column(
            sa.Column(
                "observation_mode",
                sa.String(length=16),
                nullable=False,
                server_default="RESEARCH",
            )
        )
        batch.create_index("ix_trading_ideas_observation_mode", ["observation_mode"])
    op.execute("UPDATE trading_ideas SET observation_mode = 'PAPER' WHERE horizon = 'POSITION_1M'")

    op.create_table(
        "trading_idea_snapshots",
        sa.Column("idea_id", sa.Integer(), nullable=False),
        sa.Column("decision_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ticker", sa.String(length=36), nullable=False),
        sa.Column("horizon", sa.String(length=16), nullable=False),
        sa.Column("observation_mode", sa.String(length=16), nullable=False),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("factor_scores", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("technical_score", sa.Float(), nullable=False),
        sa.Column("fundamental_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("news_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("total_score", sa.Float(), nullable=False),
        sa.Column("signal_strength", sa.Float(), nullable=False),
        sa.Column("entry_price_from", sa.Float(), nullable=False),
        sa.Column("entry_price_to", sa.Float(), nullable=False),
        sa.Column("take_profit", sa.Float(), nullable=False),
        sa.Column("stop_loss", sa.Float(), nullable=False),
        sa.Column("atr", sa.Float(), nullable=True),
        sa.Column("relevant_indicators", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("regime", sa.String(length=32), nullable=True),
        sa.ForeignKeyConstraint(["idea_id"], ["trading_ideas.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("idea_id"),
    )
    op.create_index(
        "ix_trading_idea_snapshots_decision_at",
        "trading_idea_snapshots",
        ["decision_at"],
    )
    op.create_index("ix_trading_idea_snapshots_ticker", "trading_idea_snapshots", ["ticker"])
    op.create_index("ix_trading_idea_snapshots_horizon", "trading_idea_snapshots", ["horizon"])
    # Historical rows cannot recover indicator detail that was not stored before
    # this revision. Preserve the available values and label that limitation.
    op.execute(
        """
        INSERT INTO trading_idea_snapshots (
            idea_id, decision_at, ticker, horizon, observation_mode, price,
            factor_scores, technical_score, fundamental_score, news_score,
            total_score, signal_strength, entry_price_from, entry_price_to,
            take_profit, stop_loss, atr, relevant_indicators, regime
        )
        SELECT
            id, created_at, ticker, horizon, observation_mode, current_price,
            '{}', technical_score, fundamental_score, news_score,
            total_score, confidence, entry_price_from, entry_price_to,
            take_profit, stop_loss, NULL, '{}', 'legacy_backfill'
        FROM trading_ideas
        """
    )

    op.create_table(
        "forward_notifications",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("notification_key", sa.String(length=128), nullable=False),
        sa.Column("notification_type", sa.String(length=32), nullable=False),
        sa.Column("idea_id", sa.Integer(), nullable=True),
        sa.Column("event_id", sa.Integer(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["trading_idea_events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["idea_id"], ["trading_ideas.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("telegram_id", "notification_key", name="uq_forward_notification"),
    )
    op.create_index(
        "ix_forward_notifications_telegram_id",
        "forward_notifications",
        ["telegram_id"],
    )
    op.create_index("ix_forward_notifications_idea_id", "forward_notifications", ["idea_id"])
    # Do not flood existing Telegram users with the complete pre-observation
    # lifecycle on the first deployment. New events remain deliverable.
    op.execute(
        """
        INSERT INTO forward_notifications (
            telegram_id, notification_key, notification_type,
            idea_id, event_id, sent_at
        )
        SELECT
            u.telegram_id,
            'event:' || CAST(e.id AS VARCHAR),
            e.event_type,
            e.idea_id,
            e.id,
            CURRENT_TIMESTAMP
        FROM telegram_users AS u
        CROSS JOIN trading_idea_events AS e
        WHERE e.event_type IN (
            'CREATED', 'ACTIVATED', 'TP_HIT', 'SL_HIT', 'EXPIRED',
            'ENTRY_MISSED', 'DIRECTION_REVERSED', 'CANCELLED', 'INVALIDATED'
        )
        """
    )

    op.create_table(
        "job_run_states",
        sa.Column("job_name", sa.String(length=64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=True),
        sa.Column("details", sa.Text(), nullable=False, server_default=""),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("job_name"),
    )


def downgrade() -> None:
    op.drop_table("job_run_states")
    op.drop_index("ix_forward_notifications_idea_id", table_name="forward_notifications")
    op.drop_index("ix_forward_notifications_telegram_id", table_name="forward_notifications")
    op.drop_table("forward_notifications")
    op.drop_index("ix_trading_idea_snapshots_horizon", table_name="trading_idea_snapshots")
    op.drop_index("ix_trading_idea_snapshots_ticker", table_name="trading_idea_snapshots")
    op.drop_index("ix_trading_idea_snapshots_decision_at", table_name="trading_idea_snapshots")
    op.drop_table("trading_idea_snapshots")
    with op.batch_alter_table("trading_ideas") as batch:
        batch.drop_index("ix_trading_ideas_observation_mode")
        batch.drop_column("observation_mode")
