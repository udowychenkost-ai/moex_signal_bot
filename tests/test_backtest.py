from __future__ import annotations

import math
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.backtest import (
    BacktestEngine,
    BacktestTrade,
    calculate_backtest_metrics,
)
from app.config import Settings
from app.domain import IdeaHorizon
from tests.test_extended_analysis import make_candles


def trade(*, net_pnl: float, return_pct: float, r_multiple: float) -> BacktestTrade:
    candles = make_candles(1)
    started = candles[0].begin
    return BacktestTrade(
        ticker="SBER",
        horizon=IdeaHorizon.SWING_5D.value,
        timeframe="4h",
        direction="BUY",
        sector="banks",
        confidence=75,
        status="TP_HIT" if net_pnl > 0 else "SL_HIT",
        created_at=started,
        activated_at=started,
        closed_at=started + timedelta(hours=4),
        entry_price=100,
        exit_price=105 if net_pnl > 0 else 97,
        units=10,
        lots=1,
        gross_pnl=net_pnl + 5,
        commission=5,
        net_pnl=net_pnl,
        return_pct=return_pct,
        r_multiple=r_multiple,
        holding_hours=4,
    )


def test_backtest_metrics_include_risk_and_drawdown_statistics() -> None:
    trades = [
        trade(net_pnl=100, return_pct=10, r_multiple=2),
        trade(net_pnl=-50, return_pct=-4.5454545, r_multiple=-1),
    ]
    metrics = calculate_backtest_metrics(
        trades,
        initial_equity=1000,
        equity_curve=[1000, 1100, 1050],
    )
    assert metrics.idea_count == 2
    assert metrics.activated_count == 2
    assert metrics.win_rate == 50
    assert metrics.loss_rate == 50
    assert metrics.total_return_pct == pytest.approx(5)
    assert metrics.average_r == pytest.approx(0.5)
    assert metrics.expectancy_r == pytest.approx(0.5)
    assert metrics.profit_factor == pytest.approx(2)
    assert metrics.maximum_drawdown_pct == pytest.approx(50 / 1100 * 100)
    assert math.isfinite(metrics.sharpe_ratio)


def _history(count: int = 250) -> list[SimpleNamespace]:
    result: list[SimpleNamespace] = []
    for candle in make_candles(count=count, trend="up"):
        result.append(
            SimpleNamespace(
                begin=candle.begin,
                end=candle.begin + timedelta(hours=1),
                open=candle.open,
                high=candle.high,
                low=candle.low,
                close=candle.close,
                volume=candle.volume,
            )
        )
    return result


def test_backtest_runs_the_production_signal_and_idea_pipeline() -> None:
    history = _history()
    settings = Settings(
        _env_file=None,
        technical_scoring_model="legacy",
        signal_threshold=20,
        idea_minimum_confidence=60,
        backtest_commission_pct=0.05,
    )
    result = BacktestEngine(settings).run(
        ticker="SBER",
        instrument_name="Сбербанк",
        horizon=IdeaHorizon.INTRADAY_1D,
        candles_by_timeframe={
            "15m": history,
            "1h": history,
            "1d": history,
        },
        lot_size=10,
        initial_equity=100_000,
        sector="banks",
    )
    assert result.metrics.idea_count > 0
    assert result.metrics.activated_count <= result.metrics.idea_count
    assert result.metrics.maximum_drawdown_pct >= 0
    assert set(result.breakdowns) == {
        "ticker",
        "horizon",
        "timeframe",
        "direction",
        "sector",
        "confidence_bucket",
    }
    assert result.breakdowns["ticker"]["SBER"]["ideas"] == result.metrics.idea_count
    assert all(item.units % 10 == 0 for item in result.trades)
    assert all(item.commission >= 0 for item in result.trades)
