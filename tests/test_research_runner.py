import json
from dataclasses import asdict
from datetime import UTC, datetime

from app.backtest import BacktestMetrics, BacktestResult
from app.research_runner import _confidence_assessment, render_markdown_report, result_payload


def _result() -> BacktestResult:
    metrics = BacktestMetrics(
        idea_count=4,
        activated_count=3,
        activation_rate=75,
        tp_hit_count=1,
        sl_hit_count=1,
        expired_count=1,
        invalidated_count=0,
        cancelled_count=1,
        win_rate=50,
        loss_rate=50,
        expired_rate=25,
        gross_pnl=100,
        commission=10,
        slippage=5,
        net_pnl=85,
        gross_return_pct=0.1,
        net_return_pct=0.085,
        total_return_pct=0.085,
        average_return_pct=0.03,
        average_r=0.2,
        median_r=0.2,
        expectancy_r=0.2,
        profit_factor=float("inf"),
        maximum_drawdown_pct=1,
        sharpe_ratio=0.4,
        average_holding_hours=12,
        open_ideas=0,
    )
    return BacktestResult(metrics, [], {"ticker": {}, "direction": {}, "confidence_bucket": {}})


def test_result_payload_is_strict_json_even_with_infinite_profit_factor() -> None:
    payload = result_payload(_result())

    assert payload["metrics"]["profit_factor"] is None
    json.dumps(payload, allow_nan=False)


def test_markdown_report_keeps_oos_cost_and_quality_metrics() -> None:
    result = _result()
    payload = {
        "generated_at": datetime(2026, 8, 19, tzinfo=UTC).isoformat(),
        "oos_results": {
            "horizons": {
                "SWING_5D": {
                    "configuration": {
                        "name": "legacy_default",
                        "scoring_model": "legacy",
                    },
                    "train": {
                        "metrics": asdict(result.metrics),
                        "breakdowns": result.breakdowns,
                    },
                    "validation": {
                        "metrics": asdict(result.metrics),
                        "breakdowns": result.breakdowns,
                    },
                    "test": {
                        "metrics": asdict(result.metrics),
                        "breakdowns": result.breakdowns,
                    },
                    "top_tickers": [],
                    "worst_tickers": [],
                }
            }
        },
    }

    report = render_markdown_report(payload)

    assert "OUT-OF-SAMPLE" in report
    assert "Commission" in report
    assert "Slippage" in report
    assert "no post-hoc TEST optimization" in report


def test_confidence_assessment_requires_enough_activated_ideas_per_bucket() -> None:
    assessment = _confidence_assessment(
        {
            "60-69": {"activated": 100, "expectancy_r": 0.1},
            "70-79": {"activated": 12, "expectancy_r": 0.2},
        }
    )

    assert assessment.startswith("Insufficient")
