from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import UTC, date, datetime
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ActualTradeJournal,
    DecisionSnapshotV24,
    IdeaJournal,
    ModelTradeJournal,
    TradeEventJournal,
    TradeIdSequence,
)
from app.v24_domain import (
    STRATEGY_VERSION_V24,
    FillStatus,
    HistoricalEvidenceStatus,
    JournalDirection,
    SampleType,
    TradeEventType,
)

MOSCOW_TZ = ZoneInfo("Europe/Moscow")
TICKER_PATTERN = re.compile(r"^[A-Z0-9._-]{1,36}$")

IDEA_JSON_FIELDS = {"source_set", "fundamental_context", "news_context"}
SNAPSHOT_JSON_FIELDS = {
    "data_sla",
    "sources",
    "entry",
    "liquidity_inputs",
    "risk_inputs",
    "news_context",
    "event_context",
    "gate_results",
}


class ActualTradeConfirmationRequired(ValueError):
    pass


def _enum_value(value: object) -> object:
    return value.value if isinstance(value, Enum) else value


def _json_text(value: object) -> str | None:
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _prepare_values(values: Mapping[str, Any], json_fields: set[str]) -> dict[str, Any]:
    prepared: dict[str, Any] = {}
    for key, value in values.items():
        normalized = _enum_value(value)
        prepared[key] = _json_text(normalized) if key in json_fields else normalized
    return prepared


def _normalize_signal_time(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=MOSCOW_TZ)
    return value


def _trade_date(value: datetime) -> date:
    return _normalize_signal_time(value).astimezone(MOSCOW_TZ).date()


def normalize_direction(value: str | JournalDirection) -> JournalDirection:
    normalized = str(_enum_value(value)).strip().upper()
    if normalized in {"BUY", "LONG"}:
        return JournalDirection.LONG
    if normalized in {"SELL", "SHORT"}:
        return JournalDirection.SHORT
    raise ValueError(f"Unsupported journal direction: {value!r}")


def normalize_ticker(value: str) -> str:
    ticker = value.strip().upper()
    if not TICKER_PATTERN.fullmatch(ticker):
        raise ValueError("Ticker must contain only A-Z, 0-9, dot, underscore or hyphen")
    return ticker


async def allocate_trade_id(
    session: AsyncSession,
    *,
    signal_datetime: datetime,
    ticker: str,
    direction: str | JournalDirection,
) -> str:
    """Allocate a concurrency-safe ID in the caller's current transaction."""

    normalized_ticker = normalize_ticker(ticker)
    normalized_direction = normalize_direction(direction)
    signal_date = _trade_date(signal_datetime)
    values = {
        "trade_date": signal_date,
        "ticker": normalized_ticker,
        "direction": normalized_direction.value,
        "last_sequence": 1,
    }
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        statement = postgresql_insert(TradeIdSequence).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=["trade_date", "ticker", "direction"],
            set_={"last_sequence": TradeIdSequence.last_sequence + 1},
        ).returning(TradeIdSequence.last_sequence)
        sequence = (await session.execute(statement)).scalar_one()
    elif dialect == "sqlite":
        statement = sqlite_insert(TradeIdSequence).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=["trade_date", "ticker", "direction"],
            set_={"last_sequence": TradeIdSequence.last_sequence + 1},
        ).returning(TradeIdSequence.last_sequence)
        sequence = (await session.execute(statement)).scalar_one()
    else:
        query = select(TradeIdSequence).where(
            TradeIdSequence.trade_date == signal_date,
            TradeIdSequence.ticker == normalized_ticker,
            TradeIdSequence.direction == normalized_direction.value,
        )
        row = (await session.execute(query.with_for_update())).scalar_one_or_none()
        if row is None:
            row = TradeIdSequence(**values)
            session.add(row)
            sequence = 1
        else:
            row.last_sequence += 1
            sequence = row.last_sequence
        await session.flush()

    return f"{signal_date:%Y%m%d}-{normalized_ticker}-{normalized_direction.value}-{sequence:02d}"


def _without_reserved(values: Mapping[str, Any], reserved: set[str]) -> dict[str, Any]:
    overlap = set(values) & reserved
    if overlap:
        fields = ", ".join(sorted(overlap))
        raise ValueError(f"Reserved journal fields cannot be overridden: {fields}")
    return dict(values)


async def create_idea_journal(
    session: AsyncSession,
    *,
    signal_datetime: datetime,
    ticker: str,
    direction: str | JournalDirection,
    strategy_version: str = STRATEGY_VERSION_V24,
    journal_values: Mapping[str, Any] | None = None,
    snapshot_values: Mapping[str, Any] | None = None,
) -> tuple[IdeaJournal, DecisionSnapshotV24]:
    """Persist the immutable initial card and evidence in one transaction."""

    normalized_time = _normalize_signal_time(signal_datetime)
    normalized_ticker = normalize_ticker(ticker)
    normalized_direction = normalize_direction(direction)
    trade_id = await allocate_trade_id(
        session,
        signal_datetime=normalized_time,
        ticker=normalized_ticker,
        direction=normalized_direction,
    )
    journal_extra = _without_reserved(
        journal_values or {},
        {"trade_id", "strategy_version", "signal_datetime", "ticker", "direction"},
    )
    journal_extra = _prepare_values(journal_extra, IDEA_JSON_FIELDS)
    idea = IdeaJournal(
        trade_id=trade_id,
        strategy_version=strategy_version,
        signal_datetime=normalized_time,
        ticker=normalized_ticker,
        direction=normalized_direction.value,
        **journal_extra,
    )

    supplied_snapshot = _without_reserved(
        snapshot_values or {},
        {"trade_id", "strategy_version", "signal_datetime", "ticker", "direction"},
    )
    inherited_snapshot: dict[str, Any] = {
        "price_as_of": journal_extra.get("price_as_of"),
        "data_delay_seconds": journal_extra.get("data_delay_seconds"),
        "data_sla": {
            "status": journal_extra.get("data_sla_status"),
            "result": journal_extra.get("data_sla_result"),
        },
        "sources": journal_extra.get("source_set"),
        "market_regime": journal_extra.get("market_regime"),
        "market_bias": journal_extra.get("market_bias"),
        "setup": journal_extra.get("setup"),
        "setup_quality": journal_extra.get("setup_quality"),
        "execution_quality": journal_extra.get("execution_quality"),
        "entry": {
            "optimal": journal_extra.get("optimal_entry"),
            "acceptable": journal_extra.get("acceptable_entry"),
            "no_chase": journal_extra.get("no_chase_level"),
        },
        "stop": journal_extra.get("initial_stop"),
        "tp1": journal_extra.get("tp1"),
        "tp2": journal_extra.get("tp2"),
        "news_context": journal_extra.get("news_context"),
        "calibration_group": journal_extra.get("calibration_group"),
        "final_decision": journal_extra.get("final_decision"),
        "gate_results": {},
        "evidence_status": HistoricalEvidenceStatus.AVAILABLE.value,
    }
    inherited_snapshot.update(supplied_snapshot)
    snapshot_extra = _prepare_values(inherited_snapshot, SNAPSHOT_JSON_FIELDS)
    # Flush the parent explicitly. The models intentionally have no mutable ORM
    # relationship, so unit-of-work ordering must not depend on relationship state.
    session.add(idea)
    await session.flush()
    snapshot = DecisionSnapshotV24(
        trade_id=trade_id,
        strategy_version=strategy_version,
        signal_datetime=normalized_time,
        ticker=normalized_ticker,
        direction=normalized_direction.value,
        **snapshot_extra,
    )
    session.add(snapshot)
    await session.flush()
    return idea, snapshot


async def create_model_trade(
    session: AsyncSession,
    *,
    trade_id: str,
    sample_type: SampleType | str = SampleType.FORWARD,
    fill_status: FillStatus | str = FillStatus.NOT_FILLED,
    calibration_eligible: bool = False,
    values: Mapping[str, Any] | None = None,
) -> ModelTradeJournal:
    idea = await session.get(IdeaJournal, trade_id)
    if idea is None:
        raise ValueError(f"Unknown trade_id: {trade_id}")
    extra = _without_reserved(
        values or {},
        {
            "model_trade_id",
            "trade_id",
            "strategy_version",
            "sample_type",
            "model_fill_status",
            "calibration_eligible",
        },
    )
    model_trade = ModelTradeJournal(
        model_trade_id=f"M-{trade_id}",
        trade_id=trade_id,
        strategy_version=idea.strategy_version,
        sample_type=SampleType(str(_enum_value(sample_type))).value,
        model_fill_status=FillStatus(str(_enum_value(fill_status))).value,
        calibration_eligible=calibration_eligible,
        **_prepare_values(extra, set()),
    )
    session.add(model_trade)
    await session.flush()
    return model_trade


async def append_trade_event(
    session: AsyncSession,
    *,
    trade_id: str,
    event_type: TradeEventType | str,
    event_datetime: datetime,
    model_trade_id: str | None = None,
    actual_trade_id: str | None = None,
    confirmed_by_telegram_id: int | None = None,
    confirmation_key: str | None = None,
    values: Mapping[str, Any] | None = None,
) -> TradeEventJournal:
    if await session.get(IdeaJournal, trade_id) is None:
        raise ValueError(f"Unknown trade_id: {trade_id}")
    if model_trade_id is not None:
        model = await session.get(ModelTradeJournal, model_trade_id)
        if model is None or model.trade_id != trade_id:
            raise ValueError("model_trade_id does not belong to trade_id")
    if actual_trade_id is not None:
        actual = await session.get(ActualTradeJournal, actual_trade_id)
        if actual is None or actual.trade_id != trade_id:
            raise ValueError("actual_trade_id does not belong to trade_id")
        if confirmed_by_telegram_id is not None and (
            actual.confirmed_by_telegram_id != confirmed_by_telegram_id
        ):
            raise ValueError("Only the user who confirmed the actual trade can append its events")
    if confirmation_key is not None and not confirmation_key.strip():
        raise ValueError("confirmation_key cannot be blank")
    extra = _without_reserved(
        values or {},
        {
            "event_id",
            "trade_id",
            "model_trade_id",
            "actual_trade_id",
            "confirmed_by_telegram_id",
            "confirmation_key",
            "event_datetime",
            "event_type",
        },
    )
    event = TradeEventJournal(
        trade_id=trade_id,
        model_trade_id=model_trade_id,
        actual_trade_id=actual_trade_id,
        confirmed_by_telegram_id=confirmed_by_telegram_id,
        confirmation_key=confirmation_key.strip() if confirmation_key is not None else None,
        event_datetime=_normalize_signal_time(event_datetime),
        event_type=TradeEventType(str(_enum_value(event_type))).value,
        **_prepare_values(extra, set()),
    )
    session.add(event)
    await session.flush()
    return event


async def create_actual_trade(
    session: AsyncSession,
    *,
    trade_id: str,
    confirmed_by_user: bool,
    telegram_id: int,
    confirmation_key: str,
    actual_entry_time: datetime,
    actual_entry: float,
    model_trade_id: str | None = None,
    values: Mapping[str, Any] | None = None,
) -> ActualTradeJournal:
    """Create an actual record only from explicit Telegram confirmation."""

    if confirmed_by_user is not True:
        raise ActualTradeConfirmationRequired(
            "ActualTrade requires explicit user confirmation; automatic activation is forbidden"
        )
    if not confirmation_key.strip():
        raise ValueError("confirmation_key is required for idempotent Telegram confirmation")
    if telegram_id <= 0:
        raise ValueError("telegram_id must identify the confirming user")
    if actual_entry <= 0:
        raise ValueError("actual_entry must be positive")
    idea = await session.get(IdeaJournal, trade_id)
    if idea is None:
        raise ValueError(f"Unknown trade_id: {trade_id}")
    if model_trade_id is not None:
        model = await session.get(ModelTradeJournal, model_trade_id)
        if model is None or model.trade_id != trade_id:
            raise ValueError("model_trade_id does not belong to trade_id")
    extra = _without_reserved(
        values or {},
        {
            "actual_trade_id",
            "trade_id",
            "model_trade_id",
            "strategy_version",
            "confirmed_by_telegram_id",
            "confirmation_key",
            "confirmed_at",
            "actual_entry_time",
            "actual_entry",
        },
    )
    position_rub = extra.get("actual_position_rub")
    position_shares = extra.get("actual_position_shares")
    if not any(
        isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0
        for value in (position_rub, position_shares)
    ):
        raise ValueError("actual_position_rub or actual_position_shares must be confirmed")
    now = datetime.now(UTC)
    actual = ActualTradeJournal(
        actual_trade_id=f"A-{trade_id}",
        model_trade_id=model_trade_id,
        trade_id=trade_id,
        strategy_version=idea.strategy_version,
        confirmed_by_telegram_id=telegram_id,
        confirmation_key=confirmation_key.strip(),
        confirmed_at=now,
        actual_entry_time=_normalize_signal_time(actual_entry_time),
        actual_entry=actual_entry,
        **_prepare_values(extra, set()),
    )
    session.add(actual)
    await session.flush()
    await append_trade_event(
        session,
        trade_id=trade_id,
        model_trade_id=model_trade_id,
        actual_trade_id=actual.actual_trade_id,
        confirmed_by_telegram_id=telegram_id,
        confirmation_key=confirmation_key,
        event_datetime=actual.actual_entry_time,
        event_type=TradeEventType.ENTRY,
        values={
            "current_price": actual_entry,
            "position_after": (
                actual.actual_position_shares
                if actual.actual_position_shares is not None
                else actual.actual_position_rub
            ),
            "reason": "USER_CONFIRMED_ENTRY",
            "source_or_broker_note": "TELEGRAM_USER_CONFIRMATION",
            "notes": json.dumps(
                {
                    "position_unit": (
                        "SHARES" if actual.actual_position_shares is not None else "RUB"
                    )
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        },
    )
    return actual


async def list_trade_events(session: AsyncSession, trade_id: str) -> list[TradeEventJournal]:
    result = await session.execute(
        select(TradeEventJournal)
        .where(TradeEventJournal.trade_id == trade_id)
        .order_by(TradeEventJournal.event_datetime, TradeEventJournal.event_id)
    )
    return list(result.scalars())
