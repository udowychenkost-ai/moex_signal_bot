from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.domain import (
    FundamentalScoreData,
    GeneratedSignal,
    HorizonProfile,
    IdeaDirection,
    IdeaHorizon,
    IdeaStatus,
    InsufficientDataError,
    TradingIdeaData,
)
from app.fundamentals import FundamentalAnalysisService
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


def _context_rationale(
    primary: GeneratedSignal,
    technical_components: dict[str, float],
    fundamental: FundamentalScoreData | None,
) -> list[str]:
    reasons: list[str] = []
    if primary.market_regime:
        reasons.append(
            f"IMOEX: {primary.market_regime}, режим {primary.market_regime_score:+.0f}/100"
        )
    if primary.relative_strength_label != "недоступно":
        reasons.append(
            f"Относительная сила к IMOEX: {primary.relative_strength_label} "
            f"({primary.relative_strength_score:+.0f}/100)"
        )
    volume = technical_components.get("volume", 0.0)
    if abs(volume) >= 15:
        reasons.append(f"Объём {primary.volume_state.lower()}: подтверждение {volume:+.0f}/100")
    extreme = technical_components.get("momentum_extreme", 0.0)
    if abs(extreme) >= 10:
        direction = (
            "восстановление из перепроданности" if extreme > 0 else "ослабление из перекупленности"
        )
        reasons.append(f"Momentum extreme: {direction} ({extreme:+.0f}/100)")
    if fundamental is not None and fundamental.publications:
        reasons.append(
            f"Фундаментал: {fundamental.label} относительно сектора ({fundamental.score:+.0f}/100)"
        )
    return reasons


def build_trading_idea(
    settings: Settings,
    *,
    instrument_name: str,
    horizon: IdeaHorizon,
    signals: list[GeneratedSignal],
    now: datetime | None = None,
    fundamental_score: float | None = None,
    fundamental_result: FundamentalScoreData | None = None,
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
    effective_fundamental = (
        fundamental_result.score
        if fundamental_result is not None and fundamental_result.publications
        else fundamental_score
    )
    factor_values = {"technical": technical_score}
    factor_weights = {"technical": selected_profile.technical_weight}
    if effective_fundamental is not None:
        factor_values["fundamental"] = effective_fundamental
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
    technical_components = {
        component: round(
            sum(
                by_timeframe[timeframe].raw_component_scores.get(component, 0.0) * weight
                for timeframe, weight in available.items()
            )
            / weight_total,
            4,
        )
        for component in selected_profile.technical_component_weights
    }
    factor_scores: dict[str, object] = {
        timeframe: by_timeframe[timeframe].factor_scores for timeframe in available
    }
    factor_scores["technical_components"] = technical_components
    factor_scores["factor_mix"] = {name: round(value, 4) for name, value in factor_values.items()}
    relevant_indicators = {
        timeframe: by_timeframe[timeframe].relevant_indicators for timeframe in available
    }
    rationale = _context_rationale(primary, technical_components, fundamental_result)
    rationale.extend(_unique_rationale([by_timeframe[key] for key in available]))
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
        rationale=rationale[:8],
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
        fundamental_score=round(effective_fundamental or 0.0, 4),
        news_score=round(news_score or 0.0, 4),
        total_score=round(total_score, 4),
        observation_mode=settings.observation_mode(horizon),
        atr=primary.atr,
        factor_scores=factor_scores,
        relevant_indicators=relevant_indicators,
        regime=primary.market_regime,
        market_volatility=primary.market_volatility,
        market_regime_score=primary.market_regime_score,
        relative_strength_score=primary.relative_strength_score,
        relative_strength_label=primary.relative_strength_label,
        volume_score=technical_components.get("volume", 0.0),
        volume_state=primary.volume_state,
        momentum_extreme_score=technical_components.get("momentum_extreme", 0.0),
        fundamental_components=(fundamental_result.components if fundamental_result else {}),
        fundamental_publications=(fundamental_result.publications if fundamental_result else []),
        fundamental_label=(fundamental_result.label if fundamental_result else "нет данных"),
    )


class TradingIdeaGenerator:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        signals: SignalService,
        freshness: DataFreshnessGuard | None = None,
        fundamentals: FundamentalAnalysisService | None = None,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.signals = signals
        self.freshness = freshness
        self.fundamentals = fundamentals

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
        scoring_model = self.settings.horizon_scoring_model(horizon)
        for timeframe in profile.timeframe_weights:
            try:
                if (
                    scoring_model == "contextual"
                    or scoring_model != self.settings.technical_scoring_model
                ):
                    generated = await self.signals.generate(
                        ticker,
                        timeframe,
                        contextual_weights=(
                            profile.technical_component_weights
                            if scoring_model == "contextual"
                            else None
                        ),
                        scoring_model=scoring_model,
                    )
                else:
                    generated = await self.signals.generate(ticker, timeframe)
                generated_signals.append(generated)
            except InsufficientDataError:
                continue
        fundamental = None
        primary_signal = next(
            (
                signal
                for signal in generated_signals
                if signal.timeframe == profile.primary_timeframe
            ),
            None,
        )
        if self.fundamentals is not None and primary_signal is not None:
            fundamental = await self.fundamentals.score_at(ticker, primary_signal.candle_begin)
        candidate = build_trading_idea(
            self.settings,
            instrument_name=instrument.short_name,
            horizon=horizon,
            signals=generated_signals,
            fundamental_result=fundamental,
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
