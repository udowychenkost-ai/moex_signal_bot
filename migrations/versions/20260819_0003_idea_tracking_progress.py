"""Track the last candle evaluated for each TradingIdea."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260819_0003"
down_revision: str | None = "20260819_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("trading_ideas") as batch:
        batch.add_column(sa.Column("last_evaluated_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE trading_ideas SET last_evaluated_at = source_candle_begin")


def downgrade() -> None:
    with op.batch_alter_table("trading_ideas") as batch:
        batch.drop_column("last_evaluated_at")
