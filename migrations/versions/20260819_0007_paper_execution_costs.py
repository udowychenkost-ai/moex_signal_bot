"""Store paper-trading execution prices and slippage."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260819_0007"
down_revision: str | None = "20260819_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("paper_trades") as batch:
        batch.add_column(sa.Column("entry_fill_price", sa.Float(), nullable=True))
        batch.add_column(sa.Column("exit_fill_price", sa.Float(), nullable=True))
        batch.add_column(sa.Column("slippage", sa.Float(), server_default="0", nullable=False))


def downgrade() -> None:
    with op.batch_alter_table("paper_trades") as batch:
        batch.drop_column("slippage")
        batch.drop_column("exit_fill_price")
        batch.drop_column("entry_fill_price")
