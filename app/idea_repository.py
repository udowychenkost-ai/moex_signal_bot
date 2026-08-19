from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain import IdeaDirection, IdeaStatus, TradingIdeaData
from app.models import IdeaNotification, TradingIdea, TradingIdeaEvent

OPEN_IDEA_STATUSES = (IdeaStatus.PENDING_ENTRY.value, IdeaStatus.ACTIVE.value)


@dataclass(frozen=True, slots=True)
class IdeaUpsertResult:
    idea: TradingIdea
    created: bool
    materially_changed: bool


def _model_values(data: TradingIdeaData, material_hash: str) -> dict[str, object]:
    return {
        "ticker": data.ticker.upper(),
        "instrument_name": data.instrument_name,
        "direction": data.direction.value,
        "horizon": data.horizon.value,
        "primary_timeframe": data.primary_timeframe,
        "entry_price_from": data.entry_price_from,
        "entry_price_to": data.entry_price_to,
        "current_price": data.current_price,
        "take_profit": data.take_profit,
        "stop_loss": data.stop_loss,
        "confidence": data.confidence,
        "expected_return_pct": data.expected_return_pct,
        "risk_pct": data.risk_pct,
        "risk_reward_ratio": data.risk_reward_ratio,
        "rationale": "\n".join(data.rationale),
        "invalidation_reason": data.invalidation_reason,
        "status": data.status.value,
        "source_signal_id": data.source_signal_id,
        "source_timeframes": ",".join(data.source_timeframes),
        "source_candle_begin": data.source_candle_begin,
        "last_evaluated_at": data.last_evaluated_at or data.source_candle_begin,
        "material_hash": material_hash,
        "version": data.version,
        "created_at": data.created_at,
        "updated_at": data.created_at,
        "activated_at": data.activated_at,
        "activation_price": data.activation_price,
        "expires_at": data.expires_at,
        "closed_at": data.closed_at,
        "close_reason": data.close_reason,
        "close_price": data.close_price,
    }


def _relative_change(old: float, new: float) -> float:
    return abs(new - old) / abs(old) if old else abs(new - old)


def _same_moment(left: datetime, right: datetime) -> bool:
    normalized_left = left.replace(tzinfo=UTC) if left.tzinfo is None else left.astimezone(UTC)
    normalized_right = right.replace(tzinfo=UTC) if right.tzinfo is None else right.astimezone(UTC)
    return normalized_left == normalized_right


def _is_material_change(
    idea: TradingIdea,
    data: TradingIdeaData,
    confidence_delta: float,
) -> bool:
    if abs(data.confidence - idea.confidence) >= confidence_delta:
        return True
    return any(
        _relative_change(old, new) >= 0.005
        for old, new in (
            (idea.entry_price_from, data.entry_price_from),
            (idea.entry_price_to, data.entry_price_to),
            (idea.take_profit, data.take_profit),
            (idea.stop_loss, data.stop_loss),
        )
    )


async def get_open_idea(
    session: AsyncSession,
    ticker: str,
    horizon: str,
) -> TradingIdea | None:
    return await session.scalar(
        select(TradingIdea)
        .where(
            TradingIdea.ticker == ticker.upper(),
            TradingIdea.horizon == horizon,
            TradingIdea.status.in_(OPEN_IDEA_STATUSES),
        )
        .order_by(TradingIdea.created_at.desc())
        .limit(1)
    )


async def get_latest_idea(
    session: AsyncSession,
    ticker: str,
    horizon: str,
) -> TradingIdea | None:
    return await session.scalar(
        select(TradingIdea)
        .where(TradingIdea.ticker == ticker.upper(), TradingIdea.horizon == horizon)
        .order_by(TradingIdea.created_at.desc())
        .limit(1)
    )


def add_idea_event(
    session: AsyncSession,
    idea: TradingIdea,
    *,
    event_type: str,
    from_status: str | None,
    to_status: str,
    price: float | None = None,
    details: str = "",
    occurred_at: datetime | None = None,
) -> TradingIdeaEvent:
    event = TradingIdeaEvent(
        idea_id=idea.id,
        event_type=event_type,
        from_status=from_status,
        to_status=to_status,
        price=price,
        details=details,
        occurred_at=occurred_at or datetime.now(UTC),
    )
    session.add(event)
    return event


async def create_or_update_idea(
    session: AsyncSession,
    data: TradingIdeaData,
    *,
    material_hash: str,
    confidence_delta: float,
) -> IdeaUpsertResult:
    existing = await get_open_idea(session, data.ticker, data.horizon.value)
    if existing is not None and existing.direction != data.direction.value:
        previous_status = existing.status
        existing.status = IdeaStatus.CANCELLED.value
        existing.close_reason = "Сигнал сменил направление"
        existing.close_price = data.current_price
        existing.closed_at = data.created_at
        existing.updated_at = data.created_at
        existing.version += 1
        add_idea_event(
            session,
            existing,
            event_type="DIRECTION_REVERSED",
            from_status=previous_status,
            to_status=existing.status,
            price=data.current_price,
            details=existing.close_reason,
            occurred_at=data.created_at,
        )
        existing = None

    if existing is None:
        latest = await get_latest_idea(session, data.ticker, data.horizon.value)
        if latest is not None and _same_moment(
            latest.source_candle_begin, data.source_candle_begin
        ):
            return IdeaUpsertResult(
                idea=latest,
                created=False,
                materially_changed=False,
            )
        idea = TradingIdea(**_model_values(data, material_hash))
        session.add(idea)
        await session.flush()
        add_idea_event(
            session,
            idea,
            event_type="CREATED",
            from_status=None,
            to_status=idea.status,
            price=data.current_price,
            occurred_at=data.created_at,
        )
        return IdeaUpsertResult(idea=idea, created=True, materially_changed=True)

    material = (
        abs(data.confidence - existing.confidence) >= confidence_delta
        if existing.status == IdeaStatus.ACTIVE.value
        else _is_material_change(existing, data, confidence_delta)
    )
    existing.current_price = data.current_price
    existing.source_signal_id = data.source_signal_id
    existing.source_candle_begin = data.source_candle_begin
    existing.updated_at = data.created_at
    if not material:
        return IdeaUpsertResult(idea=existing, created=False, materially_changed=False)

    previous_status = existing.status
    existing.confidence = data.confidence
    existing.rationale = "\n".join(data.rationale)
    existing.material_hash = material_hash
    existing.version += 1
    if existing.status == IdeaStatus.PENDING_ENTRY.value:
        existing.entry_price_from = data.entry_price_from
        existing.entry_price_to = data.entry_price_to
        existing.take_profit = data.take_profit
        existing.stop_loss = data.stop_loss
        existing.expected_return_pct = data.expected_return_pct
        existing.risk_pct = data.risk_pct
        existing.risk_reward_ratio = data.risk_reward_ratio
        existing.expires_at = data.expires_at
    add_idea_event(
        session,
        existing,
        event_type="REASSESSED",
        from_status=previous_status,
        to_status=existing.status,
        price=data.current_price,
        details="Существенно обновлены параметры идеи",
        occurred_at=data.created_at,
    )
    return IdeaUpsertResult(idea=existing, created=False, materially_changed=True)


async def list_open_ideas(
    session: AsyncSession,
    *,
    horizon: str | None = None,
    minimum_confidence: float = 0,
    limit: int = 50,
) -> list[TradingIdea]:
    statement = select(TradingIdea).where(
        TradingIdea.status.in_(OPEN_IDEA_STATUSES),
        TradingIdea.confidence >= minimum_confidence,
    )
    if horizon and horizon != "all":
        statement = statement.where(TradingIdea.horizon == horizon)
    rows = await session.scalars(
        statement.order_by(TradingIdea.confidence.desc(), TradingIdea.created_at.desc()).limit(
            limit
        )
    )
    return list(rows)


async def get_idea(session: AsyncSession, idea_id: int) -> TradingIdea | None:
    return await session.get(TradingIdea, idea_id)


async def list_unnotified_ideas(
    session: AsyncSession,
    *,
    telegram_id: int,
    horizon: str,
    minimum_confidence: float,
    limit: int = 20,
) -> list[TradingIdea]:
    current_version_sent = exists().where(
        IdeaNotification.telegram_id == telegram_id,
        IdeaNotification.idea_id == TradingIdea.id,
        IdeaNotification.idea_version == TradingIdea.version,
    )
    previously_sent = exists().where(
        IdeaNotification.telegram_id == telegram_id,
        IdeaNotification.idea_id == TradingIdea.id,
    )
    statement = select(TradingIdea).where(
        TradingIdea.confidence >= minimum_confidence,
        ~current_version_sent,
        (TradingIdea.status.in_(OPEN_IDEA_STATUSES) | previously_sent),
    )
    if horizon != "all":
        statement = statement.where(TradingIdea.horizon == horizon)
    result = await session.scalars(
        statement.order_by(TradingIdea.confidence.desc(), TradingIdea.updated_at.desc()).limit(
            limit
        )
    )
    return list(result)


async def mark_ideas_notified(
    session: AsyncSession,
    telegram_id: int,
    ideas: list[TradingIdea],
    *,
    sent_at: datetime,
) -> None:
    session.add_all(
        [
            IdeaNotification(
                telegram_id=telegram_id,
                idea_id=idea.id,
                idea_version=idea.version,
                sent_at=sent_at,
            )
            for idea in ideas
        ]
    )


def idea_direction(idea: TradingIdea) -> IdeaDirection:
    return IdeaDirection(idea.direction)
