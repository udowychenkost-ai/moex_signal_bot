from __future__ import annotations

import asyncio
import logging
import tomllib
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain import InstrumentData
from app.ingestion import OVERLAP
from app.moex import SOURCE_INTERVALS, MoexClient
from app.repositories import (
    candle_coverage,
    latest_candle_begin,
    upsert_candles,
    upsert_instruments,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ResearchEvaluationConfig:
    train_fraction: float
    validation_fraction: float
    test_fraction: float
    minimum_primary_candles_per_split: int
    walk_forward_folds: int

    def __post_init__(self) -> None:
        total = self.train_fraction + self.validation_fraction + self.test_fraction
        if abs(total - 1.0) > 1e-9:
            raise ValueError("research split fractions must sum to 1")
        if min(self.train_fraction, self.validation_fraction, self.test_fraction) <= 0:
            raise ValueError("research split fractions must be positive")
        if self.minimum_primary_candles_per_split < 1 or self.walk_forward_folds < 1:
            raise ValueError("research candle minimum and fold count must be positive")


@dataclass(frozen=True, slots=True)
class ResearchDatasetConfig:
    name: str
    board_id: str
    tickers: tuple[str, ...]
    start_dates: dict[str, date]
    selection_method: str
    prices_adjusted_for_corporate_actions: bool
    evaluation: ResearchEvaluationConfig

    def __post_init__(self) -> None:
        if not self.tickers or len(set(self.tickers)) != len(self.tickers):
            raise ValueError("research tickers must be non-empty and unique")
        invalid = set(self.start_dates) - set(SOURCE_INTERVALS)
        if invalid:
            raise ValueError(f"unsupported research timeframes: {', '.join(sorted(invalid))}")


def load_research_config(path: str | Path) -> ResearchDatasetConfig:
    with Path(path).open("rb") as stream:
        payload = tomllib.load(stream)
    dataset = payload["dataset"]
    evaluation = payload["evaluation"]
    start_dates = {
        str(timeframe): value if isinstance(value, date) else date.fromisoformat(str(value))
        for timeframe, value in dataset["start_dates"].items()
    }
    return ResearchDatasetConfig(
        name=str(dataset["name"]),
        board_id=str(dataset.get("board_id", "TQBR")).upper(),
        tickers=tuple(str(ticker).upper() for ticker in dataset["tickers"]),
        start_dates=start_dates,
        selection_method=str(dataset["selection_method"]),
        prices_adjusted_for_corporate_actions=bool(
            dataset["prices_adjusted_for_corporate_actions"]
        ),
        evaluation=ResearchEvaluationConfig(
            train_fraction=float(evaluation["train_fraction"]),
            validation_fraction=float(evaluation["validation_fraction"]),
            test_fraction=float(evaluation["test_fraction"]),
            minimum_primary_candles_per_split=int(evaluation["minimum_primary_candles_per_split"]),
            walk_forward_folds=int(evaluation["walk_forward_folds"]),
        ),
    )


def _at_utc_start(value: date) -> datetime:
    return datetime(value.year, value.month, value.day, tzinfo=UTC)


def _iso(value: object) -> object:
    return value.isoformat() if isinstance(value, (date, datetime)) else value


class ResearchDataService:
    def __init__(
        self,
        config: ResearchDatasetConfig,
        session_factory: async_sessionmaker[AsyncSession],
        moex: MoexClient,
        *,
        concurrency: int = 3,
    ) -> None:
        self.config = config
        self.session_factory = session_factory
        self.moex = moex
        self._semaphore = asyncio.Semaphore(concurrency)
        self._write_lock = asyncio.Lock()

    async def _requested_start(self, ticker: str, timeframe: str) -> datetime:
        configured = _at_utc_start(self.config.start_dates[timeframe])
        async with self.session_factory() as session:
            latest = await latest_candle_begin(session, ticker, timeframe)
        if latest is None:
            return configured
        if latest.tzinfo is None:
            latest = latest.replace(tzinfo=UTC)
        return max(configured, latest - OVERLAP[timeframe])

    async def _sync_ticker(
        self,
        instrument: InstrumentData,
        *,
        date_to: datetime,
    ) -> dict[str, int]:
        counts = {timeframe: 0 for timeframe in self.config.start_dates}
        grouped: dict[int, list[str]] = {}
        for timeframe in self.config.start_dates:
            grouped.setdefault(SOURCE_INTERVALS[timeframe], []).append(timeframe)

        async with self._semaphore:
            for timeframes in grouped.values():
                starts = {
                    timeframe: await self._requested_start(instrument.secid, timeframe)
                    for timeframe in timeframes
                }
                fetched = await self.moex.fetch_candles_multi(
                    instrument.secid,
                    timeframes,
                    min(starts.values()),
                    board_id=self.config.board_id,
                    date_to=date_to,
                )
                for timeframe in timeframes:
                    selected = [
                        candle
                        for candle in fetched[timeframe]
                        if candle.begin >= starts[timeframe] and candle.end <= date_to
                    ]
                    async with self._write_lock:
                        async with self.session_factory() as session, session.begin():
                            counts[timeframe] += await upsert_candles(session, selected)
        return counts

    async def sync(self, *, date_to: datetime | None = None) -> dict[str, object]:
        cutoff = date_to or datetime.now(UTC)
        available = {
            item.secid: item for item in await self.moex.fetch_instruments(self.config.board_id)
        }
        selected = [available[ticker] for ticker in self.config.tickers if ticker in available]
        missing = [ticker for ticker in self.config.tickers if ticker not in available]
        if not selected:
            raise RuntimeError("none of the configured research tickers is available on MOEX")
        async with self.session_factory() as session, session.begin():
            await upsert_instruments(session, selected)

        results = await asyncio.gather(
            *(self._sync_ticker(instrument, date_to=cutoff) for instrument in selected),
            return_exceptions=True,
        )
        errors: dict[str, str] = {}
        inserted: dict[str, dict[str, int]] = {}
        for instrument, result in zip(selected, results, strict=True):
            if isinstance(result, BaseException):
                errors[instrument.secid] = str(result)
                logger.error("Research ingestion failed for %s: %s", instrument.secid, result)
            else:
                inserted[instrument.secid] = result
        async with self.session_factory() as session:
            coverage = await candle_coverage(session)
        return {
            "dataset": self.config.name,
            "generated_at": datetime.now(UTC).isoformat(),
            "requested_tickers": list(self.config.tickers),
            "available_tickers": [item.secid for item in selected],
            "missing_tickers": missing,
            "selection_method": self.config.selection_method,
            "survivorship_bias_warning": self.config.selection_method != "point_in_time_universe",
            "prices_adjusted_for_corporate_actions": (
                self.config.prices_adjusted_for_corporate_actions
            ),
            "inserted": inserted,
            "errors": errors,
            "coverage": [{key: _iso(value) for key, value in row.items()} for row in coverage],
        }
