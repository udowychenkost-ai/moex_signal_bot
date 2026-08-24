"""Add provider fallback and raw usage telemetry without rewriting V1/V2 data.

Revision ID: 20260824_0011
Revises: 20260824_0010
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260824_0011"
down_revision = "20260824_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("candidate_experiments") as batch:
        batch.add_column(
            sa.Column(
                "ai_fallback_used",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.add_column(sa.Column("ai_usage_json", sa.Text(), nullable=False, server_default="{}"))
    with op.batch_alter_table("ai_request_logs") as batch:
        batch.add_column(
            sa.Column(
                "fallback_used",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.add_column(sa.Column("usage_json", sa.Text(), nullable=False, server_default="{}"))


def downgrade() -> None:
    with op.batch_alter_table("ai_request_logs") as batch:
        batch.drop_column("usage_json")
        batch.drop_column("fallback_used")
    with op.batch_alter_table("candidate_experiments") as batch:
        batch.drop_column("ai_usage_json")
        batch.drop_column("ai_fallback_used")
