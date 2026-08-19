from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain import CandleData, IdeaDirection, IdeaStatus, IdeaTransition
from app.idea_repository import OPEN_IDEA_STATUSES, add_idea_event
from app.models import Candle, TradingIdea
from app.repositories import get_candles_after


class TrackableIdea(Protocol):
    direction: str
    status: str
    entry_price_from: float
    entry_price_to: float
    take_profit: float
    stop_loss: float
    expires_at: datetime


class CandleLike(Protocol):
    begin: datetime
    end: datetime
    open: float
    high: float
    low: float
    close: float


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _entry_fill(idea: TrackableIdea, candle: CandleLike) -> float:
    if idea.entry_price_from <= candle.open <= idea.entry_price_to:
        return candle.open
    if candle.open > idea.entry_price_to:
        return idea.entry_price_to
    return idea.entry_price_from


def _exit_transition(
    idea: TrackableIdea,
    candle: CandleLike,
    *,
    activated_on_candle: bool,
) -> IdeaTransition | None:
    direction = IdeaDirection(idea.direction)
    if direction == IdeaDirection.BUY:
        stop_hit = candle.low <= idea.stop_loss
        target_hit = candle.high >= idea.take_profit
        unambiguous_target = candle.open <= idea.entry_price_to
    else:
        stop_hit = candle.high >= idea.stop_loss
        target_hit = candle.low <= idea.take_profit
        unambiguous_target = candle.open >= idea.entry_price_from

    # If both limits were crossed in one OHLC candle, ordering is unknowable.
    # A conservative stop-first assumption avoids inflated live/backtest results.
    if stop_hit:
        return IdeaTransition(
            to_status=IdeaStatus.SL_HIT,
            event_type="SL_HIT",
            price=idea.stop_loss,
            reason="Достигнут Stop Loss",
            occurred_at=_utc(candle.end),
        )
    if target_hit and (not activated_on_candle or unambiguous_target):
        return IdeaTransition(
            to_status=IdeaStatus.TP_HIT,
            event_type="TP_HIT",
            price=idea.take_profit,
            reason="Достигнут Take Profit",
            occurred_at=_utc(candle.end),
        )
    return None


def evaluate_idea_candle(
    idea: TrackableIdea,
    candle: CandleLike,
) -> list[IdeaTransition]:
    status = IdeaStatus(idea.status)
    if status not in {IdeaStatus.PENDING_ENTRY, IdeaStatus.ACTIVE}:
        return []
    if _utc(candle.begin) >= _utc(idea.expires_at):
        return [
            IdeaTransition(
                to_status=IdeaStatus.EXPIRED,
                event_type="EXPIRED",
                price=candle.open,
                reason="Истёк срок торговой идеи",
                occurred_at=_utc(idea.expires_at),
            )
        ]

    if status == IdeaStatus.ACTIVE:
        transition = _exit_transition(idea, candle, activated_on_candle=False)
        return [transition] if transition else []

    zone_touched = candle.low <= idea.entry_price_to and candle.high >= idea.entry_price_from
    direction = IdeaDirection(idea.direction)
    if not zone_touched:
        target_missed = (
            candle.low > idea.entry_price_to and candle.high >= idea.take_profit
            if direction == IdeaDirection.BUY
            else candle.high < idea.entry_price_from and candle.low <= idea.take_profit
        )
        stop_gapped = (
            candle.high < idea.entry_price_from and candle.low <= idea.stop_loss
            if direction == IdeaDirection.BUY
            else candle.low > idea.entry_price_to and candle.high >= idea.stop_loss
        )
        if target_missed or stop_gapped:
            return [
                IdeaTransition(
                    to_status=IdeaStatus.INVALIDATED,
                    event_type="ENTRY_MISSED",
                    price=candle.close,
                    reason=(
                        "Цена достигла цели без входа в entry zone"
                        if target_missed
                        else "Цена перескочила entry zone и уровень отмены"
                    ),
                    occurred_at=_utc(candle.end),
                )
            ]
        return []

    activation = IdeaTransition(
        to_status=IdeaStatus.ACTIVE,
        event_type="ACTIVATED",
        price=_entry_fill(idea, candle),
        reason="Цена вошла в entry zone",
        occurred_at=_utc(candle.begin),
    )
    result = [activation]
    exit_transition = _exit_transition(idea, candle, activated_on_candle=True)
    if exit_transition is not None:
        result.append(exit_transition)
    return result


class IdeaTracker:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    @staticmethod
    def _apply_transition(
        session: AsyncSession,
        idea: TradingIdea,
        transition: IdeaTransition,
    ) -> None:
        previous_status = idea.status
        idea.status = transition.to_status.value
        idea.version += 1
        idea.updated_at = transition.occurred_at
        if transition.to_status == IdeaStatus.ACTIVE:
            idea.activated_at = transition.occurred_at
        elif transition.to_status not in {
            IdeaStatus.PENDING_ENTRY,
            IdeaStatus.ACTIVE,
        }:
            idea.closed_at = transition.occurred_at
            idea.close_price = transition.price
            idea.close_reason = transition.reason
        add_idea_event(
            session,
            idea,
            event_type=transition.event_type,
            from_status=previous_status,
            to_status=idea.status,
            price=transition.price,
            details=transition.reason,
            occurred_at=transition.occurred_at,
        )

    async def track_idea(self, idea_id: int, candles: list[CandleLike]) -> list[IdeaTransition]:
        applied: list[IdeaTransition] = []
        async with self.session_factory() as session, session.begin():
            idea = await session.get(TradingIdea, idea_id)
            if idea is None:
                return []
            for candle in sorted(candles, key=lambda item: item.begin):
                if idea.status not in OPEN_IDEA_STATUSES:
                    break
                transitions = evaluate_idea_candle(idea, candle)
                idea.current_price = candle.close
                idea.last_evaluated_at = candle.begin
                for transition in transitions:
                    self._apply_transition(session, idea, transition)
                    applied.append(transition)
        return applied

    async def track_all(self, *, now: datetime | None = None) -> dict[str, int]:
        current_time = now or datetime.now(UTC)
        async with self.session_factory() as session:
            open_ideas = list(
                await session.scalars(
                    select(TradingIdea).where(TradingIdea.status.in_(OPEN_IDEA_STATUSES))
                )
            )
        counters = {"evaluated": 0, "transitions": 0, "expired": 0}
        for detached in open_ideas:
            after = detached.last_evaluated_at or detached.source_candle_begin
            async with self.session_factory() as session:
                candles: list[Candle] = await get_candles_after(
                    session,
                    detached.ticker,
                    detached.primary_timeframe,
                    after,
                )
            transitions = await self.track_idea(detached.id, candles)
            counters["evaluated"] += len(candles)
            counters["transitions"] += len(transitions)
            if transitions and transitions[-1].to_status == IdeaStatus.EXPIRED:
                counters["expired"] += 1
                continue
            if _utc(detached.expires_at) <= _utc(current_time):
                synthetic = CandleData(
                    secid=detached.ticker,
                    board_id="TQBR",
                    timeframe=detached.primary_timeframe,
                    begin=current_time,
                    end=current_time,
                    open=detached.current_price,
                    high=detached.current_price,
                    low=detached.current_price,
                    close=detached.current_price,
                    volume=0,
                    value=0,
                )
                expiry = await self.track_idea(detached.id, [synthetic])
                counters["transitions"] += len(expiry)
                counters["expired"] += bool(expiry)
        return counters
