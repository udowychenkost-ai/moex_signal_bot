"""Add idempotent Telegram confirmation metadata to v2.4 trade events.

Revision ID: 20260901_0017
Revises: 20260901_0016
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260901_0017"
down_revision = "20260901_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "trade_event_journal",
        sa.Column("confirmed_by_telegram_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "trade_event_journal",
        sa.Column("confirmation_key", sa.String(length=128), nullable=True),
    )
    op.create_index(
        "uq_trade_event_confirmation_key",
        "trade_event_journal",
        ["confirmation_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_trade_event_confirmation_key", table_name="trade_event_journal")
    op.drop_column("trade_event_journal", "confirmation_key")
    op.drop_column("trade_event_journal", "confirmed_by_telegram_id")
