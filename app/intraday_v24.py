from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from app.intraday_setup import REQUIRED_INTRADAY_TIMEFRAMES, SetupClassifierV24, SetupDetection
from app.intraday_technical import (
    IntradayTechnicalConfig,
    IntradayTechnicalSnapshot,
    analyze_intraday_technical,
)
from app.market_regime_v24 import (
    MarketRegimeAssessmentV24,
    MarketRegimeConfigV24,
    analyze_market_regime_v24,
)
from app.v24_domain import STRATEGY_VERSION_V24, EventStateV24, SetupType


@dataclass(frozen=True, slots=True)
class IntradayPipelineConfigV24:
    strategy_version: str = STRATEGY_VERSION_V24
    maximum_holding_trading_days: int = 2
    leverage_enabled: bool = False
    allowed_echelons: tuple[int, ...] = (1, 2)

    def __post_init__(self) -> None:
        if not 1 <= self.maximum_holding_trading_days <= 2:
            raise ValueError("V2.4 intraday holding period cannot exceed two trading days")
        if not self.allowed_echelons or any(value not in {1, 2} for value in self.allowed_echelons):
            raise ValueError("V2.4 intraday universe is restricted to echelons 1 and 2")


@dataclass(frozen=True, slots=True)
class IntradayPipelineResultV24:
    strategy_version: str
    snapshots: dict[str, IntradayTechnicalSnapshot]
    market: MarketRegimeAssessmentV24
    setup: SetupDetection
    no_trade: bool
    no_trade_reasons: tuple[str, ...]
    maximum_holding_trading_days: int
    leverage_enabled: bool


class IntradayPipelineV24:
    def __init__(
        self,
        config: IntradayPipelineConfigV24 | None = None,
        *,
        technical_config: IntradayTechnicalConfig | None = None,
        regime_config: MarketRegimeConfigV24 | None = None,
        setup_classifier: SetupClassifierV24 | None = None,
    ) -> None:
        self.config = config or IntradayPipelineConfigV24()
        self.technical_config = technical_config or IntradayTechnicalConfig()
        self.regime_config = regime_config or MarketRegimeConfigV24()
        self.setup_classifier = setup_classifier or SetupClassifierV24()

    def analyze(
        self,
        *,
        echelon: int,
        candles_by_timeframe: Mapping[str, list[object]],
        benchmark_by_timeframe: Mapping[str, list[object]],
        event_state: EventStateV24,
        as_of: datetime,
        anchors_by_timeframe: Mapping[str, Mapping[str, datetime]] | None = None,
        execution_1m_quality_pass: bool = False,
    ) -> IntradayPipelineResultV24:
        if echelon not in self.config.allowed_echelons:
            raise ValueError("V2.4 intraday excludes third-echelon or unclassified instruments")
        missing = set(REQUIRED_INTRADAY_TIMEFRAMES) - candles_by_timeframe.keys()
        missing_benchmark = set(REQUIRED_INTRADAY_TIMEFRAMES) - benchmark_by_timeframe.keys()
        if missing or missing_benchmark:
            details = []
            if missing:
                details.append("instrument=" + ",".join(sorted(missing)))
            if missing_benchmark:
                details.append("benchmark=" + ",".join(sorted(missing_benchmark)))
            raise ValueError("Missing MTF data: " + " ".join(details))

        selected_timeframes = list(REQUIRED_INTRADAY_TIMEFRAMES)
        if "1w" in candles_by_timeframe and "1w" in benchmark_by_timeframe:
            selected_timeframes.append("1w")
        if (
            execution_1m_quality_pass
            and "1m" in candles_by_timeframe
            and "1m" in benchmark_by_timeframe
        ):
            selected_timeframes.append("1m")
        anchors = anchors_by_timeframe or {}
        snapshots = {
            timeframe: analyze_intraday_technical(
                candles_by_timeframe[timeframe],
                timeframe=timeframe,
                benchmark_candles=benchmark_by_timeframe[timeframe],
                anchors=anchors.get(timeframe),
                config=self.technical_config,
            )
            for timeframe in selected_timeframes
        }
        market = analyze_market_regime_v24(
            benchmark_by_timeframe["1d"],
            event_state=event_state,
            as_of=as_of,
            config=self.regime_config,
        )
        setup = self.setup_classifier.classify(snapshots, market)
        reasons: list[str] = []
        if setup.setup_type is SetupType.UNKNOWN or setup.direction is None:
            reasons.extend(setup.evidence[-1:])
        if self.config.leverage_enabled:
            reasons.append("LEVERAGE_ENABLED_IS_NOT_ALLOWED_FOR_PRODUCTION_V2_4")
        if event_state is EventStateV24.DATA_NOT_AVAILABLE:
            reasons.append("EVENT_CONTEXT_DATA_NOT_AVAILABLE")
        return IntradayPipelineResultV24(
            strategy_version=self.config.strategy_version,
            snapshots=snapshots,
            market=market,
            setup=setup,
            no_trade=bool(reasons),
            no_trade_reasons=tuple(reasons),
            maximum_holding_trading_days=self.config.maximum_holding_trading_days,
            leverage_enabled=self.config.leverage_enabled,
        )
