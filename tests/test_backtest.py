from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.backtest import (
    BacktestEngine,
    BacktestTrade,
    HistoryIndex,
    available_history,
    calculate_backtest_metrics,
)
from app.config import Settings
from app.domain import IdeaHorizon, IdeaStatus
from app.ideas import build_trading_idea
from app.signals import build_signal
from tests.test_extended_analysis import make_candles
from tests.test_ideas import NOW, signal


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
    assert metrics.gross_pnl == pytest.approx(60)
    assert metrics.commission == pytest.approx(10)
    assert metrics.slippage == 0
    assert metrics.net_pnl == pytest.approx(50)


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
        "calendar_year",
    }
    assert result.breakdowns["ticker"]["SBER"]["ideas"] == result.metrics.idea_count
    assert all(item.units % 10 == 0 for item in result.trades)
    assert all(item.commission >= 0 for item in result.trades)
    assert all(item.slippage >= 0 for item in result.trades)


def test_history_at_decision_excludes_unfinished_and_future_candles() -> None:
    history = _history(80)
    decision_at = history[59].end

    available = available_history(history, decision_at)

    assert available == history[:60]
    assert all(candle.end <= decision_at for candle in available)


def test_future_prices_do_not_change_signal_or_levels_at_decision() -> None:
    history = _history(100)
    decision_at = history[-1].end
    extreme_future = SimpleNamespace(
        begin=decision_at,
        end=decision_at + timedelta(hours=1),
        open=10_000.0,
        high=20_000.0,
        low=1.0,
        close=15_000.0,
        volume=10**12,
    )
    settings = Settings(_env_file=None)

    prefix_signal = build_signal(settings, "SBER", "1h", history)
    isolated_signal = build_signal(
        settings,
        "SBER",
        "1h",
        available_history([*history, extreme_future], decision_at),
    )

    assert isolated_signal.technical_score == prefix_signal.technical_score
    assert isolated_signal.atr == prefix_signal.atr
    assert isolated_signal.support_levels == prefix_signal.support_levels
    assert isolated_signal.resistance_levels == prefix_signal.resistance_levels


def test_backtest_never_fills_on_signal_formation_bar(monkeypatch) -> None:
    settings = Settings(_env_file=None, backtest_buy_slippage_bps=0, backtest_sell_slippage_bps=0)
    template = build_trading_idea(
        settings,
        instrument_name="Сбербанк",
        horizon=IdeaHorizon.INTRADAY_1D,
        signals=[signal("15m", 60), signal("1h", 60), signal("1d", 60)],
        now=NOW,
    )
    assert template is not None
    template = replace(
        template,
        status=IdeaStatus.ACTIVE,
        activated_at=NOW,
        activation_price=777,
        entry_price_from=99,
        entry_price_to=100,
        take_profit=106,
        stop_loss=96,
    )
    first = SimpleNamespace(
        begin=datetime(2026, 1, 1, 10, tzinfo=UTC),
        end=datetime(2026, 1, 1, 11, tzinfo=UTC),
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.0,
        volume=1_000.0,
    )
    second = SimpleNamespace(
        begin=first.end,
        end=first.end + timedelta(hours=1),
        open=102.0,
        high=103.0,
        low=99.5,
        close=101.0,
        volume=1_000.0,
    )

    monkeypatch.setattr(
        "app.backtest.build_trading_idea",
        lambda *args, **kwargs: replace(template),
    )
    result = BacktestEngine(settings).run(
        ticker="SBER",
        instrument_name="Сбербанк",
        horizon=IdeaHorizon.INTRADAY_1D,
        candles_by_timeframe={"15m": [first, second]},
        initial_equity=100_000,
    )

    assert result.metrics.idea_count == 1
    assert result.metrics.activated_count == 1
    assert result.trades[0].entry_price == 100
    assert result.trades[0].entry_price != 777
    assert result.trades[0].activated_at == second.begin


def test_backtest_period_end_is_exclusive_for_non_overlapping_splits(monkeypatch) -> None:
    candles = _history(2)
    decisions: list[datetime] = []

    def record_decision(*args, **kwargs):
        decisions.append(kwargs["now"])
        return None

    monkeypatch.setattr("app.backtest.build_trading_idea", record_decision)
    BacktestEngine(Settings(_env_file=None)).run(
        ticker="SBER",
        instrument_name="Сбербанк",
        horizon=IdeaHorizon.INTRADAY_1D,
        candles_by_timeframe={"15m": candles},
        start_at=candles[0].end,
        end_at=candles[1].end,
    )

    assert decisions == [candles[0].end]


def test_research_analysis_cache_reuses_only_identical_historical_analysis(monkeypatch) -> None:
    history = _history(100)
    settings = Settings(_env_file=None, signal_threshold=20)
    cache = {}
    calls = 0
    original = __import__("app.backtest", fromlist=["prepare_technical_features"])
    real_prepare = original.prepare_technical_features

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real_prepare(*args, **kwargs)

    monkeypatch.setattr("app.backtest.prepare_technical_features", counted)
    kwargs = {
        "ticker": "SBER",
        "instrument_name": "Сбербанк",
        "horizon": IdeaHorizon.INTRADAY_1D,
        "candles_by_timeframe": {"15m": history, "1h": history, "1d": history},
        "analysis_cache": cache,
    }
    first = BacktestEngine(settings).run(**kwargs)
    first_call_count = calls
    second = BacktestEngine(settings).run(**kwargs)

    assert first_call_count > 0
    assert calls == first_call_count
    assert first.metrics == second.metrics


def test_prepared_history_indexes_preserve_backtest_results() -> None:
    history = _history(100)
    settings = Settings(_env_file=None, signal_threshold=20)
    histories = {"15m": history, "1h": history, "1d": history}
    kwargs = {
        "ticker": "SBER",
        "instrument_name": "Сбербанк",
        "horizon": IdeaHorizon.INTRADAY_1D,
        "candles_by_timeframe": histories,
        "start_at": history[20].end,
        "end_at": history[-10].end,
    }

    canonical = BacktestEngine(settings).run(**kwargs)
    prepared = BacktestEngine(settings).run(
        **kwargs,
        prepared_indexes={
            timeframe: HistoryIndex.build(candles) for timeframe, candles in histories.items()
        },
    )

    assert prepared.metrics == canonical.metrics
    assert prepared.trades == canonical.trades
