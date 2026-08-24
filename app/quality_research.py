from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.backtest import BacktestResult
from app.config import Settings
from app.domain import IdeaHorizon, QualityGateDecision, TradingIdeaData
from app.horizons import get_horizon_profile
from app.quality import QualityGate
from app.research_eval import (
    StrategyConfiguration,
    chronological_splits,
    objective_score,
    stability_score,
)
from app.research_runner import ResearchInstrument, ResearchRunner, result_payload

logger = logging.getLogger(__name__)


def _production_configuration(settings: Settings) -> StrategyConfiguration:
    return StrategyConfiguration(
        name="production_v1_quant",
        scoring_model=settings.technical_scoring_model,
        signal_threshold=settings.signal_threshold,
        minimum_confidence=settings.idea_minimum_confidence,
        minimum_reward_risk_ratio=settings.minimum_reward_risk_ratio,
        trend_weight=settings.score_weight_trend,
        momentum_weight=settings.score_weight_momentum,
        macd_weight=settings.score_weight_macd,
        bollinger_weight=settings.score_weight_bollinger,
        volume_weight=settings.score_weight_volume,
    )


def _quality_filter_factory(settings: Settings):
    gate = QualityGate(settings)

    def factory(_instrument: ResearchInstrument):
        def accepted(candidate: TradingIdeaData) -> bool:
            # The research universe is preselected for liquidity. Use the configured
            # eligibility floor when historical turnover snapshots are unavailable.
            candidate.daily_turnover = settings.quality_min_daily_turnover
            return gate.evaluate(candidate).decision == QualityGateDecision.PASS

        return accepted

    return factory


async def run_quality_comparison(
    runner: ResearchRunner,
    settings: Settings,
    *,
    tickers: tuple[str, ...] | None = None,
    horizons: tuple[IdeaHorizon, ...] = (
        IdeaHorizon.SWING_5D,
        IdeaHorizon.POSITION_1M,
        IdeaHorizon.INTRADAY_1D,
    ),
) -> dict[str, object]:
    selected_tickers = tickers or runner.config.tickers
    configuration = _production_configuration(settings)
    result: dict[str, object] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "dataset": runner.config.name,
        "strategy_version": settings.strategy_version,
        "selection_rule": (
            "For published SWING/POSITION, MIN_CONFIRMATIONS in [3,4,5]: TRAIN "
            "objective shortlist, VALIDATION stability selection. INTRADAY remains "
            "research-only and uses the declared default. OOS TEST is excluded from "
            "all selection."
        ),
        "limitations": [
            "AI verdict is not replayed historically; doing so now would not reproduce "
            "the future live model state.",
            "Top-N and cross-ticker cooldown are forward batch controls and are not "
            "simulated by the per-instrument backtest.",
            "Historical daily-turnover snapshots are unavailable; the fixed liquid "
            "research universe is treated as meeting the configured liquidity floor.",
        ],
        "horizons": {},
    }
    horizon_payload = result["horizons"]
    assert isinstance(horizon_payload, dict)
    for horizon in horizons:
        logger.info("Loading V2 QualityGate research data for %s", horizon.value)
        instruments = await runner._load_horizon(horizon, selected_tickers)
        profile = get_horizon_profile(horizon)
        start = datetime.combine(
            runner.config.start_dates[profile.primary_timeframe], datetime.min.time(), UTC
        )
        last = max(
            candle.end
            for instrument in instruments
            for candle in instrument.candles_by_timeframe[profile.primary_timeframe]
        )
        splits = chronological_splits(
            start,
            last + timedelta(microseconds=1),
            train_fraction=runner.config.evaluation.train_fraction,
            validation_fraction=runner.config.evaluation.validation_fraction,
            test_fraction=runner.config.evaluation.test_fraction,
        )
        calibration: list[tuple[int, BacktestResult, BacktestResult]] = []
        confirmation_field = {
            IdeaHorizon.INTRADAY_1D: "intraday_quality_min_confirmations",
            IdeaHorizon.SWING_5D: "swing_quality_min_confirmations",
            IdeaHorizon.POSITION_1M: "position_quality_min_confirmations",
        }[horizon]
        if horizon == IdeaHorizon.INTRADAY_1D:
            selected_n = settings.minimum_confirmations(horizon)
            selected_train = selected_validation = None
        else:
            for confirmations in (3, 4, 5):
                logger.info(
                    "Calibrating %s with MIN_CONFIRMATIONS=%s",
                    horizon.value,
                    confirmations,
                )
                candidate_settings = settings.model_copy(update={confirmation_field: confirmations})
                factory = _quality_filter_factory(candidate_settings)
                train = runner._evaluate(
                    configuration,
                    horizon,
                    instruments,
                    splits.train,
                    candidate_filter_factory=factory,
                    include_diagnostic_market_context=True,
                )
                validation = runner._evaluate(
                    configuration,
                    horizon,
                    instruments,
                    splits.validation,
                    candidate_filter_factory=factory,
                    include_diagnostic_market_context=True,
                )
                calibration.append((confirmations, train, validation))
            shortlist = sorted(
                calibration,
                key=lambda item: objective_score(item[1]),
                reverse=True,
            )[:2]
            selected_n, selected_train, selected_validation = max(
                shortlist,
                key=lambda item: stability_score(item[1], item[2]),
            )
        selected_settings = settings.model_copy(update={confirmation_field: selected_n})
        baseline_test = runner._evaluate(
            configuration,
            horizon,
            instruments,
            splits.test,
            include_diagnostic_market_context=True,
        )
        quality_test = runner._evaluate(
            configuration,
            horizon,
            instruments,
            splits.test,
            candidate_filter_factory=_quality_filter_factory(selected_settings),
            include_diagnostic_market_context=True,
        )
        logger.info(
            "Completed OOS comparison for %s: before=%s after=%s",
            horizon.value,
            baseline_test.metrics.idea_count,
            quality_test.metrics.idea_count,
        )
        before = baseline_test.metrics.idea_count
        after = quality_test.metrics.idea_count
        horizon_payload[horizon.value] = {
            "selected_min_confirmations": selected_n,
            "calibration": [
                {
                    "min_confirmations": value,
                    "train_objective": objective_score(train),
                    "validation_stability": stability_score(train, validation),
                    "train": result_payload(train),
                    "validation": result_payload(validation),
                }
                for value, train, validation in calibration
            ],
            "oos_test": {
                "before_v1_quant": result_payload(baseline_test),
                "after_v2_quality_gate": result_payload(quality_test),
                "signal_volume_before": before,
                "signal_volume_after": after,
                "signal_reduction_pct": round((1 - after / before) * 100, 2) if before else 0,
            },
            "selected_train": (
                result_payload(selected_train) if selected_train is not None else None
            ),
            "selected_validation": (
                result_payload(selected_validation) if selected_validation is not None else None
            ),
        }
    return result


def render_quality_comparison(payload: dict[str, object]) -> str:
    def number(value: object, decimals: int = 2) -> str:
        return "n/a" if value is None else f"{float(value):.{decimals}f}"

    lines = [
        "# V2 QualityGate historical comparison",
        "",
        f"Dataset: `{payload['dataset']}`",
        f"Strategy version: `{payload['strategy_version']}`",
        "",
        "AI is intentionally excluded from historical replay. Its incremental value is "
        "measured prospectively through the candidate experiment cohorts.",
        "",
        "| Horizon | Confirmations | Before | After | Reduction | WR before | WR after | "
        "PF before | PF after |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    horizons = payload["horizons"]
    assert isinstance(horizons, dict)
    for horizon, row in horizons.items():
        assert isinstance(row, dict)
        oos = row["oos_test"]
        before = oos["before_v1_quant"]["metrics"]
        after = oos["after_v2_quality_gate"]["metrics"]
        lines.append(
            f"| {horizon} | {row['selected_min_confirmations']} | "
            f"{oos['signal_volume_before']} | {oos['signal_volume_after']} | "
            f"{float(oos['signal_reduction_pct']):.2f}% | {number(before['win_rate'])}% | "
            f"{number(after['win_rate'])}% | {number(before['profit_factor'], 3)} | "
            f"{number(after['profit_factor'], 3)} |"
        )
    lines.extend(["", "## Limitations", ""])
    lines.extend(f"- {item}" for item in payload["limitations"])
    return "\n".join(lines) + "\n"


def write_quality_comparison(output_dir: Path, payload: dict[str, object]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "v2_quality_comparison.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    (output_dir / "V2_QUALITY_COMPARISON.md").write_text(
        render_quality_comparison(payload), encoding="utf-8"
    )
