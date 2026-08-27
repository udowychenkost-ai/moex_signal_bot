"""Allow real ISS best quotes when public depth is unavailable.

Revision ID: 20260827_0013
Revises: 20260824_0012
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260827_0013"
down_revision = "20260824_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("order_book_levels") as batch:
        batch.alter_column(
            "quantity",
            existing_type=sa.Float(),
            nullable=True,
        )


def downgrade() -> None:
    # Older code used zero for unavailable public depth. This is intentionally
    # confined to downgrade compatibility; the upgraded application uses NULL.
    op.execute("UPDATE order_book_levels SET quantity = 0 WHERE quantity IS NULL")
    with op.batch_alter_table("order_book_levels") as batch:
        batch.alter_column(
            "quantity",
            existing_type=sa.Float(),
            nullable=False,
        )
