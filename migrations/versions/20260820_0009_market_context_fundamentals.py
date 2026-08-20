"""Add market context and point-in-time fundamentals.

Revision ID: 20260820_0009
Revises: 20260820_0008
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260820_0009"
down_revision = "20260820_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("instruments") as batch:
        batch.add_column(
            sa.Column("sector", sa.String(length=32), nullable=False, server_default="Unknown")
        )
        batch.create_index("ix_instruments_sector", ["sector"])

    sector_updates = {
        "Financials": ("SBER", "VTBR", "MOEX"),
        "Energy": ("GAZP", "LKOH", "ROSN", "NVTK", "TATN", "SIBN", "SNGS"),
        "Information Technology": ("YDEX",),
        "Materials": ("GMKN", "PLZL", "CHMF", "NLMK", "ALRS", "PHOR"),
        "Communication Services": ("MTSS",),
        "Consumer Staples": ("MGNT",),
        "Utilities": ("IRAO",),
    }
    for sector, tickers in sector_updates.items():
        quoted = ",".join(f"'{ticker}'" for ticker in tickers)
        op.execute(f"UPDATE instruments SET sector='{sector}' WHERE secid IN ({quoted})")

    idea_columns = (
        sa.Column("market_regime", sa.String(length=16), nullable=True),
        sa.Column("market_volatility", sa.String(length=16), nullable=True),
        sa.Column("market_regime_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("relative_strength_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column(
            "relative_strength_label",
            sa.String(length=32),
            nullable=False,
            server_default="недоступно",
        ),
        sa.Column("volume_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("volume_state", sa.String(length=16), nullable=False, server_default="UNKNOWN"),
        sa.Column("momentum_extreme_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column(
            "fundamental_label",
            sa.String(length=32),
            nullable=False,
            server_default="нет данных",
        ),
    )
    with op.batch_alter_table("trading_ideas") as batch:
        for column in idea_columns:
            batch.add_column(column)

    snapshot_columns = (
        sa.Column("market_volatility", sa.String(length=16), nullable=True),
        sa.Column("market_regime_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("relative_strength_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("volume_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("momentum_extreme_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("fundamental_components", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("fundamental_publications", sa.Text(), nullable=False, server_default="[]"),
    )
    with op.batch_alter_table("trading_idea_snapshots") as batch:
        for column in snapshot_columns:
            batch.add_column(column)

    op.create_table(
        "market_candles",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("symbol", sa.String(length=24), nullable=False),
        sa.Column("timeframe", sa.String(length=8), nullable=False),
        sa.Column("begin", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Float(), nullable=False),
        sa.Column("high", sa.Float(), nullable=False),
        sa.Column("low", sa.Float(), nullable=False),
        sa.Column("close", sa.Float(), nullable=False),
        sa.Column("volume", sa.Float(), nullable=False, server_default="0"),
        sa.Column("value", sa.Float(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("symbol", "timeframe", "begin", name="uq_market_candle_key"),
    )
    op.create_index("ix_market_candles_symbol", "market_candles", ["symbol"])
    op.create_index(
        "ix_market_candles_lookup",
        "market_candles",
        ["symbol", "timeframe", "begin"],
    )

    op.create_table(
        "fundamental_reports",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("ticker", sa.String(length=36), nullable=False),
        sa.Column("sector", sa.String(length=32), nullable=False),
        sa.Column("report_period", sa.String(length=32), nullable=False),
        sa.Column("publication_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("metrics", sa.Text(), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["ticker"], ["instruments.secid"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "ticker",
            "report_period",
            "available_from",
            "source",
            name="uq_fundamental_report_version",
        ),
    )
    op.create_index("ix_fundamental_reports_ticker", "fundamental_reports", ["ticker"])
    op.create_index("ix_fundamental_reports_sector", "fundamental_reports", ["sector"])
    op.create_index(
        "ix_fundamental_reports_available_from",
        "fundamental_reports",
        ["available_from"],
    )
    op.create_index(
        "ix_fundamental_point_in_time",
        "fundamental_reports",
        ["ticker", "available_from"],
    )


def downgrade() -> None:
    op.drop_index("ix_fundamental_point_in_time", table_name="fundamental_reports")
    op.drop_index("ix_fundamental_reports_available_from", table_name="fundamental_reports")
    op.drop_index("ix_fundamental_reports_sector", table_name="fundamental_reports")
    op.drop_index("ix_fundamental_reports_ticker", table_name="fundamental_reports")
    op.drop_table("fundamental_reports")
    op.drop_index("ix_market_candles_lookup", table_name="market_candles")
    op.drop_index("ix_market_candles_symbol", table_name="market_candles")
    op.drop_table("market_candles")

    with op.batch_alter_table("trading_idea_snapshots") as batch:
        for name in (
            "fundamental_publications",
            "fundamental_components",
            "momentum_extreme_score",
            "volume_score",
            "relative_strength_score",
            "market_regime_score",
            "market_volatility",
        ):
            batch.drop_column(name)
    with op.batch_alter_table("trading_ideas") as batch:
        for name in (
            "fundamental_label",
            "momentum_extreme_score",
            "volume_state",
            "volume_score",
            "relative_strength_label",
            "relative_strength_score",
            "market_regime_score",
            "market_volatility",
            "market_regime",
        ):
            batch.drop_column(name)
    with op.batch_alter_table("instruments") as batch:
        batch.drop_index("ix_instruments_sector")
        batch.drop_column("sector")
