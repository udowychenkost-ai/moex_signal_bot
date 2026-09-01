from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.intraday_technical import IntradayTechnicalSnapshot
from app.market_regime_v24 import MarketRegimeAssessmentV24
from app.v24_domain import (
    BreakoutState,
    JournalDirection,
    SetupType,
    StructureState,
)

REQUIRED_INTRADAY_TIMEFRAMES = ("1d", "1h", "15m", "5m")


@dataclass(frozen=True, slots=True)
class SetupClassifierConfigV24:
    version: str = "intraday_v2_4_setup_1"
    pullback_atr_distance: float = 0.35
    level_atr_distance: float = 0.30
    compression_ratio_max: float = 0.75
    expansion_ratio_min: float = 1.25
    relative_strength_min_pct: float = 0.50

    def __post_init__(self) -> None:
        if (
            min(
                self.pullback_atr_distance,
                self.level_atr_distance,
                self.compression_ratio_max,
                self.expansion_ratio_min,
                self.relative_strength_min_pct,
            )
            <= 0
        ):
            raise ValueError("Setup classifier thresholds must be positive")


@dataclass(frozen=True, slots=True)
class SetupDetection:
    setup_type: SetupType
    direction: JournalDirection | None
    detected_at: datetime
    evidence: tuple[str, ...]
    invalidation: float | None
    relevant_timeframes: tuple[str, ...]


def _trend_direction(snapshot: IntradayTechnicalSnapshot) -> JournalDirection | None:
    if snapshot.current_price > snapshot.ema20 > snapshot.ema50:
        return JournalDirection.LONG
    if snapshot.current_price < snapshot.ema20 < snapshot.ema50:
        return JournalDirection.SHORT
    if snapshot.structure_state is StructureState.HH_HL:
        return JournalDirection.LONG
    if snapshot.structure_state is StructureState.LH_LL:
        return JournalDirection.SHORT
    return None


def _near(value: float | None, target: float | None, distance: float | None) -> bool:
    return bool(
        value is not None
        and target is not None
        and distance is not None
        and abs(value - target) <= distance
    )


def _invalidation(
    direction: JournalDirection,
    snapshot: IntradayTechnicalSnapshot,
) -> float | None:
    if direction is JournalDirection.LONG:
        candidates = [
            value
            for value in (
                snapshot.previous_day_low,
                snapshot.opening_range_low,
                snapshot.session_vwap,
                *snapshot.support_levels,
            )
            if value is not None and value < snapshot.current_price
        ]
        return max(candidates) if candidates else None
    candidates = [
        value
        for value in (
            snapshot.previous_day_high,
            snapshot.opening_range_high,
            snapshot.session_vwap,
            *snapshot.resistance_levels,
        )
        if value is not None and value > snapshot.current_price
    ]
    return min(candidates) if candidates else None


class SetupClassifierV24:
    def __init__(self, config: SetupClassifierConfigV24 | None = None) -> None:
        self.config = config or SetupClassifierConfigV24()

    def classify(
        self,
        snapshots: dict[str, IntradayTechnicalSnapshot],
        market: MarketRegimeAssessmentV24,
    ) -> SetupDetection:
        missing = set(REQUIRED_INTRADAY_TIMEFRAMES) - snapshots.keys()
        if missing:
            raise ValueError("Missing required intraday timeframes: " + ", ".join(sorted(missing)))
        daily = snapshots["1d"]
        hourly = snapshots["1h"]
        trigger = snapshots["15m"]
        execution = snapshots["5m"]
        detected_at = execution.as_of
        daily_direction = _trend_direction(daily)
        hourly_direction = _trend_direction(hourly)
        if daily_direction is None or daily_direction is not hourly_direction:
            return SetupDetection(
                setup_type=SetupType.UNKNOWN,
                direction=None,
                detected_at=detected_at,
                evidence=("D1/H1 trend is not aligned",),
                invalidation=None,
                relevant_timeframes=REQUIRED_INTRADAY_TIMEFRAMES,
            )
        direction = daily_direction
        if not market.allows_direction(direction):
            return SetupDetection(
                setup_type=SetupType.UNKNOWN,
                direction=None,
                detected_at=detected_at,
                evidence=(
                    "Countertrend blocked: "
                    f"market={market.regime.value} direction={direction.value}",
                ),
                invalidation=None,
                relevant_timeframes=REQUIRED_INTRADAY_TIMEFRAMES,
            )

        is_long = direction is JournalDirection.LONG
        expected_breakout = "UP" if is_long else "DOWN"
        evidence = [
            f"D1 and H1 trend aligned {direction.value}",
            f"market regime {market.regime.value}",
        ]
        setup = SetupType.UNKNOWN
        relevant = ("1d", "1h", "15m", "5m")

        opening_break = (
            execution.opening_range_high is not None
            and execution.current_price > execution.opening_range_high
            if is_long
            else execution.opening_range_low is not None
            and execution.current_price < execution.opening_range_low
        )
        if opening_break and execution.volume_confirmed is True:
            setup = SetupType.OPENING_RANGE_BREAKOUT
            evidence.extend(("opening range broken", "RVOL confirms execution"))
            relevant = ("1h", "15m", "5m")
        elif (
            trigger.breakout_state is BreakoutState.RETEST
            and trigger.breakout_direction == expected_breakout
        ):
            setup = SetupType.BREAKOUT_RETEST
            evidence.append(f"15m retest of {trigger.breakout_level}")
            relevant = ("1h", "15m", "5m")
        elif (
            trigger.compression_ratio is not None
            and trigger.compression_ratio <= self.config.compression_ratio_max
            and execution.expansion_ratio is not None
            and execution.expansion_ratio >= self.config.expansion_ratio_min
            and execution.volume_confirmed is True
        ):
            setup = SetupType.COMPRESSION_EXPANSION
            evidence.extend(("15m compression", "5m range/volume expansion"))
            relevant = ("15m", "5m")
        else:
            atr_distance = (
                trigger.atr * self.config.pullback_atr_distance if trigger.atr is not None else None
            )
            pullback_reference = trigger.session_vwap or trigger.ema20
            pullback = _near(trigger.current_price, pullback_reference, atr_distance)
            resumed = (
                execution.current_price > execution.previous_close
                if is_long
                else execution.current_price < execution.previous_close
            )
            if pullback and resumed:
                setup = SetupType.TREND_PULLBACK
                evidence.extend(("15m pullback to VWAP/EMA20", "5m trend resumption"))
                relevant = ("1d", "1h", "15m", "5m")
            else:
                vwap_cross = trigger.session_vwap is not None and (
                    trigger.previous_close <= trigger.session_vwap < trigger.current_price
                    if is_long
                    else trigger.previous_close >= trigger.session_vwap > trigger.current_price
                )
                if vwap_cross:
                    setup = SetupType.VWAP_RECLAIM if is_long else SetupType.VWAP_REJECT
                    evidence.append("15m price crossed session VWAP in trend direction")
                    relevant = ("15m", "5m")
                elif (
                    execution.expansion_ratio is not None
                    and execution.expansion_ratio >= self.config.expansion_ratio_min
                    and execution.volume_confirmed is True
                    and _trend_direction(trigger) is direction
                ):
                    setup = SetupType.MOMENTUM_CONTINUATION
                    evidence.extend(("15m trend aligned", "5m momentum and RVOL expansion"))
                    relevant = ("1h", "15m", "5m")
                else:
                    level_distance = (
                        execution.atr * self.config.level_atr_distance
                        if execution.atr is not None
                        else None
                    )
                    directional_level = (
                        execution.previous_day_low if is_long else execution.previous_day_high
                    )
                    rejection = (
                        execution.current_price > execution.previous_close
                        if is_long
                        else execution.current_price < execution.previous_close
                    )
                    if (
                        _near(execution.current_price, directional_level, level_distance)
                        and rejection
                    ):
                        setup = (
                            SetupType.SUPPORT_BOUNCE if is_long else SetupType.RESISTANCE_REJECTION
                        )
                        evidence.append("5m rejection at previous-day level")
                        relevant = ("15m", "5m")
                    elif trigger.relative_strength_pct is not None and (
                        trigger.relative_strength_pct >= self.config.relative_strength_min_pct
                        if is_long
                        else trigger.relative_strength_pct <= -self.config.relative_strength_min_pct
                    ):
                        setup = (
                            SetupType.RELATIVE_STRENGTH if is_long else SetupType.RELATIVE_WEAKNESS
                        )
                        evidence.append(
                            f"relative performance {trigger.relative_strength_pct:.2f}% vs IMOEX"
                        )
                        relevant = ("1d", "1h", "15m")

        if setup is SetupType.UNKNOWN:
            evidence.append("No deterministic v2.4 setup matched")
            direction_result: JournalDirection | None = None
        else:
            direction_result = direction
        return SetupDetection(
            setup_type=setup,
            direction=direction_result,
            detected_at=detected_at,
            evidence=tuple(evidence),
            invalidation=_invalidation(direction, trigger) if direction_result else None,
            relevant_timeframes=relevant,
        )
