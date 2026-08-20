from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis import TechnicalFeatures, analyze_technical
from app.config import Settings
from app.domain import (
    GeneratedSignal,
    InsufficientDataError,
    MarketContextData,
    TechnicalResult,
    UnknownTickerError,
)
from app.market_context import MarketRegimeService
from app.models import SignalRecord
from app.observation import completed_candles
from app.repositories import get_active_instrument, get_candles
from app.risk import atr_risk_levels, level_risk_levels


def _technical_snapshot(result: TechnicalResult) -> dict[str, float | list[float] | None]:
    return {
        "rsi": result.rsi,
        "macd": result.macd,
        "macd_signal": result.macd_signal,
        "macd_histogram": result.macd_histogram,
        "atr": result.atr,
        "ema20": result.ema20,
        "ema50": result.ema50,
        "sma20": result.sma20,
        "sma50": result.sma50,
        "sma200": result.sma200,
        "adx": result.adx,
        "stochastic_k": result.stochastic_k,
        "stochastic_d": result.stochastic_d,
        "cci": result.cci,
        "bb_high": result.bb_high,
        "bb_low": result.bb_low,
        "bb_percent": result.bb_percent,
        "obv": result.obv,
        "volume_ratio": result.volume_ratio,
        "support_levels": result.support_levels,
        "resistance_levels": result.resistance_levels,
    }


def analyze_signal_technical(
    settings: Settings,
    candles: list[object],
    *,
    features: TechnicalFeatures | None = None,
    market_context: MarketContextData | None = None,
    contextual_weights: dict[str, float] | None = None,
    scoring_model: str | None = None,
) -> TechnicalResult:
    selected_model = scoring_model or settings.technical_scoring_model
    weights = None
    if selected_model == "weighted":
        weights = settings.technical_score_weights
    elif selected_model == "contextual":
        weights = contextual_weights
    return analyze_technical(
        candles,
        scoring_model=selected_model,
        weights=weights,
        features=features,
        market_context=market_context,
    )


def build_signal(
    settings: Settings,
    secid: str,
    timeframe: str,
    candles: list[object],
    *,
    risk_per_trade_pct: float | None = None,
    technical_result: TechnicalResult | None = None,
    market_context: MarketContextData | None = None,
    contextual_weights: dict[str, float] | None = None,
    scoring_model: str | None = None,
) -> GeneratedSignal:
    """Run the production analysis/scoring/risk pipeline on supplied candles."""
    if not candles:
        raise InsufficientDataError(f"Для {secid} {timeframe} свечи ещё не загружены")
    technical = technical_result or analyze_signal_technical(
        settings,
        candles,
        market_context=market_context,
        contextual_weights=contextual_weights,
        scoring_model=scoring_model,
    )
    threshold = settings.signal_threshold
    if technical.score >= threshold:
        action = "BUY"
    elif technical.score <= -threshold:
        action = "SELL"
    else:
        action = "HOLD"

    entry = float(candles[-1].close)
    risk_action = action if action in {"BUY", "SELL"} else "BUY"
    risk = None
    if settings.risk_method == "levels":
        risk = level_risk_levels(
            action=risk_action,
            entry=entry,
            support_levels=technical.support_levels,
            resistance_levels=technical.resistance_levels,
            buffer_pct=settings.level_buffer_pct,
            minimum_reward_risk_ratio=settings.minimum_reward_risk_ratio,
        )
    if risk is None:
        risk = atr_risk_levels(
            action=risk_action,
            entry=entry,
            atr=technical.atr,
            stop_multiplier=settings.atr_stop_multiplier,
            take_multiplier=settings.atr_take_multiplier,
        )
    confidence = min(95.0, 50.0 + abs(technical.score) * 0.45)
    horizon = "intraday" if timeframe in {"5m", "15m", "1h"} else "long_term"
    indicator_snapshot = _technical_snapshot(technical)
    if market_context is not None:
        indicator_snapshot.update(
            {
                "market_benchmark_return_pct": market_context.benchmark_return_pct,
                "instrument_return_pct": market_context.instrument_return_pct,
                "market_drawdown_pct": market_context.drawdown_pct,
                "market_realized_volatility_pct": (market_context.realized_volatility_pct),
                "market_atr_pct": market_context.atr_pct,
            }
        )
    return GeneratedSignal(
        secid=secid,
        timeframe=timeframe,
        horizon=horizon,
        action=action,
        technical_score=technical.score,
        total_score=technical.score,
        confidence=confidence,
        entry_price=entry,
        stop_loss=risk.stop_loss,
        take_profit=risk.take_profit,
        risk_pct=risk_per_trade_pct or settings.default_risk_per_trade_pct,
        reward_risk_ratio=risk.reward_risk_ratio,
        rationale=technical.explanations,
        candle_begin=candles[-1].begin,
        risk_method=risk.method,
        atr=technical.atr,
        support_levels=technical.support_levels,
        resistance_levels=technical.resistance_levels,
        factor_scores=technical.component_scores,
        relevant_indicators=indicator_snapshot,
        raw_component_scores=technical.diagnostic_scores,
        market_regime=market_context.regime if market_context else None,
        market_volatility=market_context.volatility if market_context else None,
        market_regime_score=(market_context.regime_score if market_context else 0.0),
        relative_strength_score=(market_context.relative_strength_score if market_context else 0.0),
        relative_strength_label=(
            market_context.relative_strength_label if market_context else "недоступно"
        ),
        volume_score=technical.diagnostic_scores.get("volume", 0.0),
        volume_state=(
            "UNKNOWN"
            if technical.volume_ratio is None
            else (
                "EXTREME"
                if technical.volume_ratio >= 3
                else (
                    "HIGH"
                    if technical.volume_ratio >= 2
                    else "ELEVATED"
                    if technical.volume_ratio >= 1.3
                    else "NORMAL"
                )
            )
        ),
        momentum_extreme_score=technical.diagnostic_scores.get("momentum_extreme", 0.0),
    )


class SignalService:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        market_context: MarketRegimeService | None = None,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.market_context = market_context

    async def generate(
        self,
        secid: str,
        timeframe: str,
        *,
        risk_per_trade_pct: float | None = None,
        persist: bool = True,
        contextual_weights: dict[str, float] | None = None,
        scoring_model: str | None = None,
    ) -> GeneratedSignal:
        secid = secid.upper()
        async with self.session_factory() as session:
            instrument = await get_active_instrument(session, secid)
            if instrument is None:
                raise UnknownTickerError(f"Неизвестный тикер {secid}")
            candles = await get_candles(session, secid, timeframe, limit=500)
        candles = completed_candles(candles, timeframe)
        context = None
        if self.market_context is not None and self.settings.market_context_enabled:
            context = await self.market_context.analyze(
                secid,
                timeframe,
                instrument_candles=candles,
            )
        technical = analyze_signal_technical(
            self.settings,
            candles,
            market_context=context,
            contextual_weights=contextual_weights,
            scoring_model=scoring_model,
        )
        generated = build_signal(
            self.settings,
            secid,
            timeframe,
            candles,
            risk_per_trade_pct=risk_per_trade_pct,
            market_context=context,
            contextual_weights=contextual_weights,
            scoring_model=scoring_model,
            technical_result=technical,
        )
        if persist:
            async with self.session_factory() as session, session.begin():
                existing = await session.scalar(
                    select(SignalRecord)
                    .where(
                        SignalRecord.secid == generated.secid,
                        SignalRecord.timeframe == generated.timeframe,
                        SignalRecord.candle_begin == generated.candle_begin,
                        SignalRecord.action == generated.action,
                    )
                    .order_by(SignalRecord.id.desc())
                    .limit(1)
                )
                if existing is not None:
                    generated.record_id = existing.id
                    return generated
                record = SignalRecord(
                    secid=generated.secid,
                    timeframe=generated.timeframe,
                    horizon=generated.horizon,
                    action=generated.action,
                    technical_score=generated.technical_score,
                    fundamental_score=0,
                    total_score=generated.total_score,
                    confidence=generated.confidence,
                    entry_price=generated.entry_price,
                    stop_loss=generated.stop_loss,
                    take_profit=generated.take_profit,
                    risk_pct=generated.risk_pct,
                    rationale="\n".join(generated.rationale),
                    candle_begin=generated.candle_begin,
                )
                session.add(record)
                await session.flush()
                generated.record_id = record.id
        return generated


def format_signal(signal: GeneratedSignal) -> str:
    icon = {"BUY": "🟢", "SELL": "🔴", "HOLD": "🟡"}[signal.action]
    horizon = "интрадей" if signal.horizon == "intraday" else "средне-/долгосрок"
    rationale = "\n".join(f"• {line}" for line in signal.rationale[:5])
    hold_note = (
        "\n<i>TP/SL для HOLD показаны как расчётный сценарий покупки.</i>"
        if signal.action == "HOLD"
        else ""
    )
    risk_method = "ATR" if signal.risk_method == "atr" else "уровни S/R"
    return (
        f"{icon} <b>{signal.action} · {signal.secid}</b>\n"
        f"Горизонт: {horizon} · {signal.timeframe}\n"
        f"Техскор: <b>{signal.technical_score:+.0f}</b> / 100\n"
        f"Уверенность модели: {signal.confidence:.0f}%\n\n"
        f"Вход: <b>{signal.entry_price:.2f}</b>\n"
        f"Stop-loss: <b>{signal.stop_loss:.2f}</b>\n"
        f"Take-profit: <b>{signal.take_profit:.2f}</b>\n"
        f"R:R: 1:{signal.reward_risk_ratio:.1f} · метод: {risk_method}\n"
        f"Риск на сделку: {signal.risk_pct:.2f}%\n\n"
        f"<b>Почему:</b>\n{rationale}{hold_note}\n\n"
        "⚠️ Не является индивидуальной инвестиционной рекомендацией."
    )
