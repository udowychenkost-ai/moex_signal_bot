"""Create the pre-TradingIdea application schema."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260819_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "instruments",
        sa.Column("secid", sa.String(length=36), nullable=False),
        sa.Column("board_id", sa.String(length=12), nullable=False),
        sa.Column("instrument_type", sa.String(length=16), nullable=False),
        sa.Column("short_name", sa.String(length=128), nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=True),
        sa.Column("isin", sa.String(length=24), nullable=True),
        sa.Column("lot_size", sa.Integer(), nullable=True),
        sa.Column("last_price", sa.Float(), nullable=True),
        sa.Column("market_cap", sa.Float(), nullable=True),
        sa.Column("daily_turnover", sa.Float(), nullable=True),
        sa.Column("free_float", sa.Float(), nullable=True),
        sa.Column("echelon", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("secid"),
    )
    op.create_index("ix_instruments_echelon", "instruments", ["echelon"])

    op.create_table(
        "telegram_users",
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("risk_profile", sa.String(length=16), nullable=False),
        sa.Column("risk_per_trade_pct", sa.Float(), nullable=False),
        sa.Column("default_timeframe", sa.String(length=8), nullable=False),
        sa.Column("echelon_filter", sa.String(length=8), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("telegram_id"),
    )

    op.create_table(
        "candles",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("secid", sa.String(length=36), nullable=False),
        sa.Column("board_id", sa.String(length=12), nullable=False),
        sa.Column("timeframe", sa.String(length=8), nullable=False),
        sa.Column("begin", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Float(), nullable=False),
        sa.Column("high", sa.Float(), nullable=False),
        sa.Column("low", sa.Float(), nullable=False),
        sa.Column("close", sa.Float(), nullable=False),
        sa.Column("volume", sa.Float(), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["secid"], ["instruments.secid"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("secid", "board_id", "timeframe", "begin", name="uq_candle_key"),
    )
    op.create_index("ix_candles_lookup", "candles", ["secid", "timeframe", "begin"])

    op.create_table(
        "order_book_levels",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("secid", sa.String(length=36), nullable=False),
        sa.Column("board_id", sa.String(length=12), nullable=False),
        sa.Column("snapshot_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("side", sa.String(length=1), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["secid"], ["instruments.secid"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_order_book_snapshot", "order_book_levels", ["secid", "snapshot_at"])

    op.create_table(
        "watchlist_items",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("secid", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["secid"], ["instruments.secid"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["telegram_id"], ["telegram_users.telegram_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("telegram_id", "secid", name="uq_watchlist_item"),
    )

    op.create_table(
        "signals",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("secid", sa.String(length=36), nullable=False),
        sa.Column("timeframe", sa.String(length=8), nullable=False),
        sa.Column("horizon", sa.String(length=16), nullable=False),
        sa.Column("action", sa.String(length=8), nullable=False),
        sa.Column("technical_score", sa.Float(), nullable=False),
        sa.Column("fundamental_score", sa.Float(), nullable=False),
        sa.Column("total_score", sa.Float(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("entry_price", sa.Float(), nullable=False),
        sa.Column("stop_loss", sa.Float(), nullable=True),
        sa.Column("take_profit", sa.Float(), nullable=True),
        sa.Column("risk_pct", sa.Float(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("candle_begin", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["secid"], ["instruments.secid"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_signals_lookup", "signals", ["secid", "timeframe", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_signals_lookup", table_name="signals")
    op.drop_table("signals")
    op.drop_table("watchlist_items")
    op.drop_index("ix_order_book_snapshot", table_name="order_book_levels")
    op.drop_table("order_book_levels")
    op.drop_index("ix_candles_lookup", table_name="candles")
    op.drop_table("candles")
    op.drop_table("telegram_users")
    op.drop_index("ix_instruments_echelon", table_name="instruments")
    op.drop_table("instruments")
