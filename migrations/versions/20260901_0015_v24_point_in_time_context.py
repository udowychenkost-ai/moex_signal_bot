"""Add point-in-time v2.4 context persistence.

Revision ID: 20260901_0015
Revises: 20260901_0014
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260901_0015"
down_revision = "20260901_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "context_records_v24",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("context_type", sa.String(length=24), nullable=False),
        sa.Column("subject", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=128), nullable=False),
        sa.Column("source_class", sa.String(length=32), nullable=False),
        sa.Column("publication_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("data_confidence", sa.String(length=16), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "context_type",
            "subject",
            "source",
            "available_from",
            "payload_hash",
            name="uq_context_record_v24_fact",
        ),
    )
    op.create_index(
        "ix_context_record_v24_asof",
        "context_records_v24",
        ["context_type", "subject", "available_from", "fetched_at"],
    )


def downgrade() -> None:
    op.drop_table("context_records_v24")
