from __future__ import annotations

import math
from dataclasses import dataclass

from app.config import Settings
from app.v24_domain import CalculationReliability, ConfigurationStatus


def _positive(value: float | int | None) -> float | None:
    if value is None:
        return None
    normalized = float(value)
    return normalized if math.isfinite(normalized) and normalized > 0 else None


def _nonnegative(value: float | int | None) -> float | None:
    if value is None:
        return None
    normalized = float(value)
    return normalized if math.isfinite(normalized) and normalized >= 0 else None


@dataclass(frozen=True, slots=True)
class LiquidityModelConfig:
    adv_participation_rate: float | None
    session_participation_rate: float | None
    orderbook_participation_rate: float | None
    normal_exit_participation_rate: float | None
    fast_exit_participation_rate: float | None
    stress_exit_participation_rate: float | None
    max_expected_slippage_bps: float | None
    max_market_impact_bps: float | None

    @property
    def status(self) -> ConfigurationStatus:
        values = (
            self.adv_participation_rate,
            self.session_participation_rate,
            self.orderbook_participation_rate,
            self.normal_exit_participation_rate,
            self.fast_exit_participation_rate,
            self.stress_exit_participation_rate,
            self.max_expected_slippage_bps,
            self.max_market_impact_bps,
        )
        return (
            ConfigurationStatus.CONFIGURED
            if all(value is not None for value in values)
            else ConfigurationStatus.NOT_CONFIGURED
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> LiquidityModelConfig:
        return cls(
            adv_participation_rate=settings.liquidity_v2_adv_participation_rate,
            session_participation_rate=settings.liquidity_v2_session_participation_rate,
            orderbook_participation_rate=settings.liquidity_v2_orderbook_participation_rate,
            normal_exit_participation_rate=(settings.liquidity_v2_normal_exit_participation_rate),
            fast_exit_participation_rate=settings.liquidity_v2_fast_exit_participation_rate,
            stress_exit_participation_rate=(settings.liquidity_v2_stress_exit_participation_rate),
            max_expected_slippage_bps=settings.liquidity_v2_max_expected_slippage_bps,
            max_market_impact_bps=settings.liquidity_v2_max_market_impact_bps,
        )


@dataclass(frozen=True, slots=True)
class LiquidityModelInput:
    adv20_rub: float | None
    median_adv_rub: float | None
    current_turnover_rub: float | None
    expected_remaining_turnover_rub: float | None
    rvol: float | None
    spread_pct: float | None
    number_of_trades: int | None
    orderbook_depth_rub: float | None
    orderbook_depth_is_full: bool
    expected_slippage_bps: float | None
    market_impact_bps: float | None
    remaining_session_hours: float | None
    proposed_position_rub: float | None = None


@dataclass(frozen=True, slots=True)
class LiquidityModelAssessment:
    config_status: ConfigurationStatus
    reliability: CalculationReliability
    adv_participation_cap_rub: float | None
    session_participation_cap_rub: float | None
    orderbook_slippage_cap_rub: float | None
    normal_exit_cap_rub: float | None
    fast_exit_cap_rub: float | None
    stress_exit_cap_rub: float | None
    liquidity_cap_rub: float | None
    time_to_exit_25_minutes: float | None
    time_to_exit_50_minutes: float | None
    time_to_exit_75_minutes: float | None
    time_to_exit_100_minutes: float | None
    missing_inputs: tuple[str, ...]
    reasons: tuple[str, ...]


class LiquidityModelV24:
    def __init__(self, config: LiquidityModelConfig) -> None:
        rates = (
            config.adv_participation_rate,
            config.session_participation_rate,
            config.orderbook_participation_rate,
            config.normal_exit_participation_rate,
            config.fast_exit_participation_rate,
            config.stress_exit_participation_rate,
        )
        if any(value is not None and not 0 < value <= 1 for value in rates):
            raise ValueError("Liquidity participation rates must be in (0, 1]")
        if (
            config.normal_exit_participation_rate is not None
            and config.fast_exit_participation_rate is not None
            and config.fast_exit_participation_rate > config.normal_exit_participation_rate
        ):
            raise ValueError("Fast-exit participation cannot exceed normal-exit participation")
        if (
            config.fast_exit_participation_rate is not None
            and config.stress_exit_participation_rate is not None
            and config.stress_exit_participation_rate > config.fast_exit_participation_rate
        ):
            raise ValueError("Stress-exit participation cannot exceed fast-exit participation")
        self.config = config

    def assess(self, inputs: LiquidityModelInput) -> LiquidityModelAssessment:
        if self.config.status is ConfigurationStatus.NOT_CONFIGURED:
            return LiquidityModelAssessment(
                config_status=ConfigurationStatus.NOT_CONFIGURED,
                reliability=CalculationReliability.NOT_RELIABLY_CALCULABLE,
                adv_participation_cap_rub=None,
                session_participation_cap_rub=None,
                orderbook_slippage_cap_rub=None,
                normal_exit_cap_rub=None,
                fast_exit_cap_rub=None,
                stress_exit_cap_rub=None,
                liquidity_cap_rub=None,
                time_to_exit_25_minutes=None,
                time_to_exit_50_minutes=None,
                time_to_exit_75_minutes=None,
                time_to_exit_100_minutes=None,
                missing_inputs=("LIQUIDITY_MODEL_CONFIG",),
                reasons=("LIQUIDITY_MODEL_NOT_CONFIGURED",),
            )
        positive_inputs = {
            "ADV20": inputs.adv20_rub,
            "MEDIAN_ADV": inputs.median_adv_rub,
            "EXPECTED_REMAINING_TURNOVER": inputs.expected_remaining_turnover_rub,
            "RVOL": inputs.rvol,
            "NUMBER_OF_TRADES": inputs.number_of_trades,
        }
        nonnegative_inputs = {
            "CURRENT_TURNOVER": inputs.current_turnover_rub,
            "SPREAD": inputs.spread_pct,
            "EXPECTED_SLIPPAGE": inputs.expected_slippage_bps,
            "MARKET_IMPACT": inputs.market_impact_bps,
        }
        missing = [name for name, value in positive_inputs.items() if _positive(value) is None]
        missing.extend(
            name for name, value in nonnegative_inputs.items() if _nonnegative(value) is None
        )
        if not inputs.orderbook_depth_is_full or _positive(inputs.orderbook_depth_rub) is None:
            missing.append("FULL_ORDERBOOK_DEPTH")
        if missing:
            return LiquidityModelAssessment(
                config_status=ConfigurationStatus.CONFIGURED,
                reliability=CalculationReliability.NOT_RELIABLY_CALCULABLE,
                adv_participation_cap_rub=None,
                session_participation_cap_rub=None,
                orderbook_slippage_cap_rub=None,
                normal_exit_cap_rub=None,
                fast_exit_cap_rub=None,
                stress_exit_cap_rub=None,
                liquidity_cap_rub=None,
                time_to_exit_25_minutes=None,
                time_to_exit_50_minutes=None,
                time_to_exit_75_minutes=None,
                time_to_exit_100_minutes=None,
                missing_inputs=tuple(sorted(set(missing))),
                reasons=("DEPTH_DEPENDENT_CAP_NOT_RELIABLY_CALCULABLE",),
            )

        config = self.config
        adv20 = _positive(inputs.adv20_rub)
        median_adv = _positive(inputs.median_adv_rub)
        remaining = _positive(inputs.expected_remaining_turnover_rub)
        depth = _positive(inputs.orderbook_depth_rub)
        assert adv20 is not None and median_adv is not None and remaining is not None
        assert depth is not None
        assert config.adv_participation_rate is not None
        assert config.session_participation_rate is not None
        assert config.orderbook_participation_rate is not None
        assert config.normal_exit_participation_rate is not None
        assert config.fast_exit_participation_rate is not None
        assert config.stress_exit_participation_rate is not None
        assert config.max_expected_slippage_bps is not None
        assert config.max_market_impact_bps is not None

        adv_cap = min(adv20, median_adv) * config.adv_participation_rate
        session_cap = remaining * config.session_participation_rate
        orderbook_cap = depth * config.orderbook_participation_rate
        normal_exit_cap = remaining * config.normal_exit_participation_rate
        fast_exit_cap = remaining * config.fast_exit_participation_rate
        stress_exit_cap = min(
            remaining * config.stress_exit_participation_rate,
            orderbook_cap,
        )
        reasons: list[str] = []
        if inputs.expected_slippage_bps > config.max_expected_slippage_bps:
            reasons.append("EXPECTED_SLIPPAGE_LIMIT_EXCEEDED")
        if inputs.market_impact_bps > config.max_market_impact_bps:
            reasons.append("MARKET_IMPACT_LIMIT_EXCEEDED")
        liquidity_cap = (
            0.0 if reasons else min(adv_cap, session_cap, orderbook_cap, stress_exit_cap)
        )

        times: list[float | None] = [None, None, None, None]
        proposed = _positive(inputs.proposed_position_rub)
        remaining_hours = _positive(inputs.remaining_session_hours)
        if proposed is not None and remaining_hours is not None:
            normal_flow_per_hour = (
                remaining / remaining_hours * config.normal_exit_participation_rate
            )
            if normal_flow_per_hour > 0:
                times = [
                    proposed * share / normal_flow_per_hour * 60
                    for share in (0.25, 0.50, 0.75, 1.00)
                ]
        return LiquidityModelAssessment(
            config_status=ConfigurationStatus.CONFIGURED,
            reliability=CalculationReliability.RELIABLE,
            adv_participation_cap_rub=adv_cap,
            session_participation_cap_rub=session_cap,
            orderbook_slippage_cap_rub=orderbook_cap,
            normal_exit_cap_rub=normal_exit_cap,
            fast_exit_cap_rub=fast_exit_cap,
            stress_exit_cap_rub=stress_exit_cap,
            liquidity_cap_rub=liquidity_cap,
            time_to_exit_25_minutes=times[0],
            time_to_exit_50_minutes=times[1],
            time_to_exit_75_minutes=times[2],
            time_to_exit_100_minutes=times[3],
            missing_inputs=(),
            reasons=tuple(reasons),
        )
