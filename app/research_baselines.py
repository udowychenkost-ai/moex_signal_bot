from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd

from app.analysis import calculate_rsi
from app.backtest import BacktestResult, BacktestTrade, build_breakdowns, calculate_backtest_metrics
from app.config import Settings
from app.domain import IdeaHorizon
from app.horizons import get_horizon_profile
from app.risk import apply_slippage, calculate_trade_pnl


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _in_period(candle: object, start: datetime, end: datetime) -> bool:
    return _utc(candle.end) >= _utc(start) and _utc(candle.end) < _utc(end)


def _signals(candles: list[object], strategy: str) -> list[bool]:
    close = pd.Series([float(candle.close) for candle in candles])
    if strategy == "buy_hold":
        return [True] * len(candles)
    if strategy == "ema_trend":
        ema = close.ewm(span=50, adjust=False).mean()
        return list((close > ema).fillna(False))
    if strategy == "rsi_reversion":
        rsi = calculate_rsi(close)
        state = False
        result: list[bool] = []
        for value in rsi:
            if not state and pd.notna(value) and value <= 30:
                state = True
            elif state and pd.notna(value) and value >= 55:
                state = False
            result.append(state)
        return result
    raise ValueError(f"unsupported research baseline: {strategy}")


def run_long_baseline(
    settings: Settings,
    *,
    strategy: str,
    ticker: str,
    horizon: IdeaHorizon,
    candles: list[object],
    lot_size: int,
    initial_equity: float,
    start_at: datetime,
    end_at: datetime,
) -> BacktestResult:
    ordered = sorted(candles, key=lambda candle: _utc(candle.end))
    in_period = [candle for candle in ordered if _in_period(candle, start_at, end_at)]
    if len(in_period) < 2:
        return BacktestResult(
            metrics=calculate_backtest_metrics(
                [], initial_equity=initial_equity, equity_curve=[initial_equity]
            ),
            trades=[],
            breakdowns=build_breakdowns([]),
        )
    desired = _signals(in_period, strategy)
    equity = initial_equity
    equity_curve = [equity]
    trades: list[BacktestTrade] = []
    open_position: dict[str, object] | None = None

    for index in range(1, len(in_period)):
        decision = in_period[index - 1]
        execution = in_period[index]
        should_hold = desired[index - 1]
        if should_hold and open_position is None:
            reference_entry = float(execution.open)
            entry_fill = apply_slippage(
                reference_entry,
                order_side="BUY",
                buy_slippage_bps=settings.backtest_buy_slippage_bps,
                sell_slippage_bps=settings.backtest_sell_slippage_bps,
            )
            units = (int(equity // entry_fill) // lot_size) * lot_size
            open_position = {
                "created_at": _utc(decision.end),
                "activated_at": _utc(execution.begin),
                "entry_price": reference_entry,
                "entry_fill": entry_fill,
                "units": units,
                "lot_size": lot_size,
                "equity": equity,
            }
        elif not should_hold and open_position is not None:
            equity += _close_baseline_trade(
                settings,
                trades,
                open_position,
                ticker=ticker,
                horizon=horizon,
                timeframe=get_horizon_profile(horizon).primary_timeframe,
                exit_price=float(execution.open),
                closed_at=_utc(execution.begin),
            )
            equity_curve.append(equity)
            open_position = None

    if open_position is not None:
        last = in_period[-1]
        equity += _close_baseline_trade(
            settings,
            trades,
            open_position,
            ticker=ticker,
            horizon=horizon,
            timeframe=get_horizon_profile(horizon).primary_timeframe,
            exit_price=float(last.close),
            closed_at=_utc(last.end),
        )
        equity_curve.append(equity)

    return BacktestResult(
        metrics=calculate_backtest_metrics(
            trades,
            initial_equity=initial_equity,
            equity_curve=equity_curve,
        ),
        trades=trades,
        breakdowns=build_breakdowns(trades),
    )


def _close_baseline_trade(
    settings: Settings,
    trades: list[BacktestTrade],
    position: dict[str, object],
    *,
    ticker: str,
    horizon: IdeaHorizon,
    timeframe: str,
    exit_price: float,
    closed_at: datetime,
) -> float:
    entry_price = float(position["entry_price"])
    units = int(position["units"])
    lot_size = int(position["lot_size"])
    equity = float(position["equity"])
    pnl = calculate_trade_pnl(
        direction="BUY",
        entry_price=entry_price,
        exit_price=exit_price,
        units=units,
        commission_pct=settings.backtest_commission_pct,
        actual_risk=float(position["entry_fill"]) * units,
        buy_slippage_bps=settings.backtest_buy_slippage_bps,
        sell_slippage_bps=settings.backtest_sell_slippage_bps,
    )
    activated_at = position["activated_at"]
    holding = (_utc(closed_at) - _utc(activated_at)).total_seconds() / 3600
    trades.append(
        BacktestTrade(
            ticker=ticker,
            horizon=horizon.value,
            timeframe=timeframe,
            direction="BUY",
            sector="unknown",
            confidence=0,
            status="CLOSED",
            created_at=position["created_at"],
            activated_at=activated_at,
            closed_at=closed_at,
            entry_price=entry_price,
            exit_price=exit_price,
            entry_fill_price=pnl.entry_fill_price,
            exit_fill_price=pnl.exit_fill_price,
            units=units,
            lots=units // lot_size,
            gross_pnl=pnl.gross_pnl,
            commission=pnl.commission,
            slippage=pnl.slippage,
            net_pnl=pnl.net_pnl,
            gross_return_pct=pnl.gross_pnl / equity * 100 if equity else 0,
            return_pct=pnl.net_pnl / equity * 100 if equity else 0,
            r_multiple=pnl.r_multiple,
            holding_hours=holding,
        )
    )
    return pnl.net_pnl
