from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Generic, TypeVar

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.idea_repository import OPEN_IDEA_STATUSES
from app.models import (
    CandidateExperiment,
    IdeaFollow,
    Instrument,
    SignalRecord,
    TelegramUser,
    TradingIdea,
    WatchlistItem,
)
from app.repositories import (
    add_idea_follow,
    add_watchlist_item,
    remove_idea_follow,
    remove_watchlist_item,
)

PAGE_SIZE = 5
TICKER_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9._-]{0,23}$")
MAX_DATABASE_ID = 2_147_483_647
T = TypeVar("T")


class ContextAccessError(ValueError):
    pass


class ContextObjectNotFound(ValueError):
    pass


class ContextActionExpired(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ContextPage(Generic[T]):
    items: tuple[T, ...]
    page: int
    total_pages: int
    total_items: int


@dataclass(frozen=True, slots=True)
class IdeaContext:
    idea: TradingIdea
    watched: bool
    followed: bool


@dataclass(frozen=True, slots=True)
class InstrumentContext:
    instrument: Instrument
    watched: bool
    latest_idea: TradingIdea | None
    latest_signal: SignalRecord | None


@dataclass(frozen=True, slots=True)
class ResultItem:
    source: str
    object_id: int
    ticker: str
    horizon: str
    direction: str
    status: str
    score: float


def normalize_ticker(value: str) -> str:
    ticker = value.strip().upper()
    if not TICKER_PATTERN.fullmatch(ticker):
        raise ContextObjectNotFound("Некорректный тикер")
    return ticker


def normalize_idea_id(value: int) -> int:
    if not 1 <= value <= MAX_DATABASE_ID:
        raise ContextObjectNotFound("Некорректный идентификатор идеи")
    return value


def _page_number(page: int, total_items: int, page_size: int) -> tuple[int, int]:
    total_pages = max(1, math.ceil(total_items / page_size))
    return min(max(page, 0), total_pages - 1), total_pages


class TelegramContextService:
    """Authorization-aware persistence and queries for contextual Telegram actions."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    @staticmethod
    async def _require_user(session: AsyncSession, telegram_id: int) -> TelegramUser:
        user = await session.get(TelegramUser, telegram_id)
        if user is None or not user.is_active:
            raise ContextAccessError("Пользователь не зарегистрирован или отключён")
        return user

    async def idea_context(self, telegram_id: int, idea_id: int) -> IdeaContext:
        idea_id = normalize_idea_id(idea_id)
        async with self.session_factory() as session:
            await self._require_user(session, telegram_id)
            idea = await session.get(TradingIdea, idea_id)
            if idea is None:
                raise ContextObjectNotFound("Идея не найдена или уже удалена")
            watched = (
                await session.scalar(
                    select(WatchlistItem.id).where(
                        WatchlistItem.telegram_id == telegram_id,
                        WatchlistItem.secid == idea.ticker,
                    )
                )
                is not None
            )
            followed = (
                await session.scalar(
                    select(IdeaFollow.id).where(
                        IdeaFollow.telegram_id == telegram_id,
                        IdeaFollow.idea_id == idea_id,
                    )
                )
                is not None
            )
            return IdeaContext(idea=idea, watched=watched, followed=followed)

    async def set_idea_follow(
        self,
        telegram_id: int,
        idea_id: int,
        *,
        enabled: bool,
    ) -> IdeaContext:
        idea_id = normalize_idea_id(idea_id)
        async with self.session_factory() as session, session.begin():
            await self._require_user(session, telegram_id)
            idea = await session.get(TradingIdea, idea_id)
            if idea is None:
                raise ContextObjectNotFound("Идея не найдена или уже удалена")
            existing = await session.scalar(
                select(IdeaFollow.id).where(
                    IdeaFollow.telegram_id == telegram_id,
                    IdeaFollow.idea_id == idea_id,
                )
            )
            if enabled:
                if idea.status not in OPEN_IDEA_STATUSES:
                    raise ContextActionExpired("Закрытую идею уже нельзя добавить в отслеживание")
                if existing is None:
                    await add_idea_follow(session, telegram_id, idea_id)
            elif existing is not None:
                await remove_idea_follow(session, telegram_id, idea_id)
        return await self.idea_context(telegram_id, idea_id)

    async def set_watch(
        self,
        telegram_id: int,
        ticker: str,
        *,
        enabled: bool,
    ) -> bool:
        secid = normalize_ticker(ticker)
        async with self.session_factory() as session, session.begin():
            await self._require_user(session, telegram_id)
            instrument = await session.get(Instrument, secid)
            if instrument is None or not instrument.is_active:
                raise ContextObjectNotFound("Инструмент не найден в активной MOEX-вселенной")
            if enabled:
                await add_watchlist_item(session, telegram_id, secid)
            else:
                await remove_watchlist_item(session, telegram_id, secid)
            return enabled

    async def instrument_context(self, telegram_id: int, ticker: str) -> InstrumentContext:
        secid = normalize_ticker(ticker)
        async with self.session_factory() as session:
            await self._require_user(session, telegram_id)
            instrument = await session.get(Instrument, secid)
            if instrument is None or not instrument.is_active:
                raise ContextObjectNotFound("Инструмент не найден в активной MOEX-вселенной")
            watched = (
                await session.scalar(
                    select(WatchlistItem.id).where(
                        WatchlistItem.telegram_id == telegram_id,
                        WatchlistItem.secid == secid,
                    )
                )
                is not None
            )
            latest_idea = await session.scalar(
                select(TradingIdea)
                .where(TradingIdea.ticker == secid)
                .order_by(TradingIdea.created_at.desc(), TradingIdea.id.desc())
                .limit(1)
            )
            latest_signal = await session.scalar(
                select(SignalRecord)
                .where(SignalRecord.secid == secid)
                .order_by(SignalRecord.created_at.desc(), SignalRecord.id.desc())
                .limit(1)
            )
            return InstrumentContext(
                instrument=instrument,
                watched=watched,
                latest_idea=latest_idea,
                latest_signal=latest_signal,
            )

    async def watchlist_page(
        self,
        telegram_id: int,
        page: int,
        *,
        page_size: int = PAGE_SIZE,
    ) -> ContextPage[Instrument]:
        async with self.session_factory() as session:
            await self._require_user(session, telegram_id)
            condition = WatchlistItem.telegram_id == telegram_id
            total = int(
                await session.scalar(
                    select(func.count()).select_from(WatchlistItem).where(condition)
                )
                or 0
            )
            selected_page, total_pages = _page_number(page, total, page_size)
            items = tuple(
                await session.scalars(
                    select(Instrument)
                    .join(WatchlistItem, WatchlistItem.secid == Instrument.secid)
                    .where(condition)
                    .order_by(Instrument.secid)
                    .offset(selected_page * page_size)
                    .limit(page_size)
                )
            )
            return ContextPage(items, selected_page, total_pages, total)

    async def open_ideas_page(
        self,
        telegram_id: int,
        page: int,
        *,
        page_size: int = PAGE_SIZE,
    ) -> ContextPage[TradingIdea]:
        async with self.session_factory() as session:
            await self._require_user(session, telegram_id)
            condition = TradingIdea.status.in_(OPEN_IDEA_STATUSES)
            total = int(
                await session.scalar(select(func.count()).select_from(TradingIdea).where(condition))
                or 0
            )
            selected_page, total_pages = _page_number(page, total, page_size)
            items = tuple(
                await session.scalars(
                    select(TradingIdea)
                    .where(condition)
                    .order_by(TradingIdea.confidence.desc(), TradingIdea.created_at.desc())
                    .offset(selected_page * page_size)
                    .limit(page_size)
                )
            )
            return ContextPage(items, selected_page, total_pages, total)

    async def signal_history_page(
        self,
        telegram_id: int,
        ticker: str,
        page: int,
        *,
        page_size: int = PAGE_SIZE,
    ) -> ContextPage[SignalRecord]:
        secid = normalize_ticker(ticker)
        async with self.session_factory() as session:
            await self._require_user(session, telegram_id)
            if await session.get(Instrument, secid) is None:
                raise ContextObjectNotFound("Инструмент не найден")
            condition = SignalRecord.secid == secid
            total = int(
                await session.scalar(
                    select(func.count()).select_from(SignalRecord).where(condition)
                )
                or 0
            )
            selected_page, total_pages = _page_number(page, total, page_size)
            items = tuple(
                await session.scalars(
                    select(SignalRecord)
                    .where(condition)
                    .order_by(SignalRecord.created_at.desc(), SignalRecord.id.desc())
                    .offset(selected_page * page_size)
                    .limit(page_size)
                )
            )
            return ContextPage(items, selected_page, total_pages, total)

    async def results_page(
        self,
        telegram_id: int,
        result_filter: str,
        page: int,
        *,
        page_size: int = PAGE_SIZE,
    ) -> ContextPage[ResultItem]:
        async with self.session_factory() as session:
            await self._require_user(session, telegram_id)
            if result_filter == "airej":
                condition = CandidateExperiment.ai_verdict == "REJECT"
                total = int(
                    await session.scalar(
                        select(func.count()).select_from(CandidateExperiment).where(condition)
                    )
                    or 0
                )
                selected_page, total_pages = _page_number(page, total, page_size)
                rows = tuple(
                    await session.scalars(
                        select(CandidateExperiment)
                        .where(condition)
                        .order_by(CandidateExperiment.decision_at.desc())
                        .offset(selected_page * page_size)
                        .limit(page_size)
                    )
                )
                items = tuple(
                    ResultItem(
                        source="experiment",
                        object_id=row.id,
                        ticker=row.ticker,
                        horizon=row.horizon,
                        direction=row.direction,
                        status="AI_REJECT",
                        score=row.final_quality_score,
                    )
                    for row in rows
                )
                return ContextPage(items, selected_page, total_pages, total)

            statuses = {
                "win": ("TP_HIT",),
                "loss": ("SL_HIT",),
                "active": tuple(OPEN_IDEA_STATUSES),
                "expired": ("EXPIRED",),
                "missed": ("INVALIDATED",),
            }
            selected_statuses = statuses.get(result_filter)
            if selected_statuses is None:
                raise ContextObjectNotFound("Неизвестный фильтр результатов")
            condition = TradingIdea.status.in_(selected_statuses)
            total = int(
                await session.scalar(select(func.count()).select_from(TradingIdea).where(condition))
                or 0
            )
            selected_page, total_pages = _page_number(page, total, page_size)
            rows = tuple(
                await session.scalars(
                    select(TradingIdea)
                    .where(condition)
                    .order_by(TradingIdea.updated_at.desc(), TradingIdea.id.desc())
                    .offset(selected_page * page_size)
                    .limit(page_size)
                )
            )
            items = tuple(
                ResultItem(
                    source="idea",
                    object_id=row.id,
                    ticker=row.ticker,
                    horizon=row.horizon,
                    direction=row.direction,
                    status=row.status,
                    score=row.final_quality_score or row.confidence,
                )
                for row in rows
            )
            return ContextPage(items, selected_page, total_pages, total)
