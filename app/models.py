from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
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
    sector: Mapped[str] = mapped_column(String(32), default="Unknown", index=True)


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
    report_frequency: Mapped[str] = mapped_column(String(16), default="hourly")
    idea_horizon: Mapped[str] = mapped_column(String(16), default="all")
    minimum_confidence: Mapped[float] = mapped_column(Float, default=70.0)
    ai_filter_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_new_idea: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_activation: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_tp: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_sl: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_expiry: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_daily_summary: Mapped[bool] = mapped_column(Boolean, default=True)
    last_report_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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


class TradingIdea(Base):
    __tablename__ = "trading_ideas"
    __table_args__ = (
        CheckConstraint("direction IN ('BUY', 'SELL')", name="ck_trading_idea_direction"),
        CheckConstraint("entry_price_from <= entry_price_to", name="ck_trading_idea_entry_zone"),
        CheckConstraint("confidence >= 0 AND confidence <= 100", name="ck_trading_idea_confidence"),
        Index("ix_trading_ideas_open", "ticker", "horizon", "status"),
        Index("ix_trading_ideas_rank", "horizon", "confidence", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(ForeignKey("instruments.secid", ondelete="CASCADE"))
    instrument_name: Mapped[str] = mapped_column(String(128))
    direction: Mapped[str] = mapped_column(String(8))
    horizon: Mapped[str] = mapped_column(String(16))
    primary_timeframe: Mapped[str] = mapped_column(String(8))
    entry_price_from: Mapped[float] = mapped_column(Float)
    entry_price_to: Mapped[float] = mapped_column(Float)
    current_price: Mapped[float] = mapped_column(Float)
    take_profit: Mapped[float] = mapped_column(Float)
    stop_loss: Mapped[float] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float)
    technical_score: Mapped[float] = mapped_column(Float, default=0)
    fundamental_score: Mapped[float] = mapped_column(Float, default=0)
    news_score: Mapped[float] = mapped_column(Float, default=0)
    total_score: Mapped[float] = mapped_column(Float, default=0)
    observation_mode: Mapped[str] = mapped_column(String(16), default="RESEARCH", index=True)
    market_regime: Mapped[str | None] = mapped_column(String(16), nullable=True)
    market_volatility: Mapped[str | None] = mapped_column(String(16), nullable=True)
    market_regime_score: Mapped[float] = mapped_column(Float, default=0)
    relative_strength_score: Mapped[float] = mapped_column(Float, default=0)
    relative_strength_label: Mapped[str] = mapped_column(String(32), default="недоступно")
    volume_score: Mapped[float] = mapped_column(Float, default=0)
    volume_state: Mapped[str] = mapped_column(String(16), default="UNKNOWN")
    momentum_extreme_score: Mapped[float] = mapped_column(Float, default=0)
    fundamental_label: Mapped[str] = mapped_column(String(32), default="нет данных")
    quality_gate_result: Mapped[str] = mapped_column(String(16), default="LEGACY", index=True)
    final_quality_score: Mapped[float] = mapped_column(Float, default=0)
    supporting_factors: Mapped[str] = mapped_column(Text, default="[]")
    contradicting_factors: Mapped[str] = mapped_column(Text, default="[]")
    confirmation_count: Mapped[int] = mapped_column(Integer, default=0)
    strategy_version: Mapped[str] = mapped_column(String(64), default="v1", index=True)
    ai_verdict: Mapped[str] = mapped_column(String(24), default="NOT_REQUESTED", index=True)
    ai_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    ai_confidence: Mapped[str | None] = mapped_column(String(16), nullable=True)
    ai_bull_case: Mapped[str] = mapped_column(Text, default="")
    ai_bear_case: Mapped[str] = mapped_column(Text, default="")
    ai_key_risks: Mapped[str] = mapped_column(Text, default="[]")
    ai_why_now: Mapped[str] = mapped_column(Text, default="")
    ai_invalidation_conditions: Mapped[str] = mapped_column(Text, default="[]")
    ai_short_summary: Mapped[str] = mapped_column(Text, default="")
    ai_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ai_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ai_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expected_return_pct: Mapped[float] = mapped_column(Float)
    risk_pct: Mapped[float] = mapped_column(Float)
    risk_reward_ratio: Mapped[float] = mapped_column(Float)
    rationale: Mapped[str] = mapped_column(Text)
    invalidation_reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), index=True)
    source_signal_id: Mapped[int | None] = mapped_column(
        ForeignKey("signals.id", ondelete="SET NULL"), nullable=True
    )
    source_timeframes: Mapped[str] = mapped_column(String(64))
    source_candle_begin: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_evaluated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    material_hash: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    activation_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    close_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    close_price: Mapped[float | None] = mapped_column(Float, nullable=True)


class TradingIdeaEvent(Base):
    __tablename__ = "trading_idea_events"
    __table_args__ = (Index("ix_idea_events_lookup", "idea_id", "occurred_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    idea_id: Mapped[int] = mapped_column(
        ForeignKey("trading_ideas.id", ondelete="CASCADE"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(32))
    from_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    to_status: Mapped[str] = mapped_column(String(24))
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    details: Mapped[str] = mapped_column(Text, default="")
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class TradingIdeaSnapshot(Base):
    """Immutable decision-time evidence. Application code only inserts this row."""

    __tablename__ = "trading_idea_snapshots"

    idea_id: Mapped[int] = mapped_column(
        ForeignKey("trading_ideas.id", ondelete="CASCADE"), primary_key=True
    )
    decision_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ticker: Mapped[str] = mapped_column(String(36), index=True)
    horizon: Mapped[str] = mapped_column(String(16), index=True)
    observation_mode: Mapped[str] = mapped_column(String(16))
    price: Mapped[float] = mapped_column(Float)
    factor_scores: Mapped[str] = mapped_column(Text, default="{}")
    technical_score: Mapped[float] = mapped_column(Float)
    fundamental_score: Mapped[float] = mapped_column(Float, default=0)
    news_score: Mapped[float] = mapped_column(Float, default=0)
    total_score: Mapped[float] = mapped_column(Float)
    signal_strength: Mapped[float] = mapped_column(Float)
    entry_price_from: Mapped[float] = mapped_column(Float)
    entry_price_to: Mapped[float] = mapped_column(Float)
    take_profit: Mapped[float] = mapped_column(Float)
    stop_loss: Mapped[float] = mapped_column(Float)
    atr: Mapped[float | None] = mapped_column(Float, nullable=True)
    relevant_indicators: Mapped[str] = mapped_column(Text, default="{}")
    regime: Mapped[str | None] = mapped_column(String(32), nullable=True)
    market_volatility: Mapped[str | None] = mapped_column(String(16), nullable=True)
    market_regime_score: Mapped[float] = mapped_column(Float, default=0)
    relative_strength_score: Mapped[float] = mapped_column(Float, default=0)
    volume_score: Mapped[float] = mapped_column(Float, default=0)
    momentum_extreme_score: Mapped[float] = mapped_column(Float, default=0)
    fundamental_components: Mapped[str] = mapped_column(Text, default="{}")
    fundamental_publications: Mapped[str] = mapped_column(Text, default="[]")
    quality_gate_result: Mapped[str] = mapped_column(String(16), default="LEGACY")
    final_quality_score: Mapped[float] = mapped_column(Float, default=0)
    supporting_factors: Mapped[str] = mapped_column(Text, default="[]")
    contradicting_factors: Mapped[str] = mapped_column(Text, default="[]")
    confirmation_count: Mapped[int] = mapped_column(Integer, default=0)
    strategy_version: Mapped[str] = mapped_column(String(64), default="v1", index=True)
    ai_verdict: Mapped[str] = mapped_column(String(24), default="NOT_REQUESTED")
    ai_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    ai_confidence: Mapped[str | None] = mapped_column(String(16), nullable=True)
    ai_bull_case: Mapped[str] = mapped_column(Text, default="")
    ai_bear_case: Mapped[str] = mapped_column(Text, default="")
    ai_key_risks: Mapped[str] = mapped_column(Text, default="[]")
    ai_why_now: Mapped[str] = mapped_column(Text, default="")
    ai_invalidation_conditions: Mapped[str] = mapped_column(Text, default="[]")
    ai_short_summary: Mapped[str] = mapped_column(Text, default="")
    ai_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ai_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ai_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CandidateExperiment(Base):
    """Frozen V2 quant/AI candidate, including candidates never published to Telegram."""

    __tablename__ = "candidate_experiments"
    __table_args__ = (
        UniqueConstraint("candidate_key", name="uq_candidate_experiment_key"),
        Index("ix_candidate_experiment_cohort", "strategy_version", "horizon", "ai_verdict"),
        Index("ix_candidate_experiment_open", "ticker", "primary_timeframe", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_key: Mapped[str] = mapped_column(String(64))
    strategy_version: Mapped[str] = mapped_column(String(64), index=True)
    ticker: Mapped[str] = mapped_column(
        ForeignKey("instruments.secid", ondelete="CASCADE"), index=True
    )
    direction: Mapped[str] = mapped_column(String(8))
    horizon: Mapped[str] = mapped_column(String(16), index=True)
    primary_timeframe: Mapped[str] = mapped_column(String(8))
    quant_result: Mapped[str] = mapped_column(String(16))
    quality_gate_result: Mapped[str] = mapped_column(String(16), index=True)
    final_quality_score: Mapped[float] = mapped_column(Float)
    supporting_factors: Mapped[str] = mapped_column(Text, default="[]")
    contradicting_factors: Mapped[str] = mapped_column(Text, default="[]")
    quality_reasons: Mapped[str] = mapped_column(Text, default="[]")
    confirmation_count: Mapped[int] = mapped_column(Integer, default=0)
    timeframe_confirmations: Mapped[int] = mapped_column(Integer, default=0)
    ai_verdict: Mapped[str] = mapped_column(String(24), default="NOT_REQUESTED", index=True)
    ai_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    ai_confidence: Mapped[str | None] = mapped_column(String(16), nullable=True)
    ai_bull_case: Mapped[str] = mapped_column(Text, default="")
    ai_bear_case: Mapped[str] = mapped_column(Text, default="")
    ai_key_risks: Mapped[str] = mapped_column(Text, default="[]")
    ai_why_now: Mapped[str] = mapped_column(Text, default="")
    ai_invalidation_conditions: Mapped[str] = mapped_column(Text, default="[]")
    ai_short_summary: Mapped[str] = mapped_column(Text, default="")
    ai_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ai_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ai_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ai_status: Mapped[str] = mapped_column(String(16), default="NOT_REQUESTED")
    ai_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    ai_output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    ai_estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0)
    ai_latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    ai_error: Mapped[str] = mapped_column(Text, default="")
    ai_fallback_used: Mapped[bool] = mapped_column(Boolean, default=False)
    ai_usage_json: Mapped[str] = mapped_column(Text, default="{}")
    published: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    publish_reason: Mapped[str] = mapped_column(String(32), default="")
    published_idea_id: Mapped[int | None] = mapped_column(
        ForeignKey("trading_ideas.id", ondelete="SET NULL"), nullable=True, index=True
    )
    current_price: Mapped[float] = mapped_column(Float)
    entry_price_from: Mapped[float] = mapped_column(Float)
    entry_price_to: Mapped[float] = mapped_column(Float)
    take_profit: Mapped[float] = mapped_column(Float)
    stop_loss: Mapped[float] = mapped_column(Float)
    risk_reward_ratio: Mapped[float] = mapped_column(Float)
    technical_score: Mapped[float] = mapped_column(Float)
    fundamental_score: Mapped[float] = mapped_column(Float, default=0)
    news_score: Mapped[float] = mapped_column(Float, default=0)
    total_score: Mapped[float] = mapped_column(Float)
    signal_strength: Mapped[float] = mapped_column(Float)
    snapshot_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), index=True)
    source_candle_begin: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decision_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_evaluated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    activation_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    close_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    actual_r: Mapped[float | None] = mapped_column(Float, nullable=True)


class AIRequestLog(Base):
    __tablename__ = "ai_request_logs"
    __table_args__ = (Index("ix_ai_request_created", "request_kind", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_kind: Mapped[str] = mapped_column(String(32), default="CANDIDATE")
    candidate_id: Mapped[int | None] = mapped_column(
        ForeignKey("candidate_experiments.id", ondelete="SET NULL"), nullable=True, index=True
    )
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    fallback_used: Mapped[bool] = mapped_column(Boolean, default=False)
    usage_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class MarketCandle(Base):
    __tablename__ = "market_candles"
    __table_args__ = (
        UniqueConstraint("symbol", "timeframe", "begin", name="uq_market_candle_key"),
        Index("ix_market_candles_lookup", "symbol", "timeframe", "begin"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(24), index=True)
    timeframe: Mapped[str] = mapped_column(String(8))
    begin: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float, default=0)
    value: Mapped[float] = mapped_column(Float, default=0)


class FundamentalReport(Base):
    __tablename__ = "fundamental_reports"
    __table_args__ = (
        UniqueConstraint(
            "ticker",
            "report_period",
            "available_from",
            "source",
            name="uq_fundamental_report_version",
        ),
        Index("ix_fundamental_point_in_time", "ticker", "available_from"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(
        ForeignKey("instruments.secid", ondelete="CASCADE"), index=True
    )
    sector: Mapped[str] = mapped_column(String(32), index=True)
    report_period: Mapped[str] = mapped_column(String(32))
    publication_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    available_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    source: Mapped[str] = mapped_column(String(64))
    source_url: Mapped[str] = mapped_column(Text)
    metrics: Mapped[str] = mapped_column(Text)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class IdeaNotification(Base):
    __tablename__ = "idea_notifications"
    __table_args__ = (
        UniqueConstraint(
            "telegram_id", "idea_id", "idea_version", name="uq_idea_notification_version"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(
        ForeignKey("telegram_users.telegram_id", ondelete="CASCADE")
    )
    idea_id: Mapped[int] = mapped_column(
        ForeignKey("trading_ideas.id", ondelete="CASCADE"), index=True
    )
    idea_version: Mapped[int] = mapped_column(Integer)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ForwardNotification(Base):
    __tablename__ = "forward_notifications"
    __table_args__ = (
        UniqueConstraint("telegram_id", "notification_key", name="uq_forward_notification"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    notification_key: Mapped[str] = mapped_column(String(128))
    notification_type: Mapped[str] = mapped_column(String(32))
    idea_id: Mapped[int | None] = mapped_column(
        ForeignKey("trading_ideas.id", ondelete="CASCADE"), nullable=True, index=True
    )
    event_id: Mapped[int | None] = mapped_column(
        ForeignKey("trading_idea_events.id", ondelete="CASCADE"), nullable=True
    )
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class JobRunState(Base):
    __tablename__ = "job_run_states"

    job_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    success: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    details: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class PaperTrade(Base):
    __tablename__ = "paper_trades"
    __table_args__ = (UniqueConstraint("idea_id", name="uq_paper_trade_idea"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    idea_id: Mapped[int] = mapped_column(
        ForeignKey("trading_ideas.id", ondelete="CASCADE"), index=True
    )
    ticker: Mapped[str] = mapped_column(String(36), index=True)
    direction: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(16), index=True)
    entry_price: Mapped[float] = mapped_column(Float)
    entry_fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    units: Mapped[int] = mapped_column(Integer)
    lots: Mapped[int] = mapped_column(Integer)
    risk_budget: Mapped[float] = mapped_column(Float)
    actual_risk: Mapped[float] = mapped_column(Float)
    position_value: Mapped[float] = mapped_column(Float)
    gross_pnl: Mapped[float] = mapped_column(Float, default=0)
    commission: Mapped[float] = mapped_column(Float, default=0)
    slippage: Mapped[float] = mapped_column(Float, default=0)
    net_pnl: Mapped[float] = mapped_column(Float, default=0)
    r_multiple: Mapped[float] = mapped_column(Float, default=0)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
