from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from statistics import fmean, pstdev

from app.config import Settings
from app.domain import (
    GeneratedSignal,
    IdeaHorizon,
    IdeaStatus,
    TradingIdeaData,
)
from app.horizons import get_horizon_profile
from app.idea_tracker import evaluate_idea_candle
from app.ideas import build_trading_idea
from app.risk import calculate_position_size
from app.signals import build_signal


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


@dataclass(frozen=True, slots=True)
class BacktestMetrics:
    idea_count: int
    activated_count: int
    win_rate: float
    loss_rate: float
    expired_rate: float
    total_return_pct: float
    average_return_pct: float
    average_r: float
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
    gross_pnl: float = 0.0
    commission: float = 0.0
    net_pnl: float = 0.0
    return_pct: float = 0.0
    r_multiple: float = 0.0


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


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


def _open_position(
    record: _SimulationRecord,
    *,
    equity: float,
    risk_pct: float,
    lot_size: int,
) -> None:
    if record.idea.activation_price is None:
        return
    size = calculate_position_size(
        deposit=equity,
        risk_per_trade_pct=risk_pct,
        entry=record.idea.activation_price,
        stop_loss=record.idea.stop_loss,
        lot_size=lot_size,
    )
    record.equity_at_entry = equity
    record.units = size.units
    record.lots = size.lots
    record.actual_risk = size.actual_risk


def _close_position(
    record: _SimulationRecord,
    *,
    commission_pct: float,
) -> float:
    idea = record.idea
    if idea.activation_price is None or idea.close_price is None or record.units <= 0:
        return 0.0
    price_move = (
        idea.close_price - idea.activation_price
        if idea.direction.value == "BUY"
        else idea.activation_price - idea.close_price
    )
    record.gross_pnl = price_move * record.units
    record.commission = (
        (idea.activation_price + idea.close_price) * record.units * commission_pct / 100
    )
    record.net_pnl = record.gross_pnl - record.commission
    record.return_pct = (
        record.net_pnl / record.equity_at_entry * 100 if record.equity_at_entry else 0.0
    )
    record.r_multiple = record.net_pnl / record.actual_risk if record.actual_risk else 0.0
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
        units=record.units,
        lots=record.lots,
        gross_pnl=record.gross_pnl,
        commission=record.commission,
        net_pnl=record.net_pnl,
        return_pct=record.return_pct,
        r_multiple=record.r_multiple,
        holding_hours=holding,
    )


def _maximum_drawdown(equity_curve: list[float]) -> float:
    peak = equity_curve[0]
    maximum = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        if peak > 0:
            maximum = max(maximum, (peak - value) / peak * 100)
    return maximum


def _confidence_bucket(trade: BacktestTrade) -> str:
    lower = int(trade.confidence // 10) * 10
    return f"{lower}-{lower + 9}"


def _breakdowns(trades: list[BacktestTrade]) -> dict[str, dict[str, dict[str, float | int]]]:
    dimensions = {
        "ticker": lambda trade: trade.ticker,
        "horizon": lambda trade: trade.horizon,
        "timeframe": lambda trade: trade.timeframe,
        "direction": lambda trade: trade.direction,
        "sector": lambda trade: trade.sector,
        "confidence_bucket": _confidence_bucket,
    }
    result: dict[str, dict[str, dict[str, float | int]]] = {}
    for dimension, key_function in dimensions.items():
        groups: dict[str, list[BacktestTrade]] = {}
        for trade in trades:
            groups.setdefault(key_function(trade), []).append(trade)
        result[dimension] = {
            key: {
                "ideas": len(items),
                "activated": sum(item.entry_price is not None for item in items),
                "average_return_pct": round(fmean(item.return_pct for item in items), 6),
            }
            for key, items in groups.items()
        }
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
    positive = sum(trade.net_pnl for trade in wins)
    negative = abs(sum(trade.net_pnl for trade in losses))
    profit_factor = positive / negative if negative else (math.inf if positive else 0.0)
    sharpe = 0.0
    if len(returns) >= 2 and pstdev(returns) > 0:
        sharpe = fmean(returns) / pstdev(returns) * math.sqrt(len(returns))
    denominator = len(completed) or 1
    return BacktestMetrics(
        idea_count=len(trades),
        activated_count=len(activated),
        win_rate=len(wins) / denominator * 100,
        loss_rate=len(losses) / denominator * 100,
        expired_rate=len(expired) / (len(trades) or 1) * 100,
        total_return_pct=(equity_curve[-1] / initial_equity - 1) * 100,
        average_return_pct=fmean(returns) if returns else 0.0,
        average_r=fmean(r_values) if r_values else 0.0,
        expectancy_r=fmean(r_values) if r_values else 0.0,
        profit_factor=profit_factor,
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
    ) -> BacktestResult:
        profile = get_horizon_profile(horizon)
        primary_candles = sorted(
            candles_by_timeframe.get(profile.primary_timeframe, []),
            key=lambda candle: candle.begin,
        )
        equity = initial_equity or self.settings.paper_account_size
        starting_equity = equity
        equity_curve = [equity]
        records: list[_SimulationRecord] = []
        open_record: _SimulationRecord | None = None

        for primary in primary_candles:
            if open_record is not None:
                transitions = evaluate_idea_candle(open_record.idea, primary)
                for transition in transitions:
                    _apply_transitions(open_record, [transition])
                    if transition.to_status == IdeaStatus.ACTIVE:
                        _open_position(
                            open_record,
                            equity=equity,
                            risk_pct=self.settings.default_risk_per_trade_pct,
                            lot_size=lot_size,
                        )
                if open_record.idea.status not in {
                    IdeaStatus.PENDING_ENTRY,
                    IdeaStatus.ACTIVE,
                }:
                    equity += _close_position(
                        open_record,
                        commission_pct=self.settings.backtest_commission_pct,
                    )
                    equity_curve.append(equity)
                    open_record = None

            if open_record is not None:
                continue
            generated_signals: list[GeneratedSignal] = []
            for timeframe in profile.timeframe_weights:
                history = [
                    candle
                    for candle in candles_by_timeframe.get(timeframe, [])
                    if _utc(candle.end) <= _utc(primary.end)
                ]
                if len(history) < 60:
                    continue
                generated_signals.append(
                    build_signal(
                        self.settings,
                        ticker,
                        timeframe,
                        history[-500:],
                    )
                )
            candidate = build_trading_idea(
                self.settings,
                instrument_name=instrument_name,
                horizon=horizon,
                signals=generated_signals,
                now=_utc(primary.end),
            )
            if candidate is None:
                continue
            record = _SimulationRecord(candidate)
            records.append(record)
            open_record = record
            if candidate.status == IdeaStatus.ACTIVE:
                _open_position(
                    record,
                    equity=equity,
                    risk_pct=self.settings.default_risk_per_trade_pct,
                    lot_size=lot_size,
                )

        trades = [_trade(record, sector) for record in records]
        metrics = calculate_backtest_metrics(
            trades,
            initial_equity=starting_equity,
            equity_curve=equity_curve,
        )
        return BacktestResult(metrics=metrics, trades=trades, breakdowns=_breakdowns(trades))
