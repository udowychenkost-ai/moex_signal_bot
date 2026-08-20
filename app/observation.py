from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.domain import IdeaHorizon, StaleMarketDataError
from app.horizons import get_horizon_profile
from app.models import Candle, Instrument, MarketCandle

TIMEFRAME_DURATIONS = {
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
    "1d": timedelta(days=1),
    "1w": timedelta(days=7),
}


def completed_candles(
    candles: list[object],
    timeframe: str,
    *,
    now: datetime | None = None,
) -> list[object]:
    """Exclude a still-forming MOEX bucket from a decision-time window."""
    checked_at = aware_utc(now or datetime.now(UTC))
    duration = TIMEFRAME_DURATIONS[timeframe]
    return [
        candle
        for candle in candles
        if aware_utc(candle.begin) + duration <= checked_at and aware_utc(candle.end) <= checked_at
    ]


def aware_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class FreshnessRecord:
    ticker: str
    timeframe: str
    latest_at: datetime | None
    age_minutes: float | None
    limit_minutes: int
    is_fresh: bool


@dataclass(frozen=True, slots=True)
class FreshnessOverview:
    checked: int
    fresh: int
    stale: int
    latest_moex_update: datetime | None
    stale_examples: tuple[FreshnessRecord, ...]


class DataFreshnessGuard:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory

    async def check(
        self,
        ticker: str,
        timeframes: list[str],
        *,
        now: datetime | None = None,
    ) -> list[FreshnessRecord]:
        checked_at = aware_utc(now or datetime.now(UTC))
        limits = self.settings.freshness_limits
        records: list[FreshnessRecord] = []
        async with self.session_factory() as session:
            for timeframe in timeframes:
                duration = TIMEFRAME_DURATIONS[timeframe]
                latest = await session.scalar(
                    select(func.max(Candle.end)).where(
                        Candle.secid == ticker.upper(),
                        Candle.timeframe == timeframe,
                        Candle.begin <= checked_at - duration,
                    )
                )
                age = (
                    max(0.0, (checked_at - aware_utc(latest)).total_seconds() / 60)
                    if latest is not None
                    else None
                )
                limit = limits[timeframe]
                records.append(
                    FreshnessRecord(
                        ticker=ticker.upper(),
                        timeframe=timeframe,
                        latest_at=latest,
                        age_minutes=age,
                        limit_minutes=limit,
                        is_fresh=age is not None and age <= limit,
                    )
                )
        return records

    async def require_fresh(
        self,
        ticker: str,
        horizon: IdeaHorizon,
        *,
        now: datetime | None = None,
    ) -> list[FreshnessRecord]:
        profile = get_horizon_profile(horizon)
        records = await self.check(ticker, list(profile.timeframe_weights), now=now)
        stale = [record for record in records if not record.is_fresh]
        if stale:
            details = ", ".join(
                f"{record.timeframe}="
                + (
                    "missing"
                    if record.age_minutes is None
                    else f"{record.age_minutes:.0f}m>{record.limit_minutes}m"
                )
                for record in stale
            )
            raise StaleMarketDataError(
                f"Stale MOEX data blocked {ticker.upper()} {horizon.value}: {details}"
            )
        return records

    async def check_market_context(
        self,
        timeframes: list[str],
        *,
        now: datetime | None = None,
    ) -> list[FreshnessRecord]:
        if not self.settings.market_context_enabled:
            return []
        checked_at = aware_utc(now or datetime.now(UTC))
        limits = self.settings.freshness_limits
        records: list[FreshnessRecord] = []
        async with self.session_factory() as session:
            for timeframe in timeframes:
                duration = TIMEFRAME_DURATIONS[timeframe]
                latest = await session.scalar(
                    select(func.max(MarketCandle.end)).where(
                        MarketCandle.symbol == self.settings.market_benchmark.upper(),
                        MarketCandle.timeframe == timeframe,
                        MarketCandle.begin <= checked_at - duration,
                    )
                )
                age = (
                    max(0.0, (checked_at - aware_utc(latest)).total_seconds() / 60)
                    if latest is not None
                    else None
                )
                limit = limits[timeframe]
                records.append(
                    FreshnessRecord(
                        ticker=self.settings.market_benchmark.upper(),
                        timeframe=timeframe,
                        latest_at=latest,
                        age_minutes=age,
                        limit_minutes=limit,
                        is_fresh=age is not None and age <= limit,
                    )
                )
        return records

    async def overview(self, *, now: datetime | None = None) -> FreshnessOverview:
        checked_at = aware_utc(now or datetime.now(UTC))
        async with self.session_factory() as session:
            tickers = list(
                await session.scalars(
                    select(Instrument.secid)
                    .where(Instrument.is_active.is_(True))
                    .order_by(Instrument.secid)
                )
            )
        records: list[FreshnessRecord] = []
        for ticker in tickers:
            records.extend(
                await self.check(ticker, self.settings.analysis_timeframe_list, now=checked_at)
            )
        records.extend(
            await self.check_market_context(
                self.settings.analysis_timeframe_list,
                now=checked_at,
            )
        )
        latest = max(
            (aware_utc(item.latest_at) for item in records if item.latest_at is not None),
            default=None,
        )
        stale = [item for item in records if not item.is_fresh]
        return FreshnessOverview(
            checked=len(records),
            fresh=len(records) - len(stale),
            stale=len(stale),
            latest_moex_update=latest,
            stale_examples=tuple(stale[:8]),
        )
