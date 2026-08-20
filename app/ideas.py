from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.domain import (
    GeneratedSignal,
    HorizonProfile,
    IdeaDirection,
    IdeaHorizon,
    IdeaStatus,
    InsufficientDataError,
    TradingIdeaData,
)
from app.horizons import get_horizon_profile
from app.idea_repository import IdeaUpsertResult, create_or_update_idea
from app.observation import DataFreshnessGuard
from app.repositories import get_active_instrument
from app.risk import atr_risk_levels, level_risk_levels
from app.signals import SignalService


def calculate_entry_zone(
    *,
    direction: IdeaDirection,
    current_price: float,
    atr: float,
    support_levels: list[float],
    resistance_levels: list[float],
    zone_atr: float,
) -> tuple[float, float]:
    if min(current_price, atr, zone_atr) <= 0:
        raise ValueError("current_price, ATR and zone_atr must be positive")
    max_anchor_distance = atr * 2.5
    if direction == IdeaDirection.BUY:
        candidates = [
            level
            for level in support_levels
            if 0 < level <= current_price and current_price - level <= max_anchor_distance
        ]
        anchor = max(candidates, default=current_price)
    else:
        candidates = [
            level
            for level in resistance_levels
            if level >= current_price and level - current_price <= max_anchor_distance
        ]
        anchor = min(candidates, default=current_price)

    half_width = atr * zone_atr / 2
    lower = max(0.01, anchor - half_width)
    upper = anchor + half_width
    return round(lower, 6), round(upper, 6)


def idea_material_hash(data: TradingIdeaData) -> str:
    material = "|".join(
        [
            data.ticker,
            data.direction.value,
            data.horizon.value,
            f"{data.entry_price_from:.4f}",
            f"{data.entry_price_to:.4f}",
            f"{data.take_profit:.4f}",
            f"{data.stop_loss:.4f}",
            f"{data.confidence:.2f}",
            data.status.value,
        ]
    )
    return hashlib.sha256(material.encode()).hexdigest()


def _unique_rationale(signals: list[GeneratedSignal]) -> list[str]:
    result: list[str] = []
    for signal in signals:
        for reason in signal.rationale:
            value = f"{signal.timeframe}: {reason}"
            if value not in result:
                result.append(value)
            if len(result) == 8:
                return result
    return result or ["Совокупный технический score прошёл порог качества"]


def build_trading_idea(
    settings: Settings,
    *,
    instrument_name: str,
    horizon: IdeaHorizon,
    signals: list[GeneratedSignal],
    now: datetime | None = None,
    fundamental_score: float | None = None,
    news_score: float | None = None,
    profile: HorizonProfile | None = None,
) -> TradingIdeaData | None:
    selected_profile = profile or get_horizon_profile(horizon)
    if selected_profile.horizon != horizon:
        raise ValueError("profile horizon must match requested horizon")
    by_timeframe = {signal.timeframe: signal for signal in signals}
    primary = by_timeframe.get(selected_profile.primary_timeframe)
    if primary is None or primary.atr is None:
        return None
    available = {
        timeframe: weight
        for timeframe, weight in selected_profile.timeframe_weights.items()
        if timeframe in by_timeframe
    }
    coverage = sum(available.values()) / sum(selected_profile.timeframe_weights.values())
    if coverage < 0.5:
        return None
    weight_total = sum(available.values())
    technical_score = (
        sum(
            by_timeframe[timeframe].technical_score * weight
            for timeframe, weight in available.items()
        )
        / weight_total
    )
    factor_values = {"technical": technical_score}
    factor_weights = {"technical": selected_profile.technical_weight}
    if fundamental_score is not None:
        factor_values["fundamental"] = fundamental_score
        factor_weights["fundamental"] = selected_profile.fundamental_weight
    if news_score is not None:
        factor_values["news"] = news_score
        factor_weights["news"] = selected_profile.news_weight
    available_factor_weight = sum(factor_weights.values())
    total_score = (
        sum(factor_values[name] * factor_weights[name] for name in factor_values)
        / available_factor_weight
    )
    if total_score >= settings.signal_threshold:
        direction = IdeaDirection.BUY
    elif total_score <= -settings.signal_threshold:
        direction = IdeaDirection.SELL
    else:
        return None

    confidence = min(95.0, 50.0 + abs(total_score) * 0.45)
    minimum_confidence = max(settings.idea_minimum_confidence, selected_profile.minimum_confidence)
    if confidence < minimum_confidence:
        return None

    current_price = primary.entry_price
    entry_from, entry_to = calculate_entry_zone(
        direction=direction,
        current_price=current_price,
        atr=primary.atr,
        support_levels=primary.support_levels,
        resistance_levels=primary.resistance_levels,
        zone_atr=selected_profile.entry_zone_atr,
    )
    reference_entry = entry_to if direction == IdeaDirection.BUY else entry_from
    risk = None
    if settings.risk_method == "levels":
        risk = level_risk_levels(
            action=direction.value,
            entry=reference_entry,
            support_levels=primary.support_levels,
            resistance_levels=primary.resistance_levels,
            buffer_pct=settings.level_buffer_pct,
            minimum_reward_risk_ratio=settings.minimum_reward_risk_ratio,
        )
    if risk is None:
        risk = atr_risk_levels(
            action=direction.value,
            entry=reference_entry,
            atr=primary.atr,
            stop_multiplier=selected_profile.atr_stop_multiplier,
            take_multiplier=selected_profile.atr_take_multiplier,
        )
    if risk.reward_risk_ratio + 1e-9 < settings.minimum_reward_risk_ratio:
        return None

    if direction == IdeaDirection.BUY:
        expected_return = (risk.take_profit - reference_entry) / reference_entry * 100
        potential_loss = (reference_entry - risk.stop_loss) / reference_entry * 100
        invalidation = f"Идея отменяется при закреплении ниже {risk.stop_loss:.2f} ₽"
        already_invalid = current_price >= risk.take_profit or current_price <= risk.stop_loss
    else:
        expected_return = (reference_entry - risk.take_profit) / reference_entry * 100
        potential_loss = (risk.stop_loss - reference_entry) / reference_entry * 100
        invalidation = f"Идея отменяется при закреплении выше {risk.stop_loss:.2f} ₽"
        already_invalid = current_price <= risk.take_profit or current_price >= risk.stop_loss

    timestamp = now or datetime.now(UTC)
    # Every forward idea starts pending. The formation candle cannot also prove
    # activation; only a later completed primary candle may touch the frozen zone.
    status = IdeaStatus.INVALIDATED if already_invalid else IdeaStatus.PENDING_ENTRY
    factor_scores = {timeframe: by_timeframe[timeframe].factor_scores for timeframe in available}
    relevant_indicators = {
        timeframe: by_timeframe[timeframe].relevant_indicators for timeframe in available
    }
    return TradingIdeaData(
        ticker=primary.secid,
        instrument_name=instrument_name,
        direction=direction,
        horizon=horizon,
        primary_timeframe=selected_profile.primary_timeframe,
        entry_price_from=entry_from,
        entry_price_to=entry_to,
        current_price=current_price,
        take_profit=risk.take_profit,
        stop_loss=risk.stop_loss,
        confidence=round(confidence, 2),
        expected_return_pct=round(expected_return, 4),
        risk_pct=round(potential_loss, 4),
        risk_reward_ratio=round(risk.reward_risk_ratio, 4),
        rationale=_unique_rationale([by_timeframe[key] for key in available]),
        invalidation_reason=invalidation,
        status=status,
        created_at=timestamp,
        activated_at=None,
        activation_price=None,
        expires_at=timestamp + selected_profile.default_expiry,
        source_signal_id=primary.record_id,
        source_timeframes=list(available),
        source_candle_begin=primary.candle_begin,
        technical_score=round(technical_score, 4),
        fundamental_score=round(fundamental_score or 0.0, 4),
        news_score=round(news_score or 0.0, 4),
        total_score=round(total_score, 4),
        observation_mode=settings.observation_mode(horizon),
        atr=primary.atr,
        factor_scores=factor_scores,
        relevant_indicators=relevant_indicators,
    )


class TradingIdeaGenerator:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        signals: SignalService,
        freshness: DataFreshnessGuard | None = None,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.signals = signals
        self.freshness = freshness

    async def generate(
        self,
        ticker: str,
        horizon: IdeaHorizon,
    ) -> IdeaUpsertResult | None:
        ticker = ticker.upper()
        profile = get_horizon_profile(horizon)
        if self.freshness is not None:
            await self.freshness.require_fresh(ticker, horizon)
        async with self.session_factory() as session:
            instrument = await get_active_instrument(session, ticker)
        if instrument is None:
            return None

        generated_signals: list[GeneratedSignal] = []
        for timeframe in profile.timeframe_weights:
            try:
                generated_signals.append(await self.signals.generate(ticker, timeframe))
            except InsufficientDataError:
                continue
        candidate = build_trading_idea(
            self.settings,
            instrument_name=instrument.short_name,
            horizon=horizon,
            signals=generated_signals,
        )
        if candidate is None or candidate.status == IdeaStatus.INVALIDATED:
            return None
        async with self.session_factory() as session, session.begin():
            return await create_or_update_idea(
                session,
                candidate,
                material_hash=idea_material_hash(candidate),
                confidence_delta=self.settings.idea_material_confidence_delta,
            )
