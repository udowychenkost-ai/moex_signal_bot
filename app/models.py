from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Instrument(Base):
    __tablename__ = "instruments"

    secid: Mapped[str] = mapped_column(String(36), primary_key=True)
    board_id: Mapped[str] = mapped_column(String(12), default="TQBR")
    instrument_type: Mapped[str] = mapped_column(String(16), default="share")
    short_name: Mapped[str] = mapped_column(String(128))
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    isin: Mapped[str | None] = mapped_column(String(24), nullable=True)
    lot_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    market_cap: Mapped[float | None] = mapped_column(Float, nullable=True)
    daily_turnover: Mapped[float | None] = mapped_column(Float, nullable=True)
    free_float: Mapped[float | None] = mapped_column(Float, nullable=True)
    echelon: Mapped[int] = mapped_column(Integer, default=2, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Candle(Base):
    __tablename__ = "candles"
    __table_args__ = (
        UniqueConstraint("secid", "board_id", "timeframe", "begin", name="uq_candle_key"),
        Index("ix_candles_lookup", "secid", "timeframe", "begin"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    secid: Mapped[str] = mapped_column(ForeignKey("instruments.secid", ondelete="CASCADE"))
    board_id: Mapped[str] = mapped_column(String(12), default="TQBR")
    timeframe: Mapped[str] = mapped_column(String(8))
    begin: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float, default=0)
    value: Mapped[float] = mapped_column(Float, default=0)


class OrderBookLevel(Base):
    __tablename__ = "order_book_levels"
    __table_args__ = (Index("ix_order_book_snapshot", "secid", "snapshot_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    secid: Mapped[str] = mapped_column(ForeignKey("instruments.secid", ondelete="CASCADE"))
    board_id: Mapped[str] = mapped_column(String(12), default="TQBR")
    snapshot_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    side: Mapped[str] = mapped_column(String(1))
    level: Mapped[int] = mapped_column(Integer)
    price: Mapped[float] = mapped_column(Float)
    quantity: Mapped[float] = mapped_column(Float)


class TelegramUser(Base):
    __tablename__ = "telegram_users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    risk_profile: Mapped[str] = mapped_column(String(16), default="balanced")
    risk_per_trade_pct: Mapped[float] = mapped_column(Float, default=1.0)
    default_timeframe: Mapped[str] = mapped_column(String(8), default="15m")
    echelon_filter: Mapped[str] = mapped_column(String(8), default="both")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class WatchlistItem(Base):
    __tablename__ = "watchlist_items"
    __table_args__ = (UniqueConstraint("telegram_id", "secid", name="uq_watchlist_item"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(
        ForeignKey("telegram_users.telegram_id", ondelete="CASCADE")
    )
    secid: Mapped[str] = mapped_column(ForeignKey("instruments.secid", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class SignalRecord(Base):
    __tablename__ = "signals"
    __table_args__ = (Index("ix_signals_lookup", "secid", "timeframe", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    secid: Mapped[str] = mapped_column(ForeignKey("instruments.secid", ondelete="CASCADE"))
    timeframe: Mapped[str] = mapped_column(String(8))
    horizon: Mapped[str] = mapped_column(String(16))
    action: Mapped[str] = mapped_column(String(8))
    technical_score: Mapped[float] = mapped_column(Float)
    fundamental_score: Mapped[float] = mapped_column(Float, default=0)
    total_score: Mapped[float] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float)
    entry_price: Mapped[float] = mapped_column(Float)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    risk_pct: Mapped[float] = mapped_column(Float)
    rationale: Mapped[str] = mapped_column(Text)
    candle_begin: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
