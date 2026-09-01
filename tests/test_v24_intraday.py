from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.config import Settings
from app.domain import CandleData
from app.execution_v24 import (
    PathToTargetAssessment,
    PathToTargetInput,
    PriceObstacle,
    assess_path_to_target,
    build_entry_plan,
    reassess_execution,
)
from app.intraday_setup import SetupClassifierV24
from app.intraday_technical import analyze_intraday_technical
from app.intraday_v24 import IntradayPipelineV24
from app.market_regime_v24 import MarketRegimeAssessmentV24, analyze_market_regime_v24
from app.model_execution_v24 import (
    ModelExecutionRequest,
    ModelOrderType,
    simulate_model_execution,
)
from app.moex import SOURCE_INTERVALS
from app.risk_v24 import CostEstimate
from app.v24_domain import (
    BreakoutState,
    CostConfigurationStatus,
    EventStateV24,
    ExecutionAction,
    FillStatus,
    FinalClassification,
    JournalDirection,
    MarketBiasV24,
    MarketTrendRegime,
    PathToTargetStatus,
    SetupType,
    StructureState,
    VolatilityStateV24,
    VolumeProfileStatus,
)

MOSCOW = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _intraday_candles(
    *,
    ticker: str = "SBER",
    daily_growth: float = 0.3,
    intrabar_growth: float = 0.02,
    volume_multiplier_last: float = 2.0,
) -> list[CandleData]:
    candles: list[CandleData] = []
    first_day = datetime(2026, 8, 24, 10, 0, tzinfo=MOSCOW)
    price = 100.0
    for day in range(5):
        day_start = first_day + timedelta(days=day)
        price += daily_growth
        for slot in range(16):
            begin = day_start + timedelta(minutes=5 * slot)
            open_price = price
            close = price + intrabar_growth
            volume = 1_000.0
            if day == 4 and slot == 15:
                volume *= volume_multiplier_last
            candles.append(
                CandleData(
                    secid=ticker,
                    board_id="TQBR",
                    timeframe="5m",
                    begin=begin,
                    end=begin + timedelta(minutes=5),
                    open=open_price,
                    high=max(open_price, close) + 0.03,
                    low=min(open_price, close) - 0.03,
                    close=close,
                    volume=volume,
                    value=volume * close,
                )
            )
            price = close
    return candles


def _daily_candles(*, growth: float = 0.006, count: int = 100) -> list[CandleData]:
    candles: list[CandleData] = []
    begin = datetime(2026, 5, 1, 0, 0, tzinfo=MOSCOW)
    price = 100.0
    for index in range(count):
        start = begin + timedelta(days=index)
        close = price * (1 + growth)
        candles.append(
            CandleData(
                secid="IMOEX",
                board_id="SNDX",
                timeframe="1d",
                begin=start,
                end=start + timedelta(hours=23, minutes=59),
                open=price,
                high=max(price, close) * 1.002,
                low=min(price, close) * 0.998,
                close=close,
                volume=1_000_000,
                value=1_000_000 * close,
            )
        )
        price = close
    return candles


def test_intraday_mode_is_isolated_and_one_minute_is_execution_only() -> None:
    settings = Settings(_env_file=None)

    assert settings.intraday_v24_enabled is False
    assert settings.intraday_v24_strategy_version == "intraday_v2_4"
    assert settings.intraday_v24_max_holding_trading_days == 2
    assert settings.intraday_v24_leverage_enabled is False
    assert settings.intraday_v24_timeframe_list == ["1d", "1h", "15m", "5m"]
    assert SOURCE_INTERVALS["1m"] == 1
    assert "1m" not in settings.analysis_timeframe_list


def test_intraday_technical_adds_vwap_levels_rvol_and_no_fake_volume_profile() -> None:
    candles = _intraday_candles()
    benchmark = _intraday_candles(ticker="IMOEX", daily_growth=0.2, intrabar_growth=0.01)
    anchor = candles[-16].begin

    result = analyze_intraday_technical(
        candles,
        timeframe="5m",
        benchmark_candles=benchmark,
        anchors={"session": anchor},
    )

    assert result.as_of.tzinfo is not None
    assert result.session_vwap is not None
    assert result.anchored_vwaps[0].value is not None
    assert result.previous_day_high is not None
    assert result.previous_day_low is not None
    assert result.opening_range_high is not None
    assert result.opening_range_low is not None
    assert result.relative_strength_pct is not None
    assert result.rvol == pytest.approx(2.0)
    assert result.volume_confirmed is True
    assert result.compression_ratio is not None
    assert result.expansion_ratio is not None
    assert result.volume_profile_status is VolumeProfileStatus.DATA_NOT_AVAILABLE


def test_market_regime_v24_has_trend_bias_without_direction_probability() -> None:
    candles = _daily_candles()

    result = analyze_market_regime_v24(
        candles,
        event_state=EventStateV24.NORMAL,
        as_of=candles[-1].end,
    )

    assert result.regime is MarketTrendRegime.STRONG_UPTREND
    assert result.bias is MarketBiasV24.STRONG_LONG
    assert result.allows_direction(JournalDirection.LONG)
    assert not result.allows_direction(JournalDirection.SHORT)
    assert not hasattr(result, "probability")


def _market(regime: MarketTrendRegime) -> MarketRegimeAssessmentV24:
    bias = {
        MarketTrendRegime.STRONG_UPTREND: MarketBiasV24.STRONG_LONG,
        MarketTrendRegime.UPTREND: MarketBiasV24.LONG,
        MarketTrendRegime.RANGE: MarketBiasV24.NEUTRAL,
        MarketTrendRegime.DOWNTREND: MarketBiasV24.SHORT,
        MarketTrendRegime.STRONG_DOWNTREND: MarketBiasV24.STRONG_SHORT,
    }[regime]
    return MarketRegimeAssessmentV24(
        regime=regime,
        volatility=VolatilityStateV24.NORMAL_VOL,
        event_state=EventStateV24.NORMAL,
        bias=bias,
        as_of=NOW,
        ema20=110,
        ema50=100,
        return_20_pct=5,
        realized_volatility=0.01,
        volatility_percentile=50,
        reasons=(),
    )


def test_setup_classifier_is_deterministic_and_blocks_countertrend() -> None:
    base = analyze_intraday_technical(
        _intraday_candles(),
        timeframe="5m",
        benchmark_candles=_intraday_candles(ticker="IMOEX"),
    )
    trend = replace(
        base,
        current_price=110,
        previous_close=109,
        ema20=105,
        ema50=100,
        structure_state=StructureState.HH_HL,
    )
    trigger = replace(
        trend,
        timeframe="15m",
        breakout_state=BreakoutState.RETEST,
        breakout_direction="UP",
        breakout_level=108,
    )
    execution = replace(
        trend,
        timeframe="5m",
        opening_range_high=None,
        opening_range_low=None,
    )
    snapshots = {"1d": trend, "1h": trend, "15m": trigger, "5m": execution}

    detected = SetupClassifierV24().classify(
        snapshots,
        _market(MarketTrendRegime.UPTREND),
    )
    blocked = SetupClassifierV24().classify(
        snapshots,
        _market(MarketTrendRegime.DOWNTREND),
    )

    assert detected.setup_type is SetupType.BREAKOUT_RETEST
    assert detected.direction is JournalDirection.LONG
    assert detected.invalidation is not None
    assert blocked.setup_type is SetupType.UNKNOWN
    assert blocked.direction is None
    assert "Countertrend blocked" in blocked.evidence[0]


def test_intraday_pipeline_excludes_third_echelon_and_gates_unknown_event_context() -> None:
    instrument = _daily_candles()
    benchmark = _daily_candles(growth=0.005)
    frames = {timeframe: instrument for timeframe in ("1d", "1h", "15m", "5m", "1m")}
    markets = {timeframe: benchmark for timeframe in ("1d", "1h", "15m", "5m", "1m")}
    pipeline = IntradayPipelineV24()

    with pytest.raises(ValueError, match="third-echelon"):
        pipeline.analyze(
            echelon=3,
            candles_by_timeframe=frames,
            benchmark_by_timeframe=markets,
            event_state=EventStateV24.NORMAL,
            as_of=instrument[-1].end,
        )

    result = pipeline.analyze(
        echelon=2,
        candles_by_timeframe=frames,
        benchmark_by_timeframe=markets,
        event_state=EventStateV24.DATA_NOT_AVAILABLE,
        as_of=instrument[-1].end,
        execution_1m_quality_pass=False,
    )

    assert result.strategy_version == "intraday_v2_4"
    assert "1m" not in result.snapshots
    assert result.no_trade is True
    assert "EVENT_CONTEXT_DATA_NOT_AVAILABLE" in result.no_trade_reasons


def _clean_path() -> PathToTargetAssessment:
    return assess_path_to_target(
        PathToTargetInput(
            direction=JournalDirection.LONG,
            entry=100,
            target=110,
            vwap=99,
            support_levels=(98,),
            resistance_levels=(),
            previous_day_high=111,
            previous_day_low=95,
            gap_levels=(),
            volume_nodes=(),
            liquidity_obstacles=(),
            opposing_structure=False,
        )
    )


def test_path_to_target_never_hides_missing_or_blocking_evidence() -> None:
    unknown = assess_path_to_target(
        PathToTargetInput(
            direction=JournalDirection.LONG,
            entry=100,
            target=110,
            vwap=None,
            support_levels=(),
            resistance_levels=(),
            previous_day_high=None,
            previous_day_low=None,
            gap_levels=(),
            volume_nodes=None,
            liquidity_obstacles=(),
            opposing_structure=None,
        )
    )
    blocked = assess_path_to_target(
        PathToTargetInput(
            direction=JournalDirection.LONG,
            entry=100,
            target=110,
            vwap=99,
            support_levels=(),
            resistance_levels=(),
            previous_day_high=111,
            previous_day_low=95,
            gap_levels=(),
            volume_nodes=(),
            liquidity_obstacles=(PriceObstacle("THIN_LIQUIDITY", 105, True),),
            opposing_structure=False,
        )
    )

    assert unknown.status is PathToTargetStatus.UNKNOWN
    assert blocked.status is PathToTargetStatus.BLOCKED


def test_execution_reassessment_marks_chased_price_without_mutating_plan() -> None:
    plan = build_entry_plan(
        direction=JournalDirection.LONG,
        current_price=100,
        atr=2,
        preferred_reference=100,
        invalidation=95,
    )
    original = plan
    costs = CostEstimate(
        status=CostConfigurationStatus.CONFIGURED,
        entry_costs_rub=50,
        exit_costs_rub=50,
        stop_slippage_rub=20,
        short_carry_rub=0,
        total_expected_costs_rub=100,
        known_costs_rub=100,
    )

    result = reassess_execution(
        plan=plan,
        current_price=102,
        target=110,
        path=_clean_path(),
        cost_estimate=costs,
        position_amount_rub=100_000,
        assessed_at=NOW,
    )

    assert plan == original
    assert result.gross_rr is not None
    assert result.net_rr is not None
    assert result.classification is FinalClassification.SIGNAL_VALID_EXECUTION_INVALID
    assert result.action is ExecutionAction.NO_CHASE


def test_execution_waits_when_entry_inputs_are_not_reliably_calculable() -> None:
    plan = build_entry_plan(
        direction=JournalDirection.LONG,
        current_price=100,
        atr=None,
        preferred_reference=100,
        invalidation=None,
    )
    costs = CostEstimate(
        status=CostConfigurationStatus.NOT_CONFIGURED,
        entry_costs_rub=None,
        exit_costs_rub=None,
        stop_slippage_rub=None,
        short_carry_rub=None,
        total_expected_costs_rub=None,
        known_costs_rub=0,
    )

    result = reassess_execution(
        plan=plan,
        current_price=100,
        target=110,
        path=_clean_path(),
        cost_estimate=costs,
        position_amount_rub=None,
        assessed_at=NOW,
    )

    assert result.classification is FinalClassification.DATA_INSUFFICIENT
    assert result.action is ExecutionAction.WAIT


def _bar(
    begin: datetime,
    *,
    open_price: float,
    high: float,
    low: float,
    close: float,
    minutes: int = 15,
) -> CandleData:
    return CandleData(
        secid="SBER",
        board_id="TQBR",
        timeframe=f"{minutes}m",
        begin=begin,
        end=begin + timedelta(minutes=minutes),
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=1_000,
        value=100_000,
    )


def test_model_limit_touch_is_uncertain_without_reliable_fill_data() -> None:
    result = simulate_model_execution(
        ModelExecutionRequest(
            direction=JournalDirection.LONG,
            order_type=ModelOrderType.LIMIT,
            entry=100,
            stop=95,
            tp1=105,
            candles=(_bar(NOW, open_price=101, high=102, low=99, close=101),),
            reliable_fill_data=False,
        )
    )

    assert result.fill_status is FillStatus.UNCERTAIN
    assert result.calibration_eligible is False


def test_model_execution_uses_lower_timeframe_and_marks_unresolved_order_ambiguous() -> None:
    parent = _bar(NOW, open_price=100, high=106, low=94, close=101)
    unresolved = simulate_model_execution(
        ModelExecutionRequest(
            JournalDirection.LONG,
            ModelOrderType.LIMIT,
            100,
            95,
            105,
            (parent,),
            reliable_fill_data=True,
        )
    )
    lower = (
        _bar(NOW, open_price=100, high=101, low=99, close=100.5, minutes=5),
        _bar(
            NOW + timedelta(minutes=5),
            open_price=100.5,
            high=105.5,
            low=100,
            close=105,
            minutes=5,
        ),
    )
    resolved = simulate_model_execution(
        ModelExecutionRequest(
            JournalDirection.LONG,
            ModelOrderType.LIMIT,
            100,
            95,
            105,
            (parent,),
            lower_timeframe_candles=lower,
            reliable_fill_data=True,
        )
    )

    assert unresolved.ambiguous_execution is True
    assert unresolved.exit_reason == "AMBIGUOUS_CONSERVATIVE_STOP"
    assert unresolved.calibration_eligible is False
    assert resolved.ambiguous_execution is False
    assert resolved.exit_reason == "TP1"
    assert resolved.calibration_eligible is True


def test_gap_through_stop_exits_at_first_available_price() -> None:
    first = _bar(NOW, open_price=100, high=101, low=99, close=100)
    gap = _bar(
        NOW + timedelta(minutes=15),
        open_price=93,
        high=96,
        low=92,
        close=94,
    )
    result = simulate_model_execution(
        ModelExecutionRequest(
            JournalDirection.LONG,
            ModelOrderType.LIMIT,
            100,
            95,
            105,
            (first, gap),
            reliable_fill_data=True,
        )
    )

    assert result.exit_reason == "GAP_THROUGH_STOP"
    assert result.exit_fill == 93
    assert result.gap_slippage == 2
