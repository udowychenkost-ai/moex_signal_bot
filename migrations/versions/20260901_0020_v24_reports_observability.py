"""Add immutable daily journal summaries and v2.4 notification outbox.

Revision ID: 20260901_0020
Revises: 20260901_0019
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260901_0020"
down_revision = "20260901_0019"
branch_labels = None
depends_on = None


def _create_summary_guard() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            """
            CREATE TRIGGER trg_daily_journal_summaries_v24_immutable
            BEFORE UPDATE OR DELETE ON daily_journal_summaries_v24
            FOR EACH ROW EXECUTE FUNCTION v24_reject_mutation()
            """
        )
    elif dialect == "sqlite":
        op.execute(
            """
            CREATE TRIGGER trg_daily_journal_summaries_v24_no_update
            BEFORE UPDATE ON daily_journal_summaries_v24
            BEGIN
                SELECT RAISE(ABORT, 'daily_journal_summaries_v24 is immutable');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_daily_journal_summaries_v24_no_delete
            BEFORE DELETE ON daily_journal_summaries_v24
            BEGIN
                SELECT RAISE(ABORT, 'daily_journal_summaries_v24 is append-only');
            END
            """
        )


def _drop_summary_guard() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_daily_journal_summaries_v24_immutable "
            "ON daily_journal_summaries_v24"
        )
    elif dialect == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS trg_daily_journal_summaries_v24_no_update")
        op.execute("DROP TRIGGER IF EXISTS trg_daily_journal_summaries_v24_no_delete")


def upgrade() -> None:
    op.create_table(
        "daily_journal_summaries_v24",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("summary_date", sa.Date(), nullable=False),
        sa.Column("strategy_version", sa.String(length=64), nullable=False),
        sa.Column("ideas_issued", sa.Integer(), nullable=False),
        sa.Column("model_trades_opened", sa.Integer(), nullable=False),
        sa.Column("model_trades_closed", sa.Integer(), nullable=False),
        sa.Column("actual_trades_confirmed", sa.Integer(), nullable=False),
        sa.Column("model_net_pl_rub", sa.Float(), nullable=True),
        sa.Column("actual_net_pl_rub", sa.Float(), nullable=True),
        sa.Column("model_avg_r", sa.Float(), nullable=True),
        sa.Column("actual_avg_r", sa.Float(), nullable=True),
        sa.Column("best_trade", sa.String(length=64), nullable=True),
        sa.Column("worst_trade", sa.String(length=64), nullable=True),
        sa.Column("max_intraday_drawdown", sa.Float(), nullable=True),
        sa.Column("data_issues", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("execution_issues", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("false_positives", sa.Integer(), nullable=True),
        sa.Column("false_rejects", sa.Integer(), nullable=True),
        sa.Column("rule_violations", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lessons", sa.Text(), nullable=False, server_default="[]"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "summary_date",
            "strategy_version",
            name="uq_daily_journal_v24",
        ),
    )
    op.create_table(
        "v24_notification_outbox",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("notification_key", sa.String(length=160), nullable=False),
        sa.Column("notification_type", sa.String(length=40), nullable=False),
        sa.Column("trade_id", sa.String(length=64), nullable=True),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="PENDING"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'SENDING', 'SENT', 'ERROR')",
            name="ck_v24_outbox_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "telegram_id",
            "notification_key",
            name="uq_v24_notification_outbox",
        ),
    )
    op.create_index(
        "ix_v24_outbox_pending",
        "v24_notification_outbox",
        ["status", "available_at"],
    )
    _create_summary_guard()


def downgrade() -> None:
    _drop_summary_guard()
    op.drop_table("v24_notification_outbox")
    op.drop_table("daily_journal_summaries_v24")
