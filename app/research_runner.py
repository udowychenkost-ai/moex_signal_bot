from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis import TechnicalFeatures
from app.backtest import BacktestEngine, BacktestResult, HistoryIndex
from app.config import Settings
from app.domain import CandleData, IdeaHorizon, MarketContextData, TradingIdeaData
from app.horizons import get_horizon_profile
from app.models import FundamentalReport, Instrument
from app.observation import completed_candles
from app.repositories import get_candles_range, get_market_candles_range
from app.research_baselines import run_long_baseline
from app.research_data import ResearchDatasetConfig
from app.research_eval import (
    DatasetSplits,
    EvaluationSplit,
    StrategyConfiguration,
    ablation_candidates,
    aggregate_results,
    calibration_candidates,
    chronological_splits,
    objective_score,
    stability_score,
    walk_forward_splits,
)
from app.research_features import ResearchMarketContextSeries, ResearchTechnicalSeries

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ResearchInstrument:
    ticker: str
    name: str
    lot_size: int
    candles_by_timeframe: dict[str, list[CandleData]]
    features_by_timeframe: dict[str, ResearchTechnicalSeries]
    indexes_by_timeframe: dict[str, HistoryIndex]
    benchmark_by_timeframe: dict[str, list[object]]
    market_context_by_timeframe: dict[str, ResearchMarketContextSeries]

    def provide_features(
        self,
        _ticker: str,
        timeframe: str,
        history: list[object],
    ) -> TechnicalFeatures:
        return self.features_by_timeframe[timeframe].at_end(history[-1].end)

    def provide_market_context(
        self,
        _ticker: str,
        timeframe: str,
        history: list[object],
        decision_at: datetime,
    ) -> MarketContextData:
        del decision_at
        series = self.market_context_by_timeframe.get(timeframe)
        if series is None:
            series = ResearchMarketContextSeries(
                self.benchmark_by_timeframe[timeframe],
                self.candles_by_timeframe[timeframe],
                timeframe,
            )
            self.market_context_by_timeframe[timeframe] = series
        return series.at_end(history[-1].end)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _json_safe(value: object) -> object:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def result_payload(result: BacktestResult) -> dict[str, object]:
    return _json_safe(
        {
            "metrics": asdict(result.metrics),
            "breakdowns": result.breakdowns,
        }
    )


def _ranked_tickers(result: BacktestResult, *, reverse: bool) -> list[dict[str, object]]:
    rows = [
        {"ticker": ticker, **summary}
        for ticker, summary in result.breakdowns.get("ticker", {}).items()
    ]
    return sorted(rows, key=lambda row: float(row["net_pnl"]), reverse=reverse)[:5]


def _configuration_row(
    configuration: StrategyConfiguration,
    result: BacktestResult,
    *,
    score: float,
) -> dict[str, object]:
    return {
        "configuration": configuration.as_dict(),
        "selection_score": score,
        "result": result_payload(result),
    }


class ResearchRunner:
    def __init__(
        self,
        settings: Settings,
        config: ResearchDatasetConfig,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self.settings = settings
        self.config = config
        self.session_factory = session_factory
        self._feature_validation: dict[str, dict[str, object]] = {}

    def _build_feature_series(
        self,
        horizon: IdeaHorizon,
        ticker: str,
        histories: dict[str, list[CandleData]],
    ) -> dict[str, ResearchTechnicalSeries]:
        result: dict[str, ResearchTechnicalSeries] = {}
        horizon_validation = self._feature_validation.setdefault(horizon.value, {})
        for timeframe, candles in histories.items():
            if not candles:
                continue
            series = ResearchTechnicalSeries(candles)
            validation = series.compare_canonical(samples=3)
            horizon_validation[f"{ticker}:{timeframe}"] = validation
            if not validation["passed"]:
                raise RuntimeError(
                    f"research feature fast path diverged for {ticker} {timeframe}: {validation}"
                )
            result[timeframe] = series
        return result

    async def _load_horizon(
        self,
        horizon: IdeaHorizon,
        tickers: tuple[str, ...],
    ) -> list[ResearchInstrument]:
        profile = get_horizon_profile(horizon)
        result: list[ResearchInstrument] = []
        async with self.session_factory() as session:
            benchmark_histories = {
                timeframe: completed_candles(
                    list(await get_market_candles_range(session, "IMOEX", timeframe)),
                    timeframe,
                )
                for timeframe in profile.timeframe_weights
            }
            for ticker in tickers:
                instrument = await session.get(Instrument, ticker)
                if instrument is None:
                    logger.warning("Research ticker %s is absent from the dataset", ticker)
                    continue
                histories: dict[str, list[CandleData]] = {}
                for timeframe in profile.timeframe_weights:
                    rows = completed_candles(
                        list(await get_candles_range(session, ticker, timeframe)),
                        timeframe,
                    )
                    histories[timeframe] = [
                        CandleData(
                            secid=row.secid,
                            board_id=row.board_id,
                            timeframe=row.timeframe,
                            begin=_utc(row.begin),
                            end=_utc(row.end),
                            open=row.open,
                            high=row.high,
                            low=row.low,
                            close=row.close,
                            volume=row.volume,
                            value=row.value,
                        )
                        for row in rows
                    ]
                if histories.get(profile.primary_timeframe):
                    result.append(
                        ResearchInstrument(
                            ticker=ticker,
                            name=instrument.short_name,
                            lot_size=max(1, instrument.lot_size or 1),
                            candles_by_timeframe=histories,
                            features_by_timeframe=self._build_feature_series(
                                horizon,
                                ticker,
                                histories,
                            ),
                            indexes_by_timeframe={
                                timeframe: HistoryIndex.build(candles)
                                for timeframe, candles in histories.items()
                            },
                            benchmark_by_timeframe=benchmark_histories,
                            market_context_by_timeframe={},
                        )
                    )
        return result

    def _evaluate(
        self,
        configuration: StrategyConfiguration,
        horizon: IdeaHorizon,
        instruments: list[ResearchInstrument],
        split: EvaluationSplit,
        candidate_filter_factory: (
            Callable[[ResearchInstrument], Callable[[TradingIdeaData], bool]] | None
        ) = None,
        include_diagnostic_market_context: bool = False,
    ) -> BacktestResult:
        if not instruments:
            raise RuntimeError(f"no research instruments available for {horizon.value}")
        allocation = self.settings.paper_account_size / len(instruments)
        research_settings = configuration.settings(self.settings)
        profile = configuration.profile(get_horizon_profile(horizon))
        contextual = configuration.scoring_model == "contextual"

        def evaluate_instrument(instrument: ResearchInstrument) -> BacktestResult:
            return BacktestEngine(research_settings).run(
                ticker=instrument.ticker,
                instrument_name=instrument.name,
                horizon=horizon,
                candles_by_timeframe=instrument.candles_by_timeframe,
                lot_size=instrument.lot_size,
                initial_equity=allocation,
                start_at=split.start,
                end_at=split.end,
                profile=profile,
                feature_provider=instrument.provide_features,
                prepared_indexes=instrument.indexes_by_timeframe,
                market_context_provider=(
                    instrument.provide_market_context
                    if contextual or include_diagnostic_market_context
                    else None
                ),
                candidate_filter=(
                    candidate_filter_factory(instrument)
                    if candidate_filter_factory is not None
                    else None
                ),
            )

        worker_count = min(self.settings.research_workers, len(instruments))
        if worker_count == 1:
            results = [evaluate_instrument(instrument) for instrument in instruments]
        else:
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                results = list(executor.map(evaluate_instrument, instruments))
        return aggregate_results(results, initial_equity=self.settings.paper_account_size)

    def _simple_baselines(
        self,
        horizon: IdeaHorizon,
        instruments: list[ResearchInstrument],
        split: EvaluationSplit,
    ) -> dict[str, object]:
        allocation = self.settings.paper_account_size / len(instruments)
        profile = get_horizon_profile(horizon)
        output: dict[str, object] = {}
        for strategy in ("buy_hold", "ema_trend", "rsi_reversion"):
            results = [
                run_long_baseline(
                    self.settings,
                    strategy=strategy,
                    ticker=instrument.ticker,
                    horizon=horizon,
                    candles=instrument.candles_by_timeframe[profile.primary_timeframe],
                    lot_size=instrument.lot_size,
                    initial_equity=allocation,
                    start_at=split.start,
                    end_at=split.end,
                )
                for instrument in instruments
            ]
            output[strategy] = result_payload(
                aggregate_results(results, initial_equity=self.settings.paper_account_size)
            )
        return output

    def _eligibility(
        self,
        instruments: list[ResearchInstrument],
        horizon: IdeaHorizon,
        splits: DatasetSplits,
    ) -> dict[str, dict[str, int | bool]]:
        primary = get_horizon_profile(horizon).primary_timeframe
        minimum = self.config.evaluation.minimum_primary_candles_per_split
        result: dict[str, dict[str, int | bool]] = {}
        for instrument in instruments:
            candles = instrument.candles_by_timeframe[primary]
            counts = {
                split.name: sum(
                    _utc(split.start) <= _utc(candle.end) < _utc(split.end) for candle in candles
                )
                for split in (splits.train, splits.validation, splits.test)
            }
            result[instrument.ticker] = {
                **counts,
                "meets_minimum_all_splits": all(count >= minimum for count in counts.values()),
            }
        return result

    def _walk_forward(
        self,
        horizon: IdeaHorizon,
        instruments: list[ResearchInstrument],
        folds: list[tuple[EvaluationSplit, EvaluationSplit]],
        candidates: list[StrategyConfiguration],
    ) -> dict[str, object]:
        rows: list[dict[str, object]] = []
        unseen_results: list[BacktestResult] = []
        for train, unseen in folds:
            train_results = [
                (candidate, self._evaluate(candidate, horizon, instruments, train))
                for candidate in candidates
            ]
            selected, selected_train = max(
                train_results,
                key=lambda item: objective_score(item[1]),
            )
            unseen_result = self._evaluate(selected, horizon, instruments, unseen)
            unseen_results.append(unseen_result)
            rows.append(
                {
                    "train": {"start": train.start.isoformat(), "end": train.end.isoformat()},
                    "unseen": {
                        "start": unseen.start.isoformat(),
                        "end": unseen.end.isoformat(),
                    },
                    "selected_configuration": selected.as_dict(),
                    "train_selection_score": objective_score(selected_train),
                    "unseen_result": result_payload(unseen_result),
                }
            )
        return {
            "declared_candidates": [candidate.as_dict() for candidate in candidates],
            "folds": rows,
            "aggregate_unseen": result_payload(
                aggregate_results(
                    unseen_results,
                    initial_equity=self.settings.paper_account_size,
                )
            ),
        }

    async def run(
        self,
        *,
        tickers: tuple[str, ...] | None = None,
        horizons: tuple[IdeaHorizon, ...] | None = None,
    ) -> dict[str, object]:
        selected_tickers = tickers or self.config.tickers
        selected_horizons = horizons or tuple(IdeaHorizon)
        candidates = calibration_candidates()
        legacy = next(item for item in candidates if item.name == "legacy_default")
        weighted = next(item for item in candidates if item.name == "weighted_default")
        baseline_payloads: dict[str, dict[str, object]] = {
            "legacy": {"configuration": legacy.as_dict(), "horizons": {}},
            "weighted": {"configuration": weighted.as_dict(), "horizons": {}},
        }
        calibration: dict[str, object] = {
            "declared_grid": [item.as_dict() for item in candidates],
            "horizons": {},
        }
        oos: dict[str, object] = {"horizons": {}}
        walk_forward: dict[str, object] = {"horizons": {}}
        simple_baselines: dict[str, object] = {"horizons": {}}
        split_payload: dict[str, object] = {}

        for horizon in selected_horizons:
            logger.info("Loading research dataset for %s", horizon.value)
            instruments = await self._load_horizon(horizon, selected_tickers)
            primary = get_horizon_profile(horizon).primary_timeframe
            start = datetime.combine(self.config.start_dates[primary], datetime.min.time(), UTC)
            last = max(
                _utc(candle.end)
                for instrument in instruments
                for candle in instrument.candles_by_timeframe[primary]
            )
            # Include the final candle while retaining [start, end) semantics.
            dataset_end = last + timedelta(microseconds=1)
            splits = chronological_splits(
                start,
                dataset_end,
                train_fraction=self.config.evaluation.train_fraction,
                validation_fraction=self.config.evaluation.validation_fraction,
                test_fraction=self.config.evaluation.test_fraction,
            )
            split_payload[horizon.value] = {
                "primary_timeframe": primary,
                "boundaries": splits.as_dict(),
                "available_tickers": [item.ticker for item in instruments],
                "eligibility": self._eligibility(instruments, horizon, splits),
            }

            cache: dict[tuple[str, str], BacktestResult] = {}

            def evaluated(
                configuration: StrategyConfiguration,
                split: EvaluationSplit,
                *,
                _cache: dict[tuple[str, str], BacktestResult] = cache,
                _horizon: IdeaHorizon = horizon,
                _instruments: list[ResearchInstrument] = instruments,
            ) -> BacktestResult:
                key = (configuration.name, split.name)
                if key not in _cache:
                    logger.info(
                        "Evaluating %s / %s / %s",
                        _horizon.value,
                        configuration.name,
                        split.name,
                    )
                    _cache[key] = self._evaluate(
                        configuration,
                        _horizon,
                        _instruments,
                        split,
                    )
                return _cache[key]

            train_rows = [(item, evaluated(item, splits.train)) for item in candidates]
            for model_name, baseline in (("legacy", legacy), ("weighted", weighted)):
                baseline_payloads[model_name]["horizons"][horizon.value] = {
                    split.name: result_payload(evaluated(baseline, split))
                    for split in (splits.train, splits.validation, splits.test)
                }
            top_train = sorted(
                train_rows,
                key=lambda item: objective_score(item[1]),
                reverse=True,
            )[:3]
            validation_rows = [
                (item, train_result, evaluated(item, splits.validation))
                for item, train_result in top_train
            ]
            selected, selected_train, selected_validation = max(
                validation_rows,
                key=lambda item: stability_score(item[1], item[2]),
            )
            selected_test = evaluated(selected, splits.test)
            calibration["horizons"][horizon.value] = {
                "train_candidates": [
                    _configuration_row(item, result, score=objective_score(result))
                    for item, result in train_rows
                ],
                "validation_finalists": [
                    _configuration_row(
                        item,
                        validation_result,
                        score=stability_score(train_result, validation_result),
                    )
                    for item, train_result, validation_result in validation_rows
                ],
                "selected_configuration": selected.as_dict(),
                "selection_rule": (
                    "TRAIN objective shortlist (top 3), then VALIDATION stability; "
                    "TEST excluded from selection"
                ),
            }
            oos["horizons"][horizon.value] = {
                "configuration": selected.as_dict(),
                "train": result_payload(selected_train),
                "validation": result_payload(selected_validation),
                "test": result_payload(selected_test),
                "top_tickers": _json_safe(_ranked_tickers(selected_test, reverse=True)),
                "worst_tickers": _json_safe(_ranked_tickers(selected_test, reverse=False)),
            }
            simple_baselines["horizons"][horizon.value] = self._simple_baselines(
                horizon,
                instruments,
                splits.test,
            )
            folds = walk_forward_splits(
                start,
                dataset_end,
                folds=self.config.evaluation.walk_forward_folds,
            )
            walk_forward["horizons"][horizon.value] = self._walk_forward(
                horizon,
                instruments,
                folds,
                [legacy, weighted],
            )

        calibration["batch_feature_validation"] = self._feature_validation

        return _json_safe(
            {
                "generated_at": datetime.now(UTC),
                "dataset": self.config.name,
                "universe": list(selected_tickers),
                "selection_method": self.config.selection_method,
                "survivorship_bias_warning": (
                    self.config.selection_method != "point_in_time_universe"
                ),
                "prices_adjusted_for_corporate_actions": (
                    self.config.prices_adjusted_for_corporate_actions
                ),
                "splits": split_payload,
                "baseline_legacy": baseline_payloads["legacy"],
                "baseline_weighted": baseline_payloads["weighted"],
                "calibration_summary": calibration,
                "oos_results": oos,
                "research_baselines": simple_baselines,
                "walk_forward": walk_forward,
            }
        )

    async def run_ablation(
        self,
        *,
        tickers: tuple[str, ...] | None = None,
        horizons: tuple[IdeaHorizon, ...] = (
            IdeaHorizon.POSITION_1M,
            IdeaHorizon.SWING_5D,
        ),
    ) -> dict[str, object]:
        selected_tickers = tickers or self.config.tickers
        candidates = ablation_candidates()
        async with self.session_factory() as session:
            fundamental_tickers = int(
                await session.scalar(
                    select(func.count(func.distinct(FundamentalReport.ticker))).where(
                        FundamentalReport.ticker.in_(selected_tickers)
                    )
                )
                or 0
            )
        payload: dict[str, object] = {
            "generated_at": datetime.now(UTC),
            "dataset": self.config.name,
            "universe": list(selected_tickers),
            "fundamental_coverage": {
                "covered_tickers": fundamental_tickers,
                "requested_tickers": len(selected_tickers),
                "coverage_pct": round(fundamental_tickers / len(selected_tickers) * 100, 2),
            },
            "variants": {
                "A": "current technical baseline_v1",
                "B": "contextual technical + IMOEX regime; volume/RS/extreme ablated",
                "C": "contextual technical + improved volume; regime/RS/extreme ablated",
                "D": "not_evaluable_without_point_in_time_fundamentals",
                "E": "not_evaluable_without_point_in_time_fundamentals",
                "F": (
                    "contextual full available market model; fundamental leg is not "
                    "claimable when coverage is zero"
                ),
            },
            "horizons": {},
        }
        horizon_payload = payload["horizons"]
        assert isinstance(horizon_payload, dict)
        for horizon in horizons:
            logger.info("Loading ablation dataset for %s", horizon.value)
            instruments = await self._load_horizon(horizon, selected_tickers)
            primary = get_horizon_profile(horizon).primary_timeframe
            start = datetime.combine(self.config.start_dates[primary], datetime.min.time(), UTC)
            last = max(
                _utc(candle.end)
                for instrument in instruments
                for candle in instrument.candles_by_timeframe[primary]
            )
            end = last + timedelta(microseconds=1)
            splits = chronological_splits(
                start,
                end,
                train_fraction=self.config.evaluation.train_fraction,
                validation_fraction=self.config.evaluation.validation_fraction,
                test_fraction=self.config.evaluation.test_fraction,
            )
            folds = walk_forward_splits(
                start,
                end,
                folds=self.config.evaluation.walk_forward_folds,
            )
            variants: dict[str, object] = {}
            for configuration in candidates:
                logger.info("Ablation %s / %s", horizon.value, configuration.name)
                test = self._evaluate(configuration, horizon, instruments, splits.test)
                unseen = [
                    self._evaluate(configuration, horizon, instruments, split) for _, split in folds
                ]
                variants[configuration.name] = {
                    "configuration": configuration.as_dict(),
                    "oos_test": result_payload(test),
                    "walk_forward_unseen": result_payload(
                        aggregate_results(
                            unseen,
                            initial_equity=self.settings.paper_account_size,
                        )
                    ),
                }
            horizon_payload[horizon.value] = {
                "splits": splits.as_dict(),
                "available_tickers": [item.ticker for item in instruments],
                "results": variants,
                "selection_note": (
                    "fixed variants; TRAIN/VALIDATION are declared but not replayed "
                    "because no parameter selection occurs in this ablation"
                ),
                "fundamental_variants": {
                    "D_technical_fundamental": "not_evaluable",
                    "E_regime_fundamental": "not_evaluable",
                },
            }
        return _json_safe(payload)


def write_research_outputs(output_dir: Path, payload: dict[str, object]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for key in (
        "baseline_legacy",
        "baseline_weighted",
        "calibration_summary",
        "oos_results",
        "research_baselines",
        "walk_forward",
    ):
        (output_dir / f"{key}.json").write_text(
            json.dumps(payload[key], ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
    (output_dir / "BACKTEST_REPORT.md").write_text(
        render_markdown_report(payload),
        encoding="utf-8",
    )


def _number(value: Any, decimals: int = 2) -> str:
    return "n/a" if value is None else f"{float(value):.{decimals}f}"


def _append_model_comparison(
    lines: list[str],
    payload: dict[str, object],
    horizon: str,
) -> None:
    legacy = payload.get("baseline_legacy", {}).get("horizons", {}).get(horizon)
    weighted = payload.get("baseline_weighted", {}).get("horizons", {}).get(horizon)
    if not legacy or not weighted:
        return
    lines.extend(
        [
            "### Fixed legacy vs weighted baselines",
            "",
            "| Period | Model | Ideas | Activated | Win rate | Profit factor | "
            "Expectancy R | Max DD | Net P&L |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for period in ("train", "validation", "test"):
        for model, source in (("legacy", legacy), ("weighted", weighted)):
            metrics = source[period]["metrics"]
            lines.append(
                f"| {period.upper()} | {model} | {metrics['idea_count']} | "
                f"{metrics['activated_count']} | {_number(metrics['win_rate'])}% | "
                f"{_number(metrics['profit_factor'])} | "
                f"{_number(metrics['expectancy_r'])} | "
                f"{_number(metrics['maximum_drawdown_pct'])}% | "
                f"{_number(metrics['net_pnl'])} |"
            )
    lines.append("")


def _append_simple_baselines(
    lines: list[str],
    payload: dict[str, object],
    horizon: str,
) -> None:
    strategies = payload.get("research_baselines", {}).get("horizons", {}).get(horizon)
    if not strategies:
        return
    lines.extend(
        [
            "### OOS research-only benchmarks",
            "",
            "| Strategy | Trades | Win rate | Profit factor | Net P&L | Max DD |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for strategy, result in strategies.items():
        metrics = result["metrics"]
        lines.append(
            f"| {strategy} | {metrics['activated_count']} | "
            f"{_number(metrics['win_rate'])}% | {_number(metrics['profit_factor'])} | "
            f"{_number(metrics['net_pnl'])} | "
            f"{_number(metrics['maximum_drawdown_pct'])}% |"
        )
    lines.append("")


def _confidence_assessment(breakdown: dict[str, dict[str, Any]]) -> str:
    def lower_bound(bucket: str) -> int:
        return 90 if bucket == "90+" else int(bucket.split("-", maxsplit=1)[0])

    populated = [
        (bucket, summary) for bucket, summary in breakdown.items() if int(summary["activated"]) > 0
    ]
    if len(populated) < 2 or any(int(summary["activated"]) < 30 for _, summary in populated):
        return (
            "Insufficient activated ideas per confidence bucket to assess "
            "confidence ordering reliably."
        )
    usable = sorted(
        ((lower_bound(bucket), float(summary["expectancy_r"])) for bucket, summary in populated),
        key=lambda item: item[0],
    )
    monotonic = all(right[1] >= left[1] for left, right in zip(usable, usable[1:], strict=False))
    if monotonic:
        return "OOS expectancy is non-decreasing across populated confidence buckets."
    return (
        "WARNING: OOS expectancy is not monotonic across confidence buckets; "
        "confidence is not calibrated as a reliable quality rank."
    )


def _research_verdict(metrics: dict[str, Any], walk_metrics: dict[str, Any] | None) -> str:
    profit_factor = float(metrics["profit_factor"] or 0)
    expectancy = float(metrics["expectancy_r"])
    if profit_factor < 1 or expectancy <= 0:
        return "REJECT: no positive OOS expectancy after execution costs."
    if walk_metrics is not None and (
        float(walk_metrics["profit_factor"] or 0) < 1 or float(walk_metrics["expectancy_r"]) <= 0
    ):
        return "UNSTABLE: positive single OOS result is not confirmed by walk-forward."
    if profit_factor < 1.1 or expectancy < 0.05:
        return "BORDERLINE: positive edge is too small for a strong readiness claim."
    return "MODEST POSITIVE EDGE: suitable for forward paper validation, not real capital."


def render_markdown_report(payload: dict[str, object]) -> str:
    lines = [
        "# Backtest validation and calibration report",
        "",
        f"Generated: {payload['generated_at']}",
        "",
        "## Executive verdict",
        "",
        "| Horizon | Selected | OOS PF | OOS expectancy | OOS net P&L | Verdict |",
        "|---|---|---:|---:|---:|---|",
    ]
    for horizon, row in payload["oos_results"]["horizons"].items():
        metrics = row["test"]["metrics"]
        walk = payload.get("walk_forward", {}).get("horizons", {}).get(horizon)
        walk_metrics = walk["aggregate_unseen"]["metrics"] if walk else None
        lines.append(
            f"| {horizon} | {row['configuration']['name']} | "
            f"{_number(metrics['profit_factor'])} | {_number(metrics['expectancy_r'])} R | "
            f"{_number(metrics['net_pnl'])} RUB | "
            f"{_research_verdict(metrics, walk_metrics)} |"
        )
    lines.extend(
        [
            "",
            "## Method and limitations",
            "",
            (
                "- Signals are formed only from candles closed at the decision time and "
                "execute no earlier than the next primary candle."
            ),
            (
                "- TRAIN selects a shortlist; VALIDATION selects the configuration; "
                "OOS TEST is not used for parameter selection."
            ),
            (
                "- Each walk-forward fold recalibrates between the predeclared fixed "
                "legacy and weighted defaults using only that fold's past."
            ),
            "- The universe is a fixed current-liquid universe, so survivorship bias remains.",
            (
                "- MOEX candles are not adjusted for corporate actions; historical returns "
                "around splits/dividends can be distorted."
            ),
            (
                "- Sharpe is trade-level and non-annualized. Same-candle TP/SL ambiguity "
                "is resolved conservatively as SL first."
            ),
            (
                "- Maximum drawdown uses realized equity after trade closes, not "
                "intratrade mark-to-market, and can understate adverse excursion."
            ),
            (
                "- Research uses causal batch indicator features for runtime. Every "
                "ticker/timeframe is sampled against the canonical 500-candle analyzer; "
                "the run aborts on score, level, or material numeric divergence."
            ),
            "",
        ]
    )
    split_rows = payload.get("splits", {})
    if split_rows:
        lines.extend(
            [
                "## Dataset splits",
                "",
                "| Horizon | Primary | TRAIN | VALIDATION | OOS TEST | Tickers |",
                "|---|---|---|---|---|---:|",
            ]
        )
        for horizon, row in split_rows.items():
            boundaries = row["boundaries"]
            lines.append(
                f"| {horizon} | {row['primary_timeframe']} | "
                f"{boundaries['train']['start']} → {boundaries['train']['end']} | "
                f"{boundaries['validation']['start']} → "
                f"{boundaries['validation']['end']} | "
                f"{boundaries['test']['start']} → {boundaries['test']['end']} | "
                f"{len(row['available_tickers'])} |"
            )
        lines.append("")
    oos_horizons = payload["oos_results"]["horizons"]
    for horizon, row in oos_horizons.items():
        metrics = row["test"]["metrics"]
        walk = payload.get("walk_forward", {}).get("horizons", {}).get(horizon)
        walk_metrics = walk["aggregate_unseen"]["metrics"] if walk else None
        lines.extend(
            [
                f"## {horizon} — OUT-OF-SAMPLE",
                "",
                (
                    f"Selected configuration: `{row['configuration']['name']}` "
                    f"(`{row['configuration']['scoring_model']}`)"
                ),
                "",
                f"Verdict: **{_research_verdict(metrics, walk_metrics)}**",
                "",
                "| Metric | Result |",
                "|---|---:|",
                f"| Ideas | {metrics['idea_count']} |",
                f"| Activated | {metrics['activated_count']} |",
                f"| Activation rate | {_number(metrics['activation_rate'])}% |",
                (
                    "| TP / SL / expired / invalidated | "
                    f"{metrics['tp_hit_count']} / {metrics['sl_hit_count']} / "
                    f"{metrics['expired_count']} / {metrics['invalidated_count']} |"
                ),
                f"| Win rate | {_number(metrics['win_rate'])}% |",
                f"| Profit factor | {_number(metrics['profit_factor'])} |",
                f"| Expectancy | {_number(metrics['expectancy_r'])} R |",
                (
                    f"| Average / median R | {_number(metrics['average_r'])} / "
                    f"{_number(metrics['median_r'])} |"
                ),
                f"| Maximum drawdown | {_number(metrics['maximum_drawdown_pct'])}% |",
                f"| Sharpe | {_number(metrics['sharpe_ratio'])} |",
                f"| Average holding | {_number(metrics['average_holding_hours'])} h |",
                f"| Gross P&L | {_number(metrics['gross_pnl'])} RUB |",
                f"| Commission | {_number(metrics['commission'])} RUB |",
                f"| Slippage | {_number(metrics['slippage'])} RUB |",
                f"| Net P&L | {_number(metrics['net_pnl'])} RUB |",
                "",
            ]
        )
        _append_model_comparison(lines, payload, horizon)
        lines.extend(
            [
                "### Selected configuration stability",
                "",
                "| Period | Ideas | Activated | Profit factor | Expectancy R | Max DD | Net P&L |",
                "|---|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for period in ("train", "validation", "test"):
            period_metrics = row[period]["metrics"]
            lines.append(
                f"| {period.upper()} | {period_metrics['idea_count']} | "
                f"{period_metrics['activated_count']} | "
                f"{_number(period_metrics['profit_factor'])} | "
                f"{_number(period_metrics['expectancy_r'])} | "
                f"{_number(period_metrics['maximum_drawdown_pct'])}% | "
                f"{_number(period_metrics['net_pnl'])} |"
            )
        lines.append("")
        _append_simple_baselines(lines, payload, horizon)
        lines.extend(
            [
                "### Top / worst tickers",
                "",
                "| Top | Net P&L | Worst | Net P&L |",
                "|---|---:|---|---:|",
            ]
        )
        top = row["top_tickers"]
        worst = row["worst_tickers"]
        for index in range(max(len(top), len(worst))):
            good = top[index] if index < len(top) else {"ticker": "", "net_pnl": 0}
            bad = worst[index] if index < len(worst) else {"ticker": "", "net_pnl": 0}
            lines.append(
                f"| {good['ticker']} | {_number(good['net_pnl'])} | "
                f"{bad['ticker']} | {_number(bad['net_pnl'])} |"
            )
        lines.extend(
            [
                "",
                "### BUY vs SELL",
                "",
                "| Direction | Ideas | Activated | Win rate | Expectancy R | Net P&L |",
                "|---|---:|---:|---:|---:|---:|",
            ]
        )
        for direction, summary in row["test"]["breakdowns"].get("direction", {}).items():
            lines.append(
                f"| {direction} | {summary['ideas']} | {summary['activated']} | "
                f"{_number(summary['win_rate'])}% | "
                f"{_number(summary['expectancy_r'])} | {_number(summary['net_pnl'])} |"
            )
        lines.extend(
            [
                "",
                "### Confidence calibration",
                "",
                "| Bucket | Ideas | Activated | Win rate | Expectancy R | Profit factor |",
                "|---|---:|---:|---:|---:|---:|",
            ]
        )
        for bucket, summary in row["test"]["breakdowns"].get("confidence_bucket", {}).items():
            lines.append(
                f"| {bucket} | {summary['ideas']} | {summary['activated']} | "
                f"{_number(summary['win_rate'])}% | "
                f"{_number(summary['expectancy_r'])} | "
                f"{_number(summary['profit_factor'])} |"
            )
        confidence = row["test"]["breakdowns"].get("confidence_bucket", {})
        lines.extend(["", _confidence_assessment(confidence), ""])
        lines.extend(
            [
                "### Calendar-year OOS breakdown",
                "",
                "| Year | Ideas | Activated | Win rate | Expectancy R | Net P&L |",
                "|---|---:|---:|---:|---:|---:|",
            ]
        )
        for year, summary in row["test"]["breakdowns"].get("calendar_year", {}).items():
            lines.append(
                f"| {year} | {summary['ideas']} | {summary['activated']} | "
                f"{_number(summary['win_rate'])}% | "
                f"{_number(summary['expectancy_r'])} | {_number(summary['net_pnl'])} |"
            )
        if walk:
            lines.extend(
                [
                    "",
                    "### Walk-forward aggregate unseen windows",
                    "",
                    f"Folds: {len(walk['folds'])}; ideas: {walk_metrics['idea_count']}; "
                    f"activated: {walk_metrics['activated_count']}; profit factor: "
                    f"{_number(walk_metrics['profit_factor'])}; expectancy: "
                    f"{_number(walk_metrics['expectancy_r'])} R; net P&L: "
                    f"{_number(walk_metrics['net_pnl'])} RUB.",
                ]
            )
        lines.append("")
    lines.extend(
        [
            "## Interpretation guardrails",
            "",
            (
                "A profitable result is not proof of future performance. A negative or "
                "unstable OOS result is retained as-is; no post-hoc TEST optimization "
                "is performed."
            ),
            "",
        ]
    )
    return "\n".join(lines)
