"""Add TradingIdea lifecycle and report preferences."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260819_0002"
down_revision: str | None = "20260819_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("telegram_users") as batch:
        batch.add_column(
            sa.Column(
                "report_frequency", sa.String(length=16), server_default="hourly", nullable=False
            )
        )
        batch.add_column(
            sa.Column("idea_horizon", sa.String(length=16), server_default="all", nullable=False)
        )
        batch.add_column(
            sa.Column("minimum_confidence", sa.Float(), server_default="70", nullable=False)
        )
        batch.add_column(sa.Column("last_report_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "trading_ideas",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("ticker", sa.String(length=36), nullable=False),
        sa.Column("instrument_name", sa.String(length=128), nullable=False),
        sa.Column("direction", sa.String(length=8), nullable=False),
        sa.Column("horizon", sa.String(length=16), nullable=False),
        sa.Column("primary_timeframe", sa.String(length=8), nullable=False),
        sa.Column("entry_price_from", sa.Float(), nullable=False),
        sa.Column("entry_price_to", sa.Float(), nullable=False),
        sa.Column("current_price", sa.Float(), nullable=False),
        sa.Column("take_profit", sa.Float(), nullable=False),
        sa.Column("stop_loss", sa.Float(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("expected_return_pct", sa.Float(), nullable=False),
        sa.Column("risk_pct", sa.Float(), nullable=False),
        sa.Column("risk_reward_ratio", sa.Float(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("invalidation_reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("source_signal_id", sa.Integer(), nullable=True),
        sa.Column("source_timeframes", sa.String(length=64), nullable=False),
        sa.Column("source_candle_begin", sa.DateTime(timezone=True), nullable=False),
        sa.Column("material_hash", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("close_reason", sa.String(length=64), nullable=True),
        sa.Column("close_price", sa.Float(), nullable=True),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 100", name="ck_trading_idea_confidence"
        ),
        sa.CheckConstraint("direction IN ('BUY', 'SELL')", name="ck_trading_idea_direction"),
        sa.CheckConstraint("entry_price_from <= entry_price_to", name="ck_trading_idea_entry_zone"),
        sa.ForeignKeyConstraint(["ticker"], ["instruments.secid"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_signal_id"], ["signals.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_trading_ideas_status", "trading_ideas", ["status"])
    op.create_index("ix_trading_ideas_material_hash", "trading_ideas", ["material_hash"])
    op.create_index("ix_trading_ideas_expires_at", "trading_ideas", ["expires_at"])
    op.create_index("ix_trading_ideas_open", "trading_ideas", ["ticker", "horizon", "status"])
    op.create_index(
        "ix_trading_ideas_rank",
        "trading_ideas",
        ["horizon", "confidence", "created_at"],
    )

    op.create_table(
        "trading_idea_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("idea_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("from_status", sa.String(length=24), nullable=True),
        sa.Column("to_status", sa.String(length=24), nullable=False),
        sa.Column("price", sa.Float(), nullable=True),
        sa.Column("details", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["idea_id"], ["trading_ideas.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_trading_idea_events_idea_id", "trading_idea_events", ["idea_id"])
    op.create_index("ix_idea_events_lookup", "trading_idea_events", ["idea_id", "occurred_at"])

    op.create_table(
        "idea_notifications",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("idea_id", sa.Integer(), nullable=False),
        sa.Column("idea_version", sa.Integer(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["idea_id"], ["trading_ideas.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["telegram_id"], ["telegram_users.telegram_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "telegram_id", "idea_id", "idea_version", name="uq_idea_notification_version"
        ),
    )
    op.create_index("ix_idea_notifications_idea_id", "idea_notifications", ["idea_id"])


def downgrade() -> None:
    op.drop_index("ix_idea_notifications_idea_id", table_name="idea_notifications")
    op.drop_table("idea_notifications")
    op.drop_index("ix_idea_events_lookup", table_name="trading_idea_events")
    op.drop_index("ix_trading_idea_events_idea_id", table_name="trading_idea_events")
    op.drop_table("trading_idea_events")
    op.drop_index("ix_trading_ideas_rank", table_name="trading_ideas")
    op.drop_index("ix_trading_ideas_open", table_name="trading_ideas")
    op.drop_index("ix_trading_ideas_expires_at", table_name="trading_ideas")
    op.drop_index("ix_trading_ideas_material_hash", table_name="trading_ideas")
    op.drop_index("ix_trading_ideas_status", table_name="trading_ideas")
    op.drop_table("trading_ideas")
    with op.batch_alter_table("telegram_users") as batch:
        batch.drop_column("last_report_at")
        batch.drop_column("minimum_confidence")
        batch.drop_column("idea_horizon")
        batch.drop_column("report_frequency")
