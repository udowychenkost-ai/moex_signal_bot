"""Add per-user idea follows and watchlist notification preference.

Revision ID: 20260824_0012
Revises: 20260824_0011
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260824_0012"
down_revision = "20260824_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("telegram_users") as batch:
        batch.add_column(
            sa.Column(
                "notify_watchlist",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )
    op.create_table(
        "idea_follows",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("idea_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["telegram_id"],
            ["telegram_users.telegram_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["idea_id"],
            ["trading_ideas.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "telegram_id",
            "idea_id",
            name="uq_idea_follow_user_idea",
        ),
    )
    op.create_index("ix_idea_follows_telegram_id", "idea_follows", ["telegram_id"])
    op.create_index("ix_idea_follows_idea", "idea_follows", ["idea_id"])


def downgrade() -> None:
    op.drop_index("ix_idea_follows_idea", table_name="idea_follows")
    op.drop_index("ix_idea_follows_telegram_id", table_name="idea_follows")
    op.drop_table("idea_follows")
    with op.batch_alter_table("telegram_users") as batch:
        batch.drop_column("notify_watchlist")
