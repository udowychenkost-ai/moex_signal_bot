from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis import analyze_technical
from app.config import Settings
from app.domain import GeneratedSignal, InsufficientDataError, UnknownTickerError
from app.models import SignalRecord
from app.repositories import get_active_instrument, get_candles
from app.risk import atr_risk_levels, level_risk_levels


class SignalService:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory

    async def generate(
        self,
        secid: str,
        timeframe: str,
        *,
        risk_per_trade_pct: float | None = None,
    ) -> GeneratedSignal:
        secid = secid.upper()
        async with self.session_factory() as session:
            instrument = await get_active_instrument(session, secid)
            if instrument is None:
                raise UnknownTickerError(f"Неизвестный тикер {secid}")
            candles = await get_candles(session, secid, timeframe, limit=500)
        if not candles:
            raise InsufficientDataError(f"Для {secid} {timeframe} свечи ещё не загружены")

        weights = (
            self.settings.technical_score_weights
            if self.settings.technical_scoring_model == "weighted"
            else None
        )
        technical = analyze_technical(
            candles,
            scoring_model=self.settings.technical_scoring_model,
            weights=weights,
        )
        threshold = self.settings.signal_threshold
        if technical.score >= threshold:
            action = "BUY"
        elif technical.score <= -threshold:
            action = "SELL"
        else:
            action = "HOLD"

        entry = float(candles[-1].close)
        risk_action = action if action in {"BUY", "SELL"} else "BUY"
        risk = None
        if self.settings.risk_method == "levels":
            risk = level_risk_levels(
                action=risk_action,
                entry=entry,
                support_levels=technical.support_levels,
                resistance_levels=technical.resistance_levels,
                buffer_pct=self.settings.level_buffer_pct,
                minimum_reward_risk_ratio=self.settings.minimum_reward_risk_ratio,
            )
        if risk is None:
            risk = atr_risk_levels(
                action=risk_action,
                entry=entry,
                atr=technical.atr,
                stop_multiplier=self.settings.atr_stop_multiplier,
                take_multiplier=self.settings.atr_take_multiplier,
            )
        confidence = min(95.0, 50.0 + abs(technical.score) * 0.45)
        horizon = "intraday" if timeframe in {"5m", "15m", "1h"} else "long_term"
        user_risk = risk_per_trade_pct or self.settings.default_risk_per_trade_pct
        generated = GeneratedSignal(
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
            risk_pct=user_risk,
            reward_risk_ratio=risk.reward_risk_ratio,
            rationale=technical.explanations,
            candle_begin=candles[-1].begin,
            risk_method=risk.method,
        )
        async with self.session_factory() as session, session.begin():
            session.add(
                SignalRecord(
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
            )
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
