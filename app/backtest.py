from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from statistics import fmean, median, pstdev

from app.analysis import TechnicalFeatures, prepare_technical_features
from app.config import Settings
from app.domain import (
    GeneratedSignal,
    HorizonProfile,
    IdeaHorizon,
    IdeaStatus,
    InsufficientDataError,
    TradingIdeaData,
)
from app.horizons import get_horizon_profile
from app.idea_tracker import evaluate_idea_candle
from app.ideas import build_trading_idea
from app.risk import apply_slippage, calculate_position_size, calculate_trade_pnl
from app.signals import analyze_signal_technical, build_signal


@dataclass(slots=True)
class BacktestTrade:
    ticker: str
    horizon: str
    timeframe: str
    direction: str
    sector: str
    confidence: float
    status: str
    created_at: datetime
    activated_at: datetime | None
    closed_at: datetime | None
    entry_price: float | None
    exit_price: float | None
    units: int
    lots: int
    gross_pnl: float
    commission: float
    net_pnl: float
    return_pct: float
    r_multiple: float
    holding_hours: float
    slippage: float = 0.0
    gross_return_pct: float = 0.0
    entry_fill_price: float | None = None
    exit_fill_price: float | None = None


@dataclass(frozen=True, slots=True)
class BacktestMetrics:
    idea_count: int
    activated_count: int
    activation_rate: float
    tp_hit_count: int
    sl_hit_count: int
    expired_count: int
    invalidated_count: int
    cancelled_count: int
    win_rate: float
    loss_rate: float
    expired_rate: float
    gross_pnl: float
    commission: float
    slippage: float
    net_pnl: float
    gross_return_pct: float
    net_return_pct: float
    total_return_pct: float
    average_return_pct: float
    average_r: float
    median_r: float
    expectancy_r: float
    profit_factor: float
    maximum_drawdown_pct: float
    sharpe_ratio: float
    average_holding_hours: float
    open_ideas: int


@dataclass(slots=True)
class BacktestResult:
    metrics: BacktestMetrics
    trades: list[BacktestTrade]
    breakdowns: dict[str, dict[str, dict[str, float | int]]] = field(default_factory=dict)


@dataclass(slots=True)
class _SimulationRecord:
    idea: TradingIdeaData
    equity_at_entry: float = 0.0
    units: int = 0
    lots: int = 0
    actual_risk: float = 0.0
    entry_fill_price: float | None = None
    exit_fill_price: float | None = None
    gross_pnl: float = 0.0
    commission: float = 0.0
    slippage: float = 0.0
    net_pnl: float = 0.0
    gross_return_pct: float = 0.0
    return_pct: float = 0.0
    r_multiple: float = 0.0


@dataclass(frozen=True, slots=True)
class HistoryIndex:
    candles: tuple[object, ...]
    end_times: tuple[datetime, ...]

    @classmethod
    def build(cls, candles: list[object]) -> HistoryIndex:
        ordered = tuple(sorted(candles, key=lambda candle: _utc(candle.end)))
        return cls(ordered, tuple(_utc(candle.end) for candle in ordered))

    def available(self, decision_at: datetime, *, limit: int = 500) -> list[object]:
        cutoff = bisect_right(self.end_times, _utc(decision_at))
        start = max(0, cutoff - limit)
        return list(self.candles[start:cutoff])

    def period(
        self,
        start_at: datetime | None,
        end_at: datetime | None,
    ) -> tuple[object, ...]:
        start = bisect_left(self.end_times, _utc(start_at)) if start_at is not None else 0
        stop = (
            bisect_left(self.end_times, _utc(end_at)) if end_at is not None else len(self.candles)
        )
        return self.candles[start:stop]


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def available_history(
    candles: list[object],
    decision_at: datetime,
    *,
    limit: int = 500,
) -> list[object]:
    """Return only candles fully known at decision time (audit/test helper)."""
    return HistoryIndex.build(candles).available(decision_at, limit=limit)


def _apply_transitions(record: _SimulationRecord, transitions: list[object]) -> None:
    idea = record.idea
    for transition in transitions:
        idea.status = transition.to_status
        if transition.to_status == IdeaStatus.ACTIVE:
            idea.activated_at = transition.occurred_at
            idea.activation_price = transition.price
        elif transition.to_status not in {IdeaStatus.PENDING_ENTRY, IdeaStatus.ACTIVE}:
            idea.closed_at = transition.occurred_at
            idea.close_price = transition.price
            idea.close_reason = transition.reason


def _opposite_side(direction: str) -> str:
    return "SELL" if direction == "BUY" else "BUY"


def _open_position(
    record: _SimulationRecord,
    *,
    equity: float,
    risk_pct: float,
    lot_size: int,
    buy_slippage_bps: float,
    sell_slippage_bps: float,
) -> None:
    idea = record.idea
    if idea.activation_price is None:
        return
    direction = idea.direction.value
    entry_fill = apply_slippage(
        idea.activation_price,
        order_side=direction,
        buy_slippage_bps=buy_slippage_bps,
        sell_slippage_bps=sell_slippage_bps,
    )
    stop_fill = apply_slippage(
        idea.stop_loss,
        order_side=_opposite_side(direction),
        buy_slippage_bps=buy_slippage_bps,
        sell_slippage_bps=sell_slippage_bps,
    )
    size = calculate_position_size(
        deposit=equity,
        risk_per_trade_pct=risk_pct,
        entry=entry_fill,
        stop_loss=stop_fill,
        lot_size=lot_size,
    )
    record.equity_at_entry = equity
    record.units = size.units
    record.lots = size.lots
    record.actual_risk = size.actual_risk
    record.entry_fill_price = entry_fill


def _close_position(
    record: _SimulationRecord,
    *,
    commission_pct: float,
    buy_slippage_bps: float,
    sell_slippage_bps: float,
) -> float:
    idea = record.idea
    if idea.activation_price is None or idea.close_price is None or record.units <= 0:
        return 0.0
    pnl = calculate_trade_pnl(
        direction=idea.direction.value,
        entry_price=idea.activation_price,
        exit_price=idea.close_price,
        units=record.units,
        commission_pct=commission_pct,
        actual_risk=record.actual_risk,
        buy_slippage_bps=buy_slippage_bps,
        sell_slippage_bps=sell_slippage_bps,
    )
    record.entry_fill_price = pnl.entry_fill_price
    record.exit_fill_price = pnl.exit_fill_price
    record.gross_pnl = pnl.gross_pnl
    record.commission = pnl.commission
    record.slippage = pnl.slippage
    record.net_pnl = pnl.net_pnl
    record.gross_return_pct = (
        record.gross_pnl / record.equity_at_entry * 100 if record.equity_at_entry else 0.0
    )
    record.return_pct = (
        record.net_pnl / record.equity_at_entry * 100 if record.equity_at_entry else 0.0
    )
    record.r_multiple = pnl.r_multiple
    return record.net_pnl


def _trade(record: _SimulationRecord, sector: str) -> BacktestTrade:
    idea = record.idea
    holding = 0.0
    if idea.activated_at is not None and idea.closed_at is not None:
        holding = (_utc(idea.closed_at) - _utc(idea.activated_at)).total_seconds() / 3600
    return BacktestTrade(
        ticker=idea.ticker,
        horizon=idea.horizon.value,
        timeframe=idea.primary_timeframe,
        direction=idea.direction.value,
        sector=sector,
        confidence=idea.confidence,
        status=idea.status.value,
        created_at=idea.created_at,
        activated_at=idea.activated_at,
        closed_at=idea.closed_at,
        entry_price=idea.activation_price,
        exit_price=idea.close_price,
        entry_fill_price=record.entry_fill_price,
        exit_fill_price=record.exit_fill_price,
        units=record.units,
        lots=record.lots,
        gross_pnl=record.gross_pnl,
        commission=record.commission,
        slippage=record.slippage,
        net_pnl=record.net_pnl,
        gross_return_pct=record.gross_return_pct,
        return_pct=record.return_pct,
        r_multiple=record.r_multiple,
        holding_hours=holding,
    )


def _maximum_drawdown(equity_curve: list[float]) -> float:
    if not equity_curve:
        return 0.0
    peak = equity_curve[0]
    maximum = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        if peak > 0:
            maximum = max(maximum, (peak - value) / peak * 100)
    return maximum


def _profit_factor(trades: list[BacktestTrade]) -> float:
    positive = sum(trade.net_pnl for trade in trades if trade.net_pnl > 0)
    negative = abs(sum(trade.net_pnl for trade in trades if trade.net_pnl < 0))
    return positive / negative if negative else (math.inf if positive else 0.0)


def _confidence_bucket(trade: BacktestTrade) -> str:
    if trade.confidence >= 90:
        return "90+"
    lower = int(trade.confidence // 10) * 10
    return f"{lower}-{lower + 9}"


def _group_summary(items: list[BacktestTrade]) -> dict[str, float | int]:
    activated = [trade for trade in items if trade.entry_price is not None]
    completed = [trade for trade in activated if trade.exit_price is not None]
    wins = [trade for trade in completed if trade.net_pnl > 0]
    r_values = [trade.r_multiple for trade in completed]
    return {
        "ideas": len(items),
        "activated": len(activated),
        "activation_rate": len(activated) / len(items) * 100 if items else 0.0,
        "tp_hit": sum(trade.status == IdeaStatus.TP_HIT.value for trade in items),
        "sl_hit": sum(trade.status == IdeaStatus.SL_HIT.value for trade in items),
        "expired": sum(trade.status == IdeaStatus.EXPIRED.value for trade in items),
        "invalidated": sum(trade.status == IdeaStatus.INVALIDATED.value for trade in items),
        "win_rate": len(wins) / len(completed) * 100 if completed else 0.0,
        "profit_factor": _profit_factor(completed),
        "expectancy_r": fmean(r_values) if r_values else 0.0,
        "average_r": fmean(r_values) if r_values else 0.0,
        "median_r": median(r_values) if r_values else 0.0,
        "gross_pnl": sum(trade.gross_pnl for trade in completed),
        "commission": sum(trade.commission for trade in completed),
        "slippage": sum(trade.slippage for trade in completed),
        "net_pnl": sum(trade.net_pnl for trade in completed),
        "average_holding_hours": (
            fmean(trade.holding_hours for trade in completed) if completed else 0.0
        ),
    }


def build_breakdowns(
    trades: list[BacktestTrade],
) -> dict[str, dict[str, dict[str, float | int]]]:
    dimensions = {
        "ticker": lambda trade: trade.ticker,
        "horizon": lambda trade: trade.horizon,
        "timeframe": lambda trade: trade.timeframe,
        "direction": lambda trade: trade.direction,
        "sector": lambda trade: trade.sector,
        "confidence_bucket": _confidence_bucket,
        "calendar_year": lambda trade: str(_utc(trade.created_at).year),
    }
    result: dict[str, dict[str, dict[str, float | int]]] = {}
    for dimension, key_function in dimensions.items():
        groups: dict[str, list[BacktestTrade]] = {}
        for trade in trades:
            groups.setdefault(key_function(trade), []).append(trade)
        result[dimension] = {key: _group_summary(items) for key, items in groups.items()}
    return result


def calculate_backtest_metrics(
    trades: list[BacktestTrade],
    *,
    initial_equity: float,
    equity_curve: list[float],
) -> BacktestMetrics:
    activated = [trade for trade in trades if trade.entry_price is not None]
    completed = [trade for trade in activated if trade.exit_price is not None]
    wins = [trade for trade in completed if trade.net_pnl > 0]
    losses = [trade for trade in completed if trade.net_pnl < 0]
    expired = [trade for trade in trades if trade.status == IdeaStatus.EXPIRED.value]
    returns = [trade.return_pct for trade in completed]
    r_values = [trade.r_multiple for trade in completed]
    holding = [trade.holding_hours for trade in completed]
    sharpe = 0.0
    if len(returns) >= 2 and pstdev(returns) > 0:
        # Trade-level, non-annualized Sharpe; trades have irregular holding periods.
        sharpe = fmean(returns) / pstdev(returns)
    denominator = len(completed) or 1
    gross_pnl = sum(trade.gross_pnl for trade in completed)
    commission = sum(trade.commission for trade in completed)
    slippage = sum(trade.slippage for trade in completed)
    net_pnl = sum(trade.net_pnl for trade in completed)
    net_return = (equity_curve[-1] / initial_equity - 1) * 100
    return BacktestMetrics(
        idea_count=len(trades),
        activated_count=len(activated),
        activation_rate=len(activated) / len(trades) * 100 if trades else 0.0,
        tp_hit_count=sum(trade.status == IdeaStatus.TP_HIT.value for trade in trades),
        sl_hit_count=sum(trade.status == IdeaStatus.SL_HIT.value for trade in trades),
        expired_count=len(expired),
        invalidated_count=sum(trade.status == IdeaStatus.INVALIDATED.value for trade in trades),
        cancelled_count=sum(trade.status == IdeaStatus.CANCELLED.value for trade in trades),
        win_rate=len(wins) / denominator * 100,
        loss_rate=len(losses) / denominator * 100,
        expired_rate=len(expired) / (len(trades) or 1) * 100,
        gross_pnl=gross_pnl,
        commission=commission,
        slippage=slippage,
        net_pnl=net_pnl,
        gross_return_pct=gross_pnl / initial_equity * 100,
        net_return_pct=net_return,
        total_return_pct=net_return,
        average_return_pct=fmean(returns) if returns else 0.0,
        average_r=fmean(r_values) if r_values else 0.0,
        median_r=median(r_values) if r_values else 0.0,
        expectancy_r=fmean(r_values) if r_values else 0.0,
        profit_factor=_profit_factor(completed),
        maximum_drawdown_pct=_maximum_drawdown(equity_curve),
        sharpe_ratio=sharpe,
        average_holding_hours=fmean(holding) if holding else 0.0,
        open_ideas=sum(
            trade.status in {IdeaStatus.PENDING_ENTRY.value, IdeaStatus.ACTIVE.value}
            for trade in trades
        ),
    )


class BacktestEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _close_record(self, record: _SimulationRecord) -> float:
        return _close_position(
            record,
            commission_pct=self.settings.backtest_commission_pct,
            buy_slippage_bps=self.settings.backtest_buy_slippage_bps,
            sell_slippage_bps=self.settings.backtest_sell_slippage_bps,
        )

    def _open_record(self, record: _SimulationRecord, *, equity: float, lot_size: int) -> None:
        _open_position(
            record,
            equity=equity,
            risk_pct=self.settings.default_risk_per_trade_pct,
            lot_size=lot_size,
            buy_slippage_bps=self.settings.backtest_buy_slippage_bps,
            sell_slippage_bps=self.settings.backtest_sell_slippage_bps,
        )

    def run(
        self,
        *,
        ticker: str,
        instrument_name: str,
        horizon: IdeaHorizon,
        candles_by_timeframe: dict[str, list[object]],
        lot_size: int = 1,
        initial_equity: float | None = None,
        sector: str = "unknown",
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        profile: HorizonProfile | None = None,
        liquidate_at_end: bool = True,
        analysis_cache: dict[tuple[object, ...], TechnicalFeatures] | None = None,
        feature_provider: (Callable[[str, str, list[object]], TechnicalFeatures] | None) = None,
        prepared_indexes: dict[str, HistoryIndex] | None = None,
    ) -> BacktestResult:
        selected_profile = profile or get_horizon_profile(horizon)
        if selected_profile.horizon != horizon:
            raise ValueError("profile horizon must match requested horizon")
        if start_at is not None and end_at is not None and _utc(start_at) >= _utc(end_at):
            raise ValueError("start_at must precede end_at")

        indexes = {
            timeframe: (
                prepared_indexes[timeframe]
                if prepared_indexes is not None and timeframe in prepared_indexes
                else HistoryIndex.build(candles_by_timeframe.get(timeframe, []))
            )
            for timeframe in selected_profile.timeframe_weights
        }
        primary_candles = indexes[selected_profile.primary_timeframe].period(start_at, end_at)
        equity = initial_equity or self.settings.paper_account_size
        if equity <= 0:
            raise ValueError("initial equity must be positive")
        starting_equity = equity
        equity_curve = [equity]
        records: list[_SimulationRecord] = []
        open_record: _SimulationRecord | None = None
        last_processed: object | None = None

        for primary in primary_candles:
            decision_at = _utc(primary.end)
            last_processed = primary

            if open_record is not None:
                transitions = evaluate_idea_candle(open_record.idea, primary)
                for transition in transitions:
                    _apply_transitions(open_record, [transition])
                    if transition.to_status == IdeaStatus.ACTIVE:
                        self._open_record(open_record, equity=equity, lot_size=lot_size)
                if open_record.idea.status not in {
                    IdeaStatus.PENDING_ENTRY,
                    IdeaStatus.ACTIVE,
                }:
                    equity += self._close_record(open_record)
                    equity_curve.append(equity)
                    open_record = None

            if open_record is not None:
                continue
            generated_signals: list[GeneratedSignal] = []
            for timeframe in selected_profile.timeframe_weights:
                history = indexes[timeframe].available(decision_at)
                if len(history) < 60:
                    continue
                try:
                    if feature_provider is not None:
                        features = feature_provider(ticker, timeframe, history)
                    else:
                        cache_key = (ticker, timeframe, _utc(history[-1].end))
                        features = (
                            analysis_cache.get(cache_key) if analysis_cache is not None else None
                        )
                        if features is None:
                            features = prepare_technical_features(history)
                            if analysis_cache is not None:
                                analysis_cache[cache_key] = features
                    technical = analyze_signal_technical(
                        self.settings,
                        history,
                        features=features,
                    )
                    generated_signals.append(
                        build_signal(
                            self.settings,
                            ticker,
                            timeframe,
                            history,
                            technical_result=technical,
                        )
                    )
                except InsufficientDataError:
                    continue
            candidate = build_trading_idea(
                self.settings,
                instrument_name=instrument_name,
                horizon=horizon,
                signals=generated_signals,
                now=decision_at,
                profile=selected_profile,
            )
            if candidate is None or candidate.status == IdeaStatus.INVALIDATED:
                continue

            # The signal is only known after the decision candle closes. Even when
            # that close lies in the entry zone, execution starts on the next bar.
            candidate.status = IdeaStatus.PENDING_ENTRY
            candidate.activated_at = None
            candidate.activation_price = None
            record = _SimulationRecord(candidate)
            records.append(record)
            open_record = record

        if liquidate_at_end and open_record is not None and last_processed is not None:
            idea = open_record.idea
            idea.status = IdeaStatus.CANCELLED
            idea.closed_at = _utc(last_processed.end)
            idea.close_reason = "Завершение backtest-периода"
            if idea.activation_price is not None:
                idea.close_price = float(last_processed.close)
                equity += self._close_record(open_record)
                equity_curve.append(equity)
            open_record = None

        trades = [_trade(record, sector) for record in records]
        metrics = calculate_backtest_metrics(
            trades,
            initial_equity=starting_equity,
            equity_curve=equity_curve,
        )
        return BacktestResult(metrics=metrics, trades=trades, breakdowns=build_breakdowns(trades))
