from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from statistics import fmean

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    ActualTradeJournal,
    IdeaJournal,
    ModelTradeJournal,
    TradeEventJournal,
)
from app.v24_domain import (
    DegradationStatus,
    RegimePerformanceStatus,
    SetupLifecycleStatus,
    TradeEventType,
)


@dataclass(frozen=True, slots=True)
class TradeObservation:
    trade_id: str
    source: str
    strategy_version: str
    closed_at: datetime
    direction: str
    setup: str | None
    market_regime: str | None
    trend: str | None
    volatility: str | None
    time_of_day: str | None
    rr_bucket: str | None
    liquidity_state: str | None
    context: str | None
    calibration_group: str | None
    calibration_eligible: bool
    sample_type: str | None = None
    win: int | None = None
    tp_before_sl: int | None = None
    result_r: float | None = None
    gross_pl_rub: float | None = None
    net_pl_rub: float | None = None
    mfe_pct: float | None = None
    mae_pct: float | None = None
    capture_ratio: float | None = None
    holding_hours: float | None = None
    stated_probability: float | None = None
    brier_score: float | None = None
    execution_quality: float | None = None
    entry_slippage_bps: float | None = None
    entry_price: float | None = None
    exit_price: float | None = None
    false_breakout: bool | None = None


@dataclass(frozen=True, slots=True)
class ReliabilityBucket:
    lower: float
    upper: float
    count: int
    average_probability: float
    observed_rate: float


@dataclass(frozen=True, slots=True)
class PerformanceMetrics:
    n_trades: int
    n_with_win_result: int
    n_with_tp_sequence: int
    n_with_probability: int
    win_rate: float | None
    tp_before_sl_rate: float | None
    average_win_r: float | None
    average_loss_r: float | None
    average_r: float | None
    expectancy_r: float | None
    profit_factor: float | None
    net_pl_rub: float | None
    max_drawdown_rub: float | None
    max_drawdown_r: float | None
    average_mfe_pct: float | None
    average_mae_pct: float | None
    capture_ratio: float | None
    average_holding_hours: float | None
    average_brier_score: float | None
    ece: float | None
    reliability_buckets: tuple[ReliabilityBucket, ...]
    average_execution_quality: float | None


@dataclass(frozen=True, slots=True)
class SetupStatistics:
    setup: str
    metrics: PerformanceMetrics
    false_breakout_rate: float | None
    average_execution_slippage_bps: float | None
    status: SetupLifecycleStatus


@dataclass(frozen=True, slots=True)
class RegimeStatistics:
    key: tuple[str, ...]
    metrics: PerformanceMetrics
    status: RegimePerformanceStatus


@dataclass(frozen=True, slots=True)
class DegradationPolicy:
    min_recent_trades: int | None
    min_history_trades: int | None
    expectancy_drop_r: float | None
    win_rate_drop: float | None


@dataclass(frozen=True, slots=True)
class DegradationAssessment:
    status: DegradationStatus
    recent_n: int
    history_n: int
    recent_expectancy_r: float | None
    history_expectancy_r: float | None
    recent_win_rate: float | None
    history_win_rate: float | None
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ExecutionComparison:
    paired_trades: int
    average_entry_slippage_bps: float | None
    average_exit_slippage_bps: float | None
    model_vs_actual_pl_gap_rub: float | None


def _average(values: list[float]) -> float | None:
    return fmean(values) if values else None


def _rate(values: list[int]) -> float | None:
    return sum(values) / len(values) if values else None


def _drawdown(values: list[float]) -> float:
    peak = 0.0
    equity = 0.0
    maximum = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        maximum = max(maximum, peak - equity)
    return maximum


def reliability_metrics(
    observations: list[TradeObservation],
    *,
    bucket_count: int = 10,
) -> tuple[float | None, float | None, tuple[ReliabilityBucket, ...]]:
    eligible: list[tuple[float, int, float]] = []
    for item in observations:
        if (
            not item.calibration_eligible
            or item.stated_probability is None
            or item.tp_before_sl not in {0, 1}
            or not 0 <= item.stated_probability <= 1
        ):
            continue
        brier = (item.stated_probability - item.tp_before_sl) ** 2
        eligible.append((item.stated_probability, item.tp_before_sl, brier))
    if not eligible:
        return None, None, ()
    buckets: list[ReliabilityBucket] = []
    for index in range(bucket_count):
        lower = index / bucket_count
        upper = (index + 1) / bucket_count
        values = [
            item
            for item in eligible
            if lower <= item[0] < upper or (index == bucket_count - 1 and item[0] == 1)
        ]
        if not values:
            continue
        buckets.append(
            ReliabilityBucket(
                lower=lower,
                upper=upper,
                count=len(values),
                average_probability=fmean(value[0] for value in values),
                observed_rate=fmean(value[1] for value in values),
            )
        )
    ece = sum(
        bucket.count / len(eligible) * abs(bucket.average_probability - bucket.observed_rate)
        for bucket in buckets
    )
    return fmean(value[2] for value in eligible), ece, tuple(buckets)


def calculate_performance(observations: list[TradeObservation]) -> PerformanceMetrics:
    ordered = sorted(observations, key=lambda item: item.closed_at)
    win_values = [item.win for item in ordered if item.win in {0, 1}]
    tp_values = [item.tp_before_sl for item in ordered if item.tp_before_sl in {0, 1}]
    r_values = [item.result_r for item in ordered if item.result_r is not None]
    winning_r = [value for value in r_values if value > 0]
    losing_r = [value for value in r_values if value < 0]
    net_values = [item.net_pl_rub for item in ordered if item.net_pl_rub is not None]
    complete_net = len(net_values) == len(ordered) and bool(ordered)
    complete_r = len(r_values) == len(ordered) and bool(ordered)
    profit_factor = None
    if complete_net:
        gross_profit = sum(value for value in net_values if value > 0)
        gross_loss = abs(sum(value for value in net_values if value < 0))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else None
    average_brier, ece, buckets = reliability_metrics(ordered)
    return PerformanceMetrics(
        n_trades=len(ordered),
        n_with_win_result=len(win_values),
        n_with_tp_sequence=len(tp_values),
        n_with_probability=sum(bucket.count for bucket in buckets),
        win_rate=_rate(win_values),
        tp_before_sl_rate=_rate(tp_values),
        average_win_r=_average(winning_r),
        average_loss_r=_average(losing_r),
        average_r=_average(r_values),
        expectancy_r=_average(r_values),
        profit_factor=profit_factor,
        net_pl_rub=sum(net_values) if complete_net else None,
        max_drawdown_rub=_drawdown(net_values) if complete_net else None,
        max_drawdown_r=_drawdown(r_values) if complete_r else None,
        average_mfe_pct=_average([item.mfe_pct for item in ordered if item.mfe_pct is not None]),
        average_mae_pct=_average([item.mae_pct for item in ordered if item.mae_pct is not None]),
        capture_ratio=_average(
            [item.capture_ratio for item in ordered if item.capture_ratio is not None]
        ),
        average_holding_hours=_average(
            [item.holding_hours for item in ordered if item.holding_hours is not None]
        ),
        average_brier_score=average_brier,
        ece=ece,
        reliability_buckets=buckets,
        average_execution_quality=_average(
            [item.execution_quality for item in ordered if item.execution_quality is not None]
        ),
    )


def rolling_statistics(
    observations: list[TradeObservation],
) -> dict[str, PerformanceMetrics]:
    ordered = sorted(observations, key=lambda item: item.closed_at)
    return {
        "LAST_20": calculate_performance(ordered[-20:]),
        "LAST_50": calculate_performance(ordered[-50:]),
        "LAST_100": calculate_performance(ordered[-100:]),
        "ALL_HISTORY": calculate_performance(ordered),
    }


def setup_statistics(
    observations: list[TradeObservation],
    *,
    minimum_sample: int | None,
    degraded_setups: set[str] | None = None,
) -> tuple[SetupStatistics, ...]:
    grouped: dict[str, list[TradeObservation]] = defaultdict(list)
    for item in observations:
        grouped[item.setup or "UNKNOWN"].append(item)
    degraded = degraded_setups or set()
    result: list[SetupStatistics] = []
    for setup, values in sorted(grouped.items()):
        if setup in degraded:
            status = SetupLifecycleStatus.DEGRADED
        elif minimum_sample is None:
            status = SetupLifecycleStatus.SHADOW_ONLY
        elif len(values) < minimum_sample:
            status = SetupLifecycleStatus.WATCH
        else:
            status = SetupLifecycleStatus.ACTIVE
        false_breakouts = [
            int(item.false_breakout) for item in values if item.false_breakout is not None
        ]
        result.append(
            SetupStatistics(
                setup=setup,
                metrics=calculate_performance(values),
                false_breakout_rate=_rate(false_breakouts),
                average_execution_slippage_bps=_average(
                    [
                        item.entry_slippage_bps
                        for item in values
                        if item.entry_slippage_bps is not None
                    ]
                ),
                status=status,
            )
        )
    return tuple(result)


def regime_statistics(
    observations: list[TradeObservation],
    *,
    minimum_sample: int | None,
) -> tuple[RegimeStatistics, ...]:
    grouped: dict[tuple[str, ...], list[TradeObservation]] = defaultdict(list)
    for item in observations:
        key = (
            item.setup or "UNKNOWN",
            item.direction,
            item.market_regime or "UNKNOWN",
            item.volatility or "UNKNOWN",
            item.time_of_day or "UNKNOWN",
            item.liquidity_state or "UNKNOWN",
        )
        grouped[key].append(item)
    result: list[RegimeStatistics] = []
    for key, values in sorted(grouped.items()):
        complete_key = "UNKNOWN" not in key
        available = complete_key and minimum_sample is not None and len(values) >= minimum_sample
        result.append(
            RegimeStatistics(
                key=key,
                metrics=calculate_performance(values),
                status=(
                    RegimePerformanceStatus.AVAILABLE
                    if available
                    else RegimePerformanceStatus.REGIME_PERFORMANCE_UNKNOWN
                ),
            )
        )
    return tuple(result)


class ModelDegradationDetector:
    def assess(
        self,
        observations: list[TradeObservation],
        policy: DegradationPolicy,
    ) -> DegradationAssessment:
        configured = all(
            value is not None
            for value in (
                policy.min_recent_trades,
                policy.min_history_trades,
                policy.expectancy_drop_r,
                policy.win_rate_drop,
            )
        )
        if not configured:
            return DegradationAssessment(
                DegradationStatus.NOT_CONFIGURED,
                0,
                0,
                None,
                None,
                None,
                None,
                ("DEGRADATION_THRESHOLDS_NOT_CONFIGURED",),
            )
        recent_n = int(policy.min_recent_trades or 0)
        history_n = int(policy.min_history_trades or 0)
        ordered = sorted(observations, key=lambda item: item.closed_at)
        if len(ordered) < recent_n + history_n:
            return DegradationAssessment(
                DegradationStatus.INSUFFICIENT_DATA,
                min(len(ordered), recent_n),
                max(0, len(ordered) - recent_n),
                None,
                None,
                None,
                None,
                ("INSUFFICIENT_RECENT_OR_HISTORY_SAMPLE",),
            )
        recent = ordered[-recent_n:]
        history = ordered[:-recent_n]
        recent_metrics = calculate_performance(recent)
        history_metrics = calculate_performance(history)
        if any(
            value is None
            for value in (
                recent_metrics.expectancy_r,
                history_metrics.expectancy_r,
                recent_metrics.win_rate,
                history_metrics.win_rate,
            )
        ):
            return DegradationAssessment(
                DegradationStatus.INSUFFICIENT_DATA,
                len(recent),
                len(history),
                recent_metrics.expectancy_r,
                history_metrics.expectancy_r,
                recent_metrics.win_rate,
                history_metrics.win_rate,
                ("RESULT_FIELDS_INCOMPLETE",),
            )
        expectancy_drop = history_metrics.expectancy_r - recent_metrics.expectancy_r
        win_rate_drop = history_metrics.win_rate - recent_metrics.win_rate
        reasons: list[str] = []
        if expectancy_drop >= float(policy.expectancy_drop_r):
            reasons.append("EXPECTANCY_DROP")
        if win_rate_drop >= float(policy.win_rate_drop):
            reasons.append("WIN_RATE_DROP")
        return DegradationAssessment(
            (DegradationStatus.MODEL_CONFIDENCE_REDUCED if reasons else DegradationStatus.STABLE),
            len(recent),
            len(history),
            recent_metrics.expectancy_r,
            history_metrics.expectancy_r,
            recent_metrics.win_rate,
            history_metrics.win_rate,
            tuple(reasons),
        )


def compare_model_actual_execution(
    model: list[TradeObservation],
    actual: list[TradeObservation],
) -> ExecutionComparison:
    model_by_trade = {item.trade_id: item for item in model}
    pairs = [
        (model_by_trade[item.trade_id], item) for item in actual if item.trade_id in model_by_trade
    ]
    entry_slippage: list[float] = []
    exit_slippage: list[float] = []
    pl_gaps: list[float] = []
    for model_item, actual_item in pairs:
        if actual_item.entry_slippage_bps is not None:
            entry_slippage.append(actual_item.entry_slippage_bps)
        elif (
            model_item.entry_price is not None
            and actual_item.entry_price is not None
            and model_item.entry_price > 0
        ):
            multiplier = 1 if actual_item.direction == "LONG" else -1
            entry_slippage.append(
                multiplier
                * (actual_item.entry_price - model_item.entry_price)
                / model_item.entry_price
                * 10_000
            )
        if (
            model_item.exit_price is not None
            and actual_item.exit_price is not None
            and model_item.exit_price > 0
        ):
            multiplier = -1 if actual_item.direction == "LONG" else 1
            exit_slippage.append(
                multiplier
                * (actual_item.exit_price - model_item.exit_price)
                / model_item.exit_price
                * 10_000
            )
        if model_item.net_pl_rub is not None and actual_item.net_pl_rub is not None:
            pl_gaps.append(actual_item.net_pl_rub - model_item.net_pl_rub)
    return ExecutionComparison(
        paired_trades=len(pairs),
        average_entry_slippage_bps=_average(entry_slippage),
        average_exit_slippage_bps=_average(exit_slippage),
        model_vs_actual_pl_gap_rub=_average(pl_gaps),
    )


class V24StatisticsService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def model_observations(
        self,
        *,
        strategy_version: str | None = None,
        calibration_group: str | None = None,
    ) -> list[TradeObservation]:
        async with self.session_factory() as session:
            query = (
                select(ModelTradeJournal, IdeaJournal)
                .join(IdeaJournal, IdeaJournal.trade_id == ModelTradeJournal.trade_id)
                .where(
                    or_(
                        ModelTradeJournal.final_exit_time.is_not(None),
                        ModelTradeJournal.result_r.is_not(None),
                        ModelTradeJournal.tp_before_sl_1_0.is_not(None),
                    )
                )
            )
            if strategy_version is not None:
                query = query.where(ModelTradeJournal.strategy_version == strategy_version)
            if calibration_group is not None:
                query = query.where(ModelTradeJournal.calibration_group == calibration_group)
            rows = (await session.execute(query)).all()
        observations: list[TradeObservation] = []
        for model, idea in rows:
            closed_at = model.final_exit_time or model.model_entry_time or idea.signal_datetime
            observations.append(
                TradeObservation(
                    trade_id=model.trade_id,
                    source="MODEL",
                    strategy_version=model.strategy_version,
                    closed_at=closed_at,
                    direction=model.calibration_direction or idea.direction,
                    setup=model.calibration_setup or idea.setup,
                    market_regime=model.calibration_market_regime or idea.market_regime,
                    trend=model.calibration_trend,
                    volatility=model.calibration_volatility,
                    time_of_day=model.calibration_time_of_day,
                    rr_bucket=model.calibration_rr_bucket,
                    liquidity_state=model.calibration_liquidity_state,
                    context=model.calibration_context,
                    calibration_group=model.calibration_group,
                    calibration_eligible=model.calibration_eligible,
                    sample_type=model.sample_type,
                    win=model.win_1_0,
                    tp_before_sl=model.tp_before_sl_1_0,
                    result_r=model.result_r,
                    gross_pl_rub=model.gross_pl_rub,
                    net_pl_rub=model.net_pl_rub,
                    mfe_pct=model.mfe_pct,
                    mae_pct=model.mae_pct,
                    capture_ratio=model.capture_ratio,
                    holding_hours=model.holding_hours,
                    stated_probability=model.stated_probability,
                    brier_score=model.brier_score,
                    execution_quality=model.execution_quality_at_entry,
                    entry_price=model.model_entry,
                    exit_price=model.final_exit,
                )
            )
        return observations

    async def actual_observations(
        self,
        *,
        strategy_version: str | None = None,
    ) -> list[TradeObservation]:
        async with self.session_factory() as session:
            query = select(ActualTradeJournal, IdeaJournal).join(
                IdeaJournal,
                IdeaJournal.trade_id == ActualTradeJournal.trade_id,
            )
            if strategy_version is not None:
                query = query.where(ActualTradeJournal.strategy_version == strategy_version)
            rows = (await session.execute(query)).all()
            events = list(
                (
                    await session.execute(
                        select(TradeEventJournal)
                        .where(TradeEventJournal.actual_trade_id.is_not(None))
                        .order_by(TradeEventJournal.event_datetime, TradeEventJournal.event_id)
                    )
                ).scalars()
            )
            model_ids = [actual.model_trade_id for actual, _ in rows if actual.model_trade_id]
            models = (
                list(
                    (
                        await session.execute(
                            select(ModelTradeJournal).where(
                                ModelTradeJournal.model_trade_id.in_(model_ids)
                            )
                        )
                    ).scalars()
                )
                if model_ids
                else []
            )
        event_map: dict[str, list[TradeEventJournal]] = defaultdict(list)
        for event in events:
            if event.actual_trade_id is not None:
                event_map[event.actual_trade_id].append(event)
        model_map = {item.model_trade_id: item for item in models}
        observations: list[TradeObservation] = []
        for actual, idea in rows:
            derived = self._actual_result(actual, event_map[actual.actual_trade_id])
            if derived is None:
                continue
            closed_at, exit_price, gross_pl, net_pl, result_r, win = derived
            model = model_map.get(actual.model_trade_id or "")
            observations.append(
                TradeObservation(
                    trade_id=actual.trade_id,
                    source="ACTUAL",
                    strategy_version=actual.strategy_version,
                    closed_at=closed_at,
                    direction=(model.calibration_direction if model else None) or idea.direction,
                    setup=(model.calibration_setup if model else None) or idea.setup,
                    market_regime=(
                        (model.calibration_market_regime if model else None) or idea.market_regime
                    ),
                    trend=model.calibration_trend if model else None,
                    volatility=model.calibration_volatility if model else None,
                    time_of_day=model.calibration_time_of_day if model else None,
                    rr_bucket=model.calibration_rr_bucket if model else None,
                    liquidity_state=model.calibration_liquidity_state if model else None,
                    context=model.calibration_context if model else None,
                    calibration_group=model.calibration_group if model else None,
                    calibration_eligible=False,
                    win=win,
                    result_r=result_r,
                    gross_pl_rub=gross_pl,
                    net_pl_rub=net_pl,
                    mfe_pct=actual.mfe_pct,
                    mae_pct=actual.mae_pct,
                    capture_ratio=actual.capture_ratio,
                    holding_hours=(
                        actual.holding_hours
                        if actual.holding_hours is not None
                        else (closed_at - actual.actual_entry_time).total_seconds() / 3600
                    ),
                    execution_quality=actual.execution_quality_actual,
                    entry_slippage_bps=actual.entry_slippage_bps,
                    entry_price=actual.actual_entry,
                    exit_price=exit_price,
                )
            )
        return observations

    @staticmethod
    def _actual_result(
        actual: ActualTradeJournal,
        events: list[TradeEventJournal],
    ) -> tuple[datetime, float, float, float | None, float | None, int | None] | None:
        initial_units = (
            actual.actual_position_shares
            if actual.actual_position_shares is not None
            else (
                actual.actual_position_rub / actual.actual_entry
                if actual.actual_position_rub is not None and actual.actual_entry > 0
                else None
            )
        )
        if initial_units is None:
            return None
        unit_mode = "SHARES" if actual.actual_position_shares is not None else "RUB"
        remaining_units = initial_units
        gross = 0.0
        exit_costs = 0.0
        costs_complete = actual.actual_entry_costs is not None
        closed_at: datetime | None = None
        exit_price: float | None = None
        direction = actual.trade_id.rsplit("-", maxsplit=2)[-2]
        multiplier = 1 if direction == "LONG" else -1
        for event in events:
            if event.event_type not in {
                TradeEventType.PARTIAL_EXIT.value,
                TradeEventType.REDUCE.value,
                TradeEventType.FULL_EXIT.value,
            }:
                continue
            if event.current_price is None:
                return None
            next_units = (
                0.0
                if event.event_type == TradeEventType.FULL_EXIT.value
                else (
                    event.position_after
                    if unit_mode == "SHARES" and event.position_after is not None
                    else (
                        event.position_after / actual.actual_entry
                        if event.position_after is not None
                        else None
                    )
                )
            )
            if next_units is None or next_units < 0 or next_units > remaining_units:
                return None
            closed_units = remaining_units - next_units
            gross += multiplier * (event.current_price - actual.actual_entry) * closed_units
            remaining_units = next_units
            if event.costs_rub is None:
                costs_complete = False
            else:
                exit_costs += event.costs_rub
            if event.event_type == TradeEventType.FULL_EXIT.value:
                closed_at = event.event_datetime
                exit_price = event.current_price
                break
        if closed_at is None or exit_price is None:
            return None
        net = (
            gross - float(actual.actual_entry_costs) - exit_costs
            if costs_complete and actual.actual_entry_costs is not None
            else None
        )
        result_r = (
            net / actual.initial_risk_rub
            if (
                net is not None
                and actual.initial_risk_rub is not None
                and actual.initial_risk_rub > 0
            )
            else actual.result_r
        )
        win = int(net > 0) if net is not None else actual.win_1_0
        return closed_at, exit_price, gross, net, result_r, win

    async def separate_global_statistics(
        self,
        *,
        strategy_version: str | None = None,
    ) -> dict[str, PerformanceMetrics]:
        model = await self.model_observations(strategy_version=strategy_version)
        actual = await self.actual_observations(strategy_version=strategy_version)
        return {
            "MODEL": calculate_performance(model),
            "ACTUAL": calculate_performance(actual),
        }
