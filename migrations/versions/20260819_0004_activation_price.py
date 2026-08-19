"""Store the actual TradingIdea activation price."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260819_0004"
down_revision: str | None = "20260819_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("trading_ideas") as batch:
        batch.add_column(sa.Column("activation_price", sa.Float(), nullable=True))
    op.execute(
        "UPDATE trading_ideas SET activation_price = current_price "
        "WHERE status = 'ACTIVE' AND activated_at IS NOT NULL"
    )


def downgrade() -> None:
    with op.batch_alter_table("trading_ideas") as batch:
        batch.drop_column("activation_price")
