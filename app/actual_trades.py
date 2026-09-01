from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.journal import append_trade_event, create_actual_trade, list_trade_events
from app.models import ActualTradeJournal, IdeaJournal, ModelTradeJournal, TradeEventJournal
from app.v24_domain import ActualTradeAction, ActualTradeStatus, TradeEventType


class ActualTradeError(ValueError):
    pass


class ActualTradeNotFound(ActualTradeError):
    pass


class ActualTradeAlreadyExists(ActualTradeError):
    pass


class ActualTradeClosed(ActualTradeError):
    pass


@dataclass(frozen=True, slots=True)
class ActualTradeConfirmation:
    trade_id: str
    telegram_id: int
    confirmation_key: str
    entry_time: datetime
    entry_price: float
    position_shares: float | None = None
    position_rub: float | None = None
    commission_rub: float | None = None
    notes: str | None = None


@dataclass(frozen=True, slots=True)
class ActualActionConfirmation:
    actual_trade_id: str
    telegram_id: int
    confirmation_key: str
    action: ActualTradeAction
    event_time: datetime
    current_price: float | None = None
    stop_after: float | None = None
    tp_after: float | None = None
    position_after: float | None = None
    reason: str | None = None
    notes: str | None = None


@dataclass(frozen=True, slots=True)
class ActualPositionState:
    actual_trade_id: str
    trade_id: str
    ticker: str
    direction: str
    status: ActualTradeStatus
    entry_price: float
    entry_time: datetime
    position_value: float
    position_unit: str
    current_stop: float | None
    current_tp: float | None
    current_price: float | None
    last_event_time: datetime
    event_count: int


@dataclass(frozen=True, slots=True)
class ConfirmationResult:
    actual: ActualTradeJournal
    created: bool


@dataclass(frozen=True, slots=True)
class EventConfirmationResult:
    event: TradeEventJournal
    created: bool


def _positive(value: float | None, name: str, *, optional: bool = False) -> float | None:
    if value is None and optional:
        return None
    if value is None or isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        raise ActualTradeError(f"{name} должно быть положительным числом")
    return float(value)


def _non_negative(value: float | None, name: str, *, optional: bool = False) -> float | None:
    if value is None and optional:
        return None
    if value is None or isinstance(value, bool) or not math.isfinite(value) or value < 0:
        raise ActualTradeError(f"{name} не может быть отрицательным")
    return float(value)


def _entry_slippage_bps(direction: str, actual: float, model: float | None) -> float | None:
    if model is None or model <= 0:
        return None
    if direction == "LONG":
        return (actual - model) / model * 10_000
    return (model - actual) / model * 10_000


def _position_state(
    actual: ActualTradeJournal,
    idea: IdeaJournal,
    events: list[TradeEventJournal],
) -> ActualPositionState:
    if actual.actual_position_shares is not None:
        position_value = actual.actual_position_shares
        position_unit = "SHARES"
    elif actual.actual_position_rub is not None:
        position_value = actual.actual_position_rub
        position_unit = "RUB"
    else:  # The DB constraint is enforced by the service; old/manual rows remain explicit.
        position_value = 0.0
        position_unit = "UNKNOWN"
    stop = actual.initial_stop
    target = actual.tp1
    current_price: float | None = actual.actual_entry
    status = ActualTradeStatus.OPEN
    last_time = actual.actual_entry_time
    for event in events:
        last_time = event.event_datetime
        if event.current_price is not None:
            current_price = event.current_price
        if event.stop_after is not None:
            stop = event.stop_after
        if event.tp_after is not None:
            target = event.tp_after
        if event.position_after is not None:
            position_value = event.position_after
        if event.event_type == TradeEventType.FULL_EXIT.value:
            position_value = 0.0
            status = ActualTradeStatus.CLOSED
        elif event.event_type == TradeEventType.CANCEL.value:
            position_value = 0.0
            status = ActualTradeStatus.CANCELLED
    return ActualPositionState(
        actual_trade_id=actual.actual_trade_id,
        trade_id=actual.trade_id,
        ticker=idea.ticker,
        direction=idea.direction,
        status=status,
        entry_price=actual.actual_entry,
        entry_time=actual.actual_entry_time,
        position_value=position_value,
        position_unit=position_unit,
        current_stop=stop,
        current_tp=target,
        current_price=current_price,
        last_event_time=last_time,
        event_count=len(events),
    )


class ActualTradeService:
    """User-confirmed actual journal; it never places or infers broker orders."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def get_idea(self, trade_id: str) -> IdeaJournal:
        async with self.session_factory() as session:
            idea = await session.get(IdeaJournal, trade_id)
            if idea is None:
                raise ActualTradeNotFound("Торговая идея v2.4 не найдена")
            return idea

    async def confirm_entry(self, confirmation: ActualTradeConfirmation) -> ConfirmationResult:
        entry_price = _positive(confirmation.entry_price, "Фактическая цена")
        shares = _positive(confirmation.position_shares, "Количество акций", optional=True)
        rub = _positive(confirmation.position_rub, "Сумма позиции", optional=True)
        commission = _non_negative(
            confirmation.commission_rub,
            "Комиссия",
            optional=True,
        )
        if (shares is None) == (rub is None):
            raise ActualTradeError("Укажите либо количество акций, либо сумму позиции")
        if confirmation.entry_time.tzinfo is None:
            raise ActualTradeError("Время исполнения должно содержать timezone")
        key = confirmation.confirmation_key.strip()
        if not key:
            raise ActualTradeError("Не получен ключ Telegram-подтверждения")

        async with self.session_factory() as session, session.begin():
            existing_key = await session.scalar(
                select(ActualTradeJournal).where(ActualTradeJournal.confirmation_key == key)
            )
            if existing_key is not None:
                if (
                    existing_key.trade_id != confirmation.trade_id
                    or existing_key.confirmed_by_telegram_id != confirmation.telegram_id
                ):
                    raise ActualTradeError("Ключ подтверждения уже относится к другой сделке")
                return ConfirmationResult(existing_key, created=False)
            existing_trade = await session.scalar(
                select(ActualTradeJournal).where(
                    ActualTradeJournal.trade_id == confirmation.trade_id
                )
            )
            if existing_trade is not None:
                raise ActualTradeAlreadyExists("Эта фактическая позиция уже подтверждена")
            idea = await session.get(IdeaJournal, confirmation.trade_id)
            if idea is None:
                raise ActualTradeNotFound("Торговая идея v2.4 не найдена")
            model = await session.scalar(
                select(ModelTradeJournal).where(
                    ModelTradeJournal.trade_id == confirmation.trade_id
                )
            )
            model_entry = model.model_entry if model is not None else None
            position_rub = rub if rub is not None else shares * entry_price
            units_for_risk = shares if shares is not None else position_rub / entry_price
            initial_risk = (
                abs(entry_price - idea.initial_stop) * units_for_risk
                if idea.initial_stop is not None
                else None
            )
            actual = await create_actual_trade(
                session,
                trade_id=idea.trade_id,
                model_trade_id=model.model_trade_id if model is not None else None,
                confirmed_by_user=True,
                telegram_id=confirmation.telegram_id,
                confirmation_key=key,
                actual_entry_time=confirmation.entry_time,
                actual_entry=entry_price,
                values={
                    "actual_position_shares": shares,
                    "actual_position_rub": position_rub,
                    "model_entry": model_entry,
                    "entry_slippage_bps": _entry_slippage_bps(
                        idea.direction,
                        entry_price,
                        model_entry,
                    ),
                    "initial_stop": idea.initial_stop,
                    "tp1": idea.tp1,
                    "tp2": idea.tp2,
                    "actual_entry_costs": commission,
                    "initial_risk_rub": initial_risk,
                    "notes": confirmation.notes,
                },
            )
            return ConfirmationResult(actual, created=True)

    async def get_position(
        self,
        actual_trade_id: str,
        *,
        telegram_id: int,
    ) -> ActualPositionState:
        async with self.session_factory() as session:
            actual = await session.get(ActualTradeJournal, actual_trade_id)
            if actual is None or actual.confirmed_by_telegram_id != telegram_id:
                raise ActualTradeNotFound("Фактическая позиция не найдена")
            idea = await session.get(IdeaJournal, actual.trade_id)
            if idea is None:
                raise ActualTradeNotFound("Исходная идея не найдена")
            events = await list_trade_events(session, actual.trade_id)
            return _position_state(actual, idea, events)

    async def get_position_for_trade(
        self,
        trade_id: str,
        *,
        telegram_id: int,
    ) -> ActualPositionState | None:
        async with self.session_factory() as session:
            actual = await session.scalar(
                select(ActualTradeJournal).where(
                    ActualTradeJournal.trade_id == trade_id,
                    ActualTradeJournal.confirmed_by_telegram_id == telegram_id,
                )
            )
            if actual is None:
                return None
            idea = await session.get(IdeaJournal, trade_id)
            if idea is None:
                raise ActualTradeNotFound("Исходная идея не найдена")
            events = await list_trade_events(session, trade_id)
            return _position_state(actual, idea, events)

    async def record_action(
        self,
        confirmation: ActualActionConfirmation,
    ) -> EventConfirmationResult:
        if confirmation.event_time.tzinfo is None:
            raise ActualTradeError("Время действия должно содержать timezone")
        key = confirmation.confirmation_key.strip()
        if not key:
            raise ActualTradeError("Не получен ключ Telegram-подтверждения")
        current_price = _positive(
            confirmation.current_price,
            "Цена исполнения",
            optional=True,
        )
        stop_after = _positive(confirmation.stop_after, "Новый стоп", optional=True)
        tp_after = _positive(confirmation.tp_after, "Новая цель", optional=True)
        position_after = _non_negative(
            confirmation.position_after,
            "Остаток позиции",
            optional=True,
        )

        async with self.session_factory() as session, session.begin():
            existing = await session.scalar(
                select(TradeEventJournal).where(TradeEventJournal.confirmation_key == key)
            )
            if existing is not None:
                if (
                    existing.actual_trade_id != confirmation.actual_trade_id
                    or existing.confirmed_by_telegram_id != confirmation.telegram_id
                ):
                    raise ActualTradeError("Ключ подтверждения уже относится к другому действию")
                return EventConfirmationResult(existing, created=False)
            actual = await session.get(ActualTradeJournal, confirmation.actual_trade_id)
            if actual is None or actual.confirmed_by_telegram_id != confirmation.telegram_id:
                raise ActualTradeNotFound("Фактическая позиция не найдена")
            idea = await session.get(IdeaJournal, actual.trade_id)
            if idea is None:
                raise ActualTradeNotFound("Исходная идея не найдена")
            events = await list_trade_events(session, actual.trade_id)
            state = _position_state(actual, idea, events)
            if state.status is not ActualTradeStatus.OPEN:
                raise ActualTradeClosed("Позиция уже закрыта или отменена")

            event_type, reason = self._event_mapping(confirmation.action)
            if confirmation.action in {
                ActualTradeAction.MOVE_STOP,
                ActualTradeAction.LOCK_PROFIT,
            } and stop_after is None:
                raise ActualTradeError("Для изменения стопа нужна новая фактическая цена стопа")
            if confirmation.action is ActualTradeAction.BREAK_EVEN:
                stop_after = actual.actual_entry
            if confirmation.action in {
                ActualTradeAction.PARTIAL_CLOSE,
                ActualTradeAction.REDUCE,
            }:
                if current_price is None or position_after is None:
                    raise ActualTradeError("Нужны фактическая цена и остаток позиции")
                if position_after >= state.position_value:
                    raise ActualTradeError("Остаток должен быть меньше текущей позиции")
            if confirmation.action is ActualTradeAction.FULL_CLOSE:
                if current_price is None:
                    raise ActualTradeError("Нужна фактическая цена полного закрытия")
                position_after = 0.0
            if confirmation.action is ActualTradeAction.CANCEL:
                position_after = 0.0

            metadata = {
                "action": confirmation.action.value,
                "position_unit": state.position_unit,
                "user_notes": confirmation.notes,
            }
            event = await append_trade_event(
                session,
                trade_id=actual.trade_id,
                model_trade_id=actual.model_trade_id,
                actual_trade_id=actual.actual_trade_id,
                confirmed_by_telegram_id=confirmation.telegram_id,
                confirmation_key=key,
                event_datetime=confirmation.event_time,
                event_type=event_type,
                values={
                    "current_price": current_price,
                    "stop_before": state.current_stop,
                    "stop_after": stop_after,
                    "tp_before": state.current_tp,
                    "tp_after": tp_after,
                    "position_before": state.position_value,
                    "position_after": position_after,
                    "reason": confirmation.reason or reason,
                    "source_or_broker_note": "TELEGRAM_USER_CONFIRMATION",
                    "notes": json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                },
            )
            return EventConfirmationResult(event, created=True)

    @staticmethod
    def _event_mapping(action: ActualTradeAction) -> tuple[TradeEventType, str]:
        return {
            ActualTradeAction.HOLD: (TradeEventType.THESIS_UPDATE, "USER_CONFIRMED_HOLD"),
            ActualTradeAction.MOVE_STOP: (TradeEventType.STOP_MOVE, "USER_CONFIRMED_STOP_MOVE"),
            ActualTradeAction.BREAK_EVEN: (
                TradeEventType.STOP_MOVE,
                "USER_CONFIRMED_BREAK_EVEN",
            ),
            ActualTradeAction.LOCK_PROFIT: (
                TradeEventType.STOP_MOVE,
                "USER_CONFIRMED_LOCK_PROFIT",
            ),
            ActualTradeAction.PARTIAL_CLOSE: (
                TradeEventType.PARTIAL_EXIT,
                "USER_CONFIRMED_PARTIAL_CLOSE",
            ),
            ActualTradeAction.REDUCE: (TradeEventType.REDUCE, "USER_CONFIRMED_REDUCE"),
            ActualTradeAction.FULL_CLOSE: (
                TradeEventType.FULL_EXIT,
                "USER_CONFIRMED_FULL_CLOSE",
            ),
            ActualTradeAction.CANCEL: (TradeEventType.CANCEL, "USER_CONFIRMED_CANCEL"),
        }[action]
