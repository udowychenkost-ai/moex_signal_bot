from __future__ import annotations

import json
import logging
import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.backtest import BacktestEngine, BacktestResult
from app.config import Settings
from app.domain import CandleData, IdeaHorizon
from app.horizons import get_horizon_profile
from app.models import Instrument
from app.repositories import get_candles_range
from app.research_baselines import run_long_baseline
from app.research_data import ResearchDatasetConfig
from app.research_eval import (
    DatasetSplits,
    EvaluationSplit,
    StrategyConfiguration,
    aggregate_results,
    calibration_candidates,
    chronological_splits,
    objective_score,
    stability_score,
    walk_forward_splits,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ResearchInstrument:
    ticker: str
    name: str
    lot_size: int
    candles_by_timeframe: dict[str, list[CandleData]]


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

    async def _load_horizon(
        self,
        horizon: IdeaHorizon,
        tickers: tuple[str, ...],
    ) -> list[ResearchInstrument]:
        profile = get_horizon_profile(horizon)
        result: list[ResearchInstrument] = []
        async with self.session_factory() as session:
            for ticker in tickers:
                instrument = await session.get(Instrument, ticker)
                if instrument is None:
                    logger.warning("Research ticker %s is absent from the dataset", ticker)
                    continue
                histories: dict[str, list[CandleData]] = {}
                for timeframe in profile.timeframe_weights:
                    rows = await get_candles_range(session, ticker, timeframe)
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
                        )
                    )
        return result

    def _evaluate(
        self,
        configuration: StrategyConfiguration,
        horizon: IdeaHorizon,
        instruments: list[ResearchInstrument],
        split: EvaluationSplit,
    ) -> BacktestResult:
        if not instruments:
            raise RuntimeError(f"no research instruments available for {horizon.value}")
        allocation = self.settings.paper_account_size / len(instruments)
        research_settings = configuration.settings(self.settings)
        profile = configuration.profile(get_horizon_profile(horizon))
        results = []
        for instrument in instruments:
            results.append(
                BacktestEngine(research_settings).run(
                    ticker=instrument.ticker,
                    instrument_name=instrument.name,
                    horizon=horizon,
                    candles_by_timeframe=instrument.candles_by_timeframe,
                    lot_size=instrument.lot_size,
                    initial_equity=allocation,
                    start_at=split.start,
                    end_at=split.end,
                    profile=profile,
                )
            )
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

            for model_name, baseline in (("legacy", legacy), ("weighted", weighted)):
                baseline_payloads[model_name]["horizons"][horizon.value] = {
                    split.name: result_payload(evaluated(baseline, split))
                    for split in (splits.train, splits.validation, splits.test)
                }

            train_rows = [(item, evaluated(item, splits.train)) for item in candidates]
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
                candidates,
            )

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


def render_markdown_report(payload: dict[str, object]) -> str:
    lines = [
        "# Backtest validation and calibration report",
        "",
        f"Generated: {payload['generated_at']}",
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
        "- The universe is a fixed current-liquid universe, so survivorship bias remains.",
        (
            "- MOEX candles are not adjusted for corporate actions; historical returns "
            "around splits/dividends can be distorted."
        ),
        (
            "- Sharpe is trade-level and non-annualized. Same-candle TP/SL ambiguity "
            "is resolved conservatively as SL first."
        ),
        "",
    ]
    oos_horizons = payload["oos_results"]["horizons"]
    for horizon, row in oos_horizons.items():
        metrics = row["test"]["metrics"]
        lines.extend(
            [
                f"## {horizon} — OUT-OF-SAMPLE",
                "",
                (
                    f"Best configuration: `{row['configuration']['name']}` "
                    f"(`{row['configuration']['scoring_model']}`)"
                ),
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
