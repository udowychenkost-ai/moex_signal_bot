from datetime import timedelta

from app.config import Settings
from app.domain import IdeaHorizon
from app.research_baselines import run_long_baseline
from tests.test_backtest import _history


def test_buy_hold_baseline_executes_after_decision_and_attributes_costs() -> None:
    candles = _history(100)
    settings = Settings(
        _env_file=None,
        backtest_commission_pct=0.05,
        backtest_buy_slippage_bps=10,
        backtest_sell_slippage_bps=20,
    )

    result = run_long_baseline(
        settings,
        strategy="buy_hold",
        ticker="SBER",
        horizon=IdeaHorizon.POSITION_1M,
        candles=candles,
        lot_size=10,
        initial_equity=100_000,
        start_at=candles[0].end,
        end_at=candles[-1].end,
    )

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.activated_at >= trade.created_at
    assert trade.commission > 0
    assert trade.slippage > 0
    assert trade.lots == trade.units // 10
    assert trade.net_pnl == trade.gross_pnl - trade.commission - trade.slippage


def test_ema_and_rsi_baselines_are_deterministic_research_only_benchmarks() -> None:
    candles = _history(250)
    settings = Settings(_env_file=None)
    kwargs = {
        "ticker": "SBER",
        "horizon": IdeaHorizon.SWING_5D,
        "candles": candles,
        "lot_size": 10,
        "initial_equity": 100_000,
        "start_at": candles[0].end,
        "end_at": candles[-1].end + timedelta(seconds=1),
    }

    first = run_long_baseline(settings, strategy="ema_trend", **kwargs)
    second = run_long_baseline(settings, strategy="ema_trend", **kwargs)
    rsi = run_long_baseline(settings, strategy="rsi_reversion", **kwargs)

    assert first.metrics == second.metrics
    assert rsi.metrics.idea_count >= 0
