from __future__ import annotations

import logging
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time
from enum import StrEnum
from statistics import fmean
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.models import Candle, Instrument, OrderBookLevel, TradingIdea
from app.observation import aware_utc, completed_candles

logger = logging.getLogger(__name__)


class LiquidityRating(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class BookLevelInput:
    side: str
    price: float
    quantity_lots: float | None


@dataclass(frozen=True, slots=True)
class LiquidityDepth:
    band: float
    bid_depth: float
    ask_depth: float
    entry_depth: float
    exit_depth: float
    relevant_depth: float


@dataclass(frozen=True, slots=True)
class LiquidityInput:
    ticker: str
    instrument_name: str
    direction: str
    price: float | None
    turnover_today: float | None
    last_daily_turnover: float | None
    adv20: float | None
    adv_days_target: int
    adv_days_used: int
    lot_size: int
    volatility: str | None
    book_levels: tuple[BookLevelInput, ...] = ()
    book_snapshot_at: datetime | None = None
    market_open: bool = False
    now: datetime | None = None


@dataclass(frozen=True, slots=True)
class LiquidityAssessment:
    ticker: str
    instrument_name: str
    direction: str
    price: float | None
    turnover_today: float | None
    last_daily_turnover: float | None
    adv20: float | None
    adv_days_target: int
    adv_days_used: int
    relative_turnover: float | None
    spread_pct: float | None
    spread_is_current: bool
    book_snapshot_at: datetime | None
    book_age_seconds: float | None
    book_fresh: bool
    market_open: bool
    depths: tuple[LiquidityDepth, ...]
    turnover_cap: float | None
    depth_cap: float | None
    entry_depth: float | None
    exit_depth: float | None
    relevant_depth: float | None
    volatility_modifier: float
    spread_modifier: float
    comfortable_size: float | None
    comfortable_size_rounded: float | None
    liquidity_rating: LiquidityRating
    rating_score: float | None
    calculation_basis: str

    def depth(self, band: float) -> LiquidityDepth | None:
        return next((item for item in self.depths if math.isclose(item.band, band)), None)

    def ai_snapshot(self) -> dict[str, object]:
        """Stable optional context for a future prompt; not wired into verdicts in V2.1.5."""
        return {
            "liquidity_rating": self.liquidity_rating.value,
            "adv20": self.adv20,
            "spread": self.spread_pct if self.spread_is_current else None,
            "relative_turnover": self.relative_turnover,
            "comfortable_liquidity_size": self.comfortable_size_rounded,
        }

    def debug_values(self) -> dict[str, object]:
        return {
            "ticker": self.ticker,
            "turnover_today": self.turnover_today,
            "adv20": self.adv20,
            "relative_turnover": self.relative_turnover,
            "spread_pct": self.spread_pct,
            "book_age_seconds": self.book_age_seconds,
            "entry_depth": self.entry_depth,
            "exit_depth": self.exit_depth,
            "relevant_depth": self.relevant_depth,
            "turnover_cap": self.turnover_cap,
            "depth_cap": self.depth_cap,
            "volatility_modifier": self.volatility_modifier,
            "spread_modifier": self.spread_modifier,
            "comfortable_size": self.comfortable_size,
            "liquidity_rating": self.liquidity_rating.value,
        }


def _positive(value: float | int | None) -> float | None:
    if value is None:
        return None
    checked = float(value)
    return checked if math.isfinite(checked) and checked > 0 else None


def historical_turnovers(
    candles: Sequence[object],
    *,
    days: int,
    now: datetime,
) -> tuple[float | None, int, float | None]:
    """Return ADV, sample size and the last completed daily turnover in RUB."""
    completed = completed_candles(list(candles), "1d", now=now)
    values: list[float] = []
    for candle in completed:
        direct = _positive(getattr(candle, "value", None))
        reconstructed = None
        if direct is None:
            close = _positive(getattr(candle, "close", None))
            volume = _positive(getattr(candle, "volume", None))
            if close is not None and volume is not None:
                reconstructed = close * volume
        turnover = direct or reconstructed
        if turnover is not None:
            values.append(turnover)
    selected = values[-days:]
    return (
        fmean(selected) if selected else None,
        len(selected),
        values[-1] if values else None,
    )


def _book_top(levels: Iterable[BookLevelInput]) -> tuple[float | None, float | None]:
    bids = [level.price for level in levels if level.side == "B" and level.price > 0]
    asks = [level.price for level in levels if level.side == "S" and level.price > 0]
    return (max(bids) if bids else None, min(asks) if asks else None)


def _spread_pct(levels: Iterable[BookLevelInput]) -> float | None:
    best_bid, best_ask = _book_top(levels)
    if best_bid is None or best_ask is None or best_ask < best_bid:
        return None
    mid = (best_bid + best_ask) / 2
    return (best_ask - best_bid) / mid if mid > 0 else None


def calculate_book_depths(
    levels: Sequence[BookLevelInput],
    *,
    direction: str,
    lot_size: int,
    bands: Sequence[float],
) -> tuple[LiquidityDepth, ...]:
    """Calculate executable RUB notional. MOEX order-book QUANTITY is in lots."""
    best_bid, best_ask = _book_top(levels)
    if best_bid is None or best_ask is None:
        return ()
    lot = max(1, int(lot_size))
    normalized_direction = direction.upper()
    if normalized_direction not in {"BUY", "SELL"}:
        raise ValueError(f"Unsupported idea direction: {direction}")
    result: list[LiquidityDepth] = []
    for band in bands:
        bid_depth = sum(
            level.price * (level.quantity_lots or 0) * lot
            for level in levels
            if level.side == "B"
            and level.price >= best_bid * (1 - band)
            and level.price <= best_bid
            and level.quantity_lots is not None
            and level.quantity_lots > 0
        )
        ask_depth = sum(
            level.price * (level.quantity_lots or 0) * lot
            for level in levels
            if level.side == "S"
            and level.price <= best_ask * (1 + band)
            and level.price >= best_ask
            and level.quantity_lots is not None
            and level.quantity_lots > 0
        )
        entry_depth, exit_depth = (
            (ask_depth, bid_depth) if normalized_direction == "BUY" else (bid_depth, ask_depth)
        )
        result.append(
            LiquidityDepth(
                band=float(band),
                bid_depth=bid_depth,
                ask_depth=ask_depth,
                entry_depth=entry_depth,
                exit_depth=exit_depth,
                relevant_depth=min(entry_depth, exit_depth),
            )
        )
    return tuple(result)


def round_liquidity_size_down(value: float | None) -> float | None:
    """Round down to a human-friendly 1/2.5/5/7.5 × 10^n liquidity step."""
    checked = _positive(value)
    if checked is None:
        return None
    exponent = math.floor(math.log10(checked))
    candidates = [
        mantissa * (10**power)
        for power in range(max(0, exponent - 2), exponent + 1)
        for mantissa in (1.0, 2.5, 5.0, 7.5)
        if mantissa * (10**power) <= checked
    ]
    if candidates:
        return max(candidates)
    return math.floor(checked)


def _tier_score(value: float, medium: float, high: float) -> float:
    if value >= high:
        return 1.0
    if value >= medium:
        return 0.6
    return 0.2


def _inverse_tier_score(value: float, high: float, medium: float) -> float:
    if value <= high:
        return 1.0
    if value <= medium:
        return 0.6
    return 0.0


def _liquidity_rating(
    settings: Settings,
    *,
    adv20: float | None,
    relative_turnover: float | None,
    spread_pct: float | None,
    relevant_depth: float | None,
    book_fresh: bool,
    book_snapshot_at: datetime | None,
) -> tuple[LiquidityRating, float | None]:
    components: list[tuple[float, float]] = []
    if adv20 is not None:
        components.append(
            (
                settings.liquidity_rating_adv_weight,
                _tier_score(
                    adv20,
                    settings.liquidity_medium_adv_threshold,
                    settings.liquidity_high_adv_threshold,
                ),
            )
        )
    if relative_turnover is not None:
        components.append(
            (
                settings.liquidity_rating_relative_turnover_weight,
                _tier_score(
                    relative_turnover,
                    settings.liquidity_medium_relative_turnover,
                    settings.liquidity_high_relative_turnover,
                ),
            )
        )
    if spread_pct is not None and book_fresh:
        components.append(
            (
                settings.liquidity_rating_spread_weight,
                _inverse_tier_score(
                    spread_pct,
                    settings.liquidity_spread_tight_threshold,
                    settings.liquidity_spread_wide_threshold,
                ),
            )
        )
    if relevant_depth is not None and book_fresh:
        components.append(
            (
                settings.liquidity_rating_depth_weight,
                _tier_score(
                    relevant_depth,
                    settings.liquidity_medium_depth_threshold,
                    settings.liquidity_high_depth_threshold,
                ),
            )
        )
    # Freshness is a real component rather than silently treating a stale book as current.
    components.append(
        (
            settings.liquidity_rating_freshness_weight,
            1.0 if book_fresh else (0.0 if book_snapshot_at is not None else 0.2),
        )
    )
    known_weight = sum(weight for weight, _ in components)
    if known_weight < settings.liquidity_min_known_rating_weight:
        return LiquidityRating.UNKNOWN, None
    score = sum(weight * value for weight, value in components) / known_weight
    if score >= settings.liquidity_high_rating_score:
        return LiquidityRating.HIGH, score
    if score >= settings.liquidity_medium_rating_score:
        return LiquidityRating.MEDIUM, score
    return LiquidityRating.LOW, score


def _spread_modifier(settings: Settings, spread_pct: float | None) -> float:
    if spread_pct is None:
        return settings.liquidity_unknown_spread_modifier
    if spread_pct <= settings.liquidity_spread_tight_threshold:
        return settings.liquidity_spread_tight_modifier
    if spread_pct <= settings.liquidity_spread_normal_threshold:
        return settings.liquidity_spread_normal_modifier
    if spread_pct <= settings.liquidity_spread_wide_threshold:
        return settings.liquidity_spread_wide_modifier
    return settings.liquidity_spread_very_wide_modifier


def _volatility_modifier(settings: Settings, volatility: str | None) -> float:
    return {
        "LOW": settings.liquidity_low_volatility_modifier,
        "NORMAL": settings.liquidity_normal_volatility_modifier,
        "HIGH": settings.liquidity_high_volatility_modifier,
        "EXTREME": settings.liquidity_extreme_volatility_modifier,
    }.get((volatility or "").upper(), settings.liquidity_unknown_volatility_modifier)


def calculate_liquidity(
    inputs: LiquidityInput,
    settings: Settings,
) -> LiquidityAssessment:
    checked_at = aware_utc(inputs.now or datetime.now(UTC))
    snapshot_at = aware_utc(inputs.book_snapshot_at) if inputs.book_snapshot_at else None
    book_age = (
        max(0.0, (checked_at - snapshot_at).total_seconds()) if snapshot_at is not None else None
    )
    book_fresh = bool(
        inputs.market_open
        and inputs.book_levels
        and book_age is not None
        and book_age <= settings.liquidity_orderbook_freshness_seconds
    )
    last_spread = _spread_pct(inputs.book_levels)
    spread_for_calculation = last_spread if book_fresh else None
    depths = (
        calculate_book_depths(
            inputs.book_levels,
            direction=inputs.direction,
            lot_size=inputs.lot_size,
            bands=settings.liquidity_book_bands,
        )
        if book_fresh
        else ()
    )
    primary = next(
        (item for item in depths if math.isclose(item.band, settings.liquidity_book_band_primary)),
        None,
    )
    adv20 = _positive(inputs.adv20)
    turnover_today = _positive(inputs.turnover_today)
    comparison_turnover = (
        turnover_today if inputs.market_open else _positive(inputs.last_daily_turnover)
    )
    relative_turnover = (
        comparison_turnover / adv20
        if comparison_turnover is not None and adv20 is not None
        else None
    )
    turnover_cap = adv20 * settings.liquidity_turnover_participation if adv20 else None
    relevant_depth = _positive(primary.relevant_depth) if primary else None
    depth_cap = (
        relevant_depth * settings.liquidity_book_participation
        if relevant_depth is not None
        else None
    )
    volatility_modifier = _volatility_modifier(settings, inputs.volatility)
    spread_modifier = _spread_modifier(settings, spread_for_calculation)
    if turnover_cap is None:
        raw_size = None
        basis = "INSUFFICIENT"
    elif depth_cap is not None:
        raw_size = min(turnover_cap, depth_cap) * volatility_modifier * spread_modifier
        basis = "TURNOVER_AND_BOOK"
    else:
        raw_size = turnover_cap * volatility_modifier * spread_modifier
        basis = "TURNOVER_ONLY"
    rating, rating_score = _liquidity_rating(
        settings,
        adv20=adv20,
        relative_turnover=relative_turnover,
        spread_pct=last_spread,
        relevant_depth=relevant_depth,
        book_fresh=book_fresh,
        book_snapshot_at=snapshot_at,
    )
    return LiquidityAssessment(
        ticker=inputs.ticker.upper(),
        instrument_name=inputs.instrument_name,
        direction=inputs.direction.upper(),
        price=_positive(inputs.price),
        turnover_today=turnover_today,
        last_daily_turnover=_positive(inputs.last_daily_turnover),
        adv20=adv20,
        adv_days_target=inputs.adv_days_target,
        adv_days_used=inputs.adv_days_used,
        relative_turnover=relative_turnover,
        spread_pct=last_spread,
        spread_is_current=book_fresh and last_spread is not None,
        book_snapshot_at=snapshot_at,
        book_age_seconds=book_age,
        book_fresh=book_fresh,
        market_open=inputs.market_open,
        depths=depths,
        turnover_cap=turnover_cap,
        depth_cap=depth_cap,
        entry_depth=primary.entry_depth if primary else None,
        exit_depth=primary.exit_depth if primary else None,
        relevant_depth=relevant_depth,
        volatility_modifier=volatility_modifier,
        spread_modifier=spread_modifier,
        comfortable_size=raw_size,
        comfortable_size_rounded=round_liquidity_size_down(raw_size),
        liquidity_rating=rating,
        rating_score=rating_score,
        calculation_basis=basis,
    )


def expected_market_open(now: datetime, timezone: str) -> bool:
    """Conservative local fallback when no fresh book proves that a session is active."""
    local = aware_utc(now).astimezone(ZoneInfo(timezone))
    return local.weekday() < 5 and time(6, 50) <= local.time() < time(23, 50)


class LiquidityService:
    """Read-only DB-first liquidity assessment for Telegram and future AI context."""

    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory

    async def assess_idea(
        self,
        idea: TradingIdea,
        *,
        now: datetime | None = None,
        market_open: bool | None = None,
    ) -> LiquidityAssessment:
        return await self.assess(
            idea.ticker,
            direction=idea.direction,
            instrument_name=idea.instrument_name,
            price_hint=idea.current_price,
            volatility=idea.market_volatility,
            now=now,
            market_open=market_open,
        )

    async def assess(
        self,
        ticker: str,
        *,
        direction: str,
        instrument_name: str | None = None,
        price_hint: float | None = None,
        volatility: str | None = None,
        now: datetime | None = None,
        market_open: bool | None = None,
    ) -> LiquidityAssessment:
        checked_at = aware_utc(now or datetime.now(UTC))
        secid = ticker.upper()
        async with self.session_factory() as session:
            instrument = await session.get(Instrument, secid)
            if instrument is None:
                raise ValueError(f"Instrument {secid} was not found")
            candles = list(
                await session.scalars(
                    select(Candle)
                    .where(Candle.secid == secid, Candle.timeframe == "1d")
                    .order_by(Candle.begin.desc())
                    .limit(max(self.settings.liquidity_adv_days + 15, 40))
                )
            )
            candles.reverse()
            latest_snapshot = await session.scalar(
                select(func.max(OrderBookLevel.snapshot_at)).where(OrderBookLevel.secid == secid)
            )
            book_rows = (
                list(
                    await session.scalars(
                        select(OrderBookLevel)
                        .where(
                            OrderBookLevel.secid == secid,
                            OrderBookLevel.snapshot_at == latest_snapshot,
                        )
                        .order_by(OrderBookLevel.side, OrderBookLevel.level)
                    )
                )
                if latest_snapshot is not None
                else []
            )
            updated_at = instrument.updated_at
            database_price = instrument.last_price
            database_turnover = instrument.daily_turnover
            short_name = instrument.short_name
            lot_size = instrument.lot_size or 1

        adv20, adv_days_used, last_daily_turnover = historical_turnovers(
            candles,
            days=self.settings.liquidity_adv_days,
            now=checked_at,
        )
        latest_aware = aware_utc(latest_snapshot) if latest_snapshot is not None else None
        is_open = (
            market_open
            if market_open is not None
            else expected_market_open(checked_at, self.settings.scheduler_timezone)
        )
        turnover_fresh = bool(
            updated_at is not None
            and is_open
            and 0
            <= (checked_at - aware_utc(updated_at)).total_seconds()
            <= self.settings.liquidity_turnover_freshness_seconds
        )
        turnover_today = database_turnover if turnover_fresh else None
        if not is_open and database_turnover is not None and updated_at is not None:
            local_checked = checked_at.astimezone(ZoneInfo(self.settings.scheduler_timezone)).date()
            local_updated = (
                aware_utc(updated_at).astimezone(ZoneInfo(self.settings.scheduler_timezone)).date()
            )
            if local_checked == local_updated:
                last_daily_turnover = database_turnover
        assessment = calculate_liquidity(
            LiquidityInput(
                ticker=secid,
                instrument_name=instrument_name or short_name,
                direction=direction,
                price=database_price or price_hint,
                turnover_today=turnover_today,
                last_daily_turnover=last_daily_turnover,
                adv20=adv20,
                adv_days_target=self.settings.liquidity_adv_days,
                adv_days_used=adv_days_used,
                lot_size=lot_size,
                volatility=volatility,
                book_levels=tuple(
                    BookLevelInput(
                        side=row.side,
                        price=row.price,
                        quantity_lots=row.quantity,
                    )
                    for row in book_rows
                ),
                book_snapshot_at=latest_aware,
                market_open=is_open,
                now=checked_at,
            ),
            self.settings,
        )
        logger.debug("liquidity assessment %s", assessment.debug_values())
        return assessment
