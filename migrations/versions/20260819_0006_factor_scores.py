"""Store extensible technical, fundamental and news factor scores."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260819_0006"
down_revision: str | None = "20260819_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("trading_ideas") as batch:
        batch.add_column(
            sa.Column("technical_score", sa.Float(), server_default="0", nullable=False)
        )
        batch.add_column(
            sa.Column("fundamental_score", sa.Float(), server_default="0", nullable=False)
        )
        batch.add_column(sa.Column("news_score", sa.Float(), server_default="0", nullable=False))
        batch.add_column(sa.Column("total_score", sa.Float(), server_default="0", nullable=False))


def downgrade() -> None:
    with op.batch_alter_table("trading_ideas") as batch:
        batch.drop_column("total_score")
        batch.drop_column("news_score")
        batch.drop_column("fundamental_score")
        batch.drop_column("technical_score")
