"""Add persistent v2.4 kill switch and journal write probe.

Revision ID: 20260901_0019
Revises: 20260901_0018
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260901_0019"
down_revision = "20260901_0018"
branch_labels = None
depends_on = None


def _create_immutability_guard() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            """
            CREATE TRIGGER trg_kill_switch_events_immutable
            BEFORE UPDATE OR DELETE ON kill_switch_events
            FOR EACH ROW EXECUTE FUNCTION v24_reject_mutation()
            """
        )
    elif dialect == "sqlite":
        op.execute(
            """
            CREATE TRIGGER trg_kill_switch_events_no_update
            BEFORE UPDATE ON kill_switch_events
            BEGIN
                SELECT RAISE(ABORT, 'kill_switch_events is immutable');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_kill_switch_events_no_delete
            BEFORE DELETE ON kill_switch_events
            BEGIN
                SELECT RAISE(ABORT, 'kill_switch_events is append-only');
            END
            """
        )


def _drop_immutability_guard() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS trg_kill_switch_events_immutable ON kill_switch_events")
    elif dialect == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS trg_kill_switch_events_no_update")
        op.execute("DROP TRIGGER IF EXISTS trg_kill_switch_events_no_delete")


def upgrade() -> None:
    op.create_table(
        "kill_switch_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("reasons", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("triggered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by_telegram_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "state IN ('NORMAL', 'CAPITAL_PRESERVATION')",
            name="ck_kill_switch_state",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_kill_switch_state_time",
        "kill_switch_events",
        ["state", "triggered_at"],
    )
    op.create_table(
        "journal_health_probes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    _create_immutability_guard()


def downgrade() -> None:
    _drop_immutability_guard()
    op.drop_table("journal_health_probes")
    op.drop_table("kill_switch_events")
