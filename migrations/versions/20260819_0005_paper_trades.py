"""Add paper trades tied one-to-one to published TradingIdeas."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260819_0005"
down_revision: str | None = "20260819_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "paper_trades",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("idea_id", sa.Integer(), nullable=False),
        sa.Column("ticker", sa.String(length=36), nullable=False),
        sa.Column("direction", sa.String(length=8), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("entry_price", sa.Float(), nullable=False),
        sa.Column("exit_price", sa.Float(), nullable=True),
        sa.Column("units", sa.Integer(), nullable=False),
        sa.Column("lots", sa.Integer(), nullable=False),
        sa.Column("risk_budget", sa.Float(), nullable=False),
        sa.Column("actual_risk", sa.Float(), nullable=False),
        sa.Column("position_value", sa.Float(), nullable=False),
        sa.Column("gross_pnl", sa.Float(), nullable=False),
        sa.Column("commission", sa.Float(), nullable=False),
        sa.Column("net_pnl", sa.Float(), nullable=False),
        sa.Column("r_multiple", sa.Float(), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exit_reason", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["idea_id"], ["trading_ideas.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idea_id", name="uq_paper_trade_idea"),
    )
    op.create_index("ix_paper_trades_idea_id", "paper_trades", ["idea_id"])
    op.create_index("ix_paper_trades_status", "paper_trades", ["status"])
    op.create_index("ix_paper_trades_ticker", "paper_trades", ["ticker"])


def downgrade() -> None:
    op.drop_index("ix_paper_trades_ticker", table_name="paper_trades")
    op.drop_index("ix_paper_trades_status", table_name="paper_trades")
    op.drop_index("ix_paper_trades_idea_id", table_name="paper_trades")
    op.drop_table("paper_trades")
