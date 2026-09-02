from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import KillSwitchEvent
from app.v24_domain import KillSwitchReason, KillSwitchState


@dataclass(frozen=True, slots=True)
class KillSwitchStatus:
    initialized: bool
    state: KillSwitchState
    reasons: tuple[KillSwitchReason, ...]
    source: str | None
    triggered_at: datetime | None
    event_id: int | None

    @property
    def allows_new_positions(self) -> bool:
        return self.initialized and self.state is KillSwitchState.NORMAL


class KillSwitchService:
    """Persistent fail-closed state for new v2.4 positions only."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def current(self) -> KillSwitchStatus:
        async with self.session_factory() as session:
            event = await session.scalar(
                select(KillSwitchEvent)
                .order_by(KillSwitchEvent.triggered_at.desc(), KillSwitchEvent.id.desc())
                .limit(1)
            )
        if event is None:
            return KillSwitchStatus(
                initialized=False,
                state=KillSwitchState.CAPITAL_PRESERVATION,
                reasons=(),
                source=None,
                triggered_at=None,
                event_id=None,
            )
        raw_reasons = json.loads(event.reasons)
        reasons = tuple(KillSwitchReason(item) for item in raw_reasons)
        return KillSwitchStatus(
            initialized=True,
            state=KillSwitchState(event.state),
            reasons=reasons,
            source=event.source,
            triggered_at=event.triggered_at,
            event_id=event.id,
        )

    async def evaluate(
        self,
        reasons: set[KillSwitchReason],
        *,
        source: str,
        checked_at: datetime | None = None,
        details: dict[str, object] | None = None,
        created_by_telegram_id: int | None = None,
    ) -> KillSwitchStatus:
        if not source.strip():
            raise ValueError("Kill switch source is required")
        timestamp = checked_at or datetime.now(UTC)
        if timestamp.tzinfo is None:
            raise ValueError("Kill switch timestamp must be timezone-aware")
        ordered_reasons = tuple(sorted(reasons, key=lambda item: item.value))
        desired_state = (
            KillSwitchState.CAPITAL_PRESERVATION if ordered_reasons else KillSwitchState.NORMAL
        )
        current = await self.current()
        if (
            current.initialized
            and current.state is desired_state
            and current.reasons == ordered_reasons
        ):
            return current
        async with self.session_factory() as session, session.begin():
            event = KillSwitchEvent(
                state=desired_state.value,
                reasons=json.dumps(
                    [item.value for item in ordered_reasons],
                    separators=(",", ":"),
                ),
                source=source.strip(),
                details=(
                    json.dumps(details, ensure_ascii=False, sort_keys=True)
                    if details is not None
                    else None
                ),
                triggered_at=timestamp,
                created_by_telegram_id=created_by_telegram_id,
            )
            session.add(event)
            await session.flush()
            event_id = event.id
        return KillSwitchStatus(
            initialized=True,
            state=desired_state,
            reasons=ordered_reasons,
            source=source.strip(),
            triggered_at=timestamp,
            event_id=event_id,
        )

    async def activate_manual(
        self,
        reason: KillSwitchReason,
        *,
        telegram_id: int,
        activated_at: datetime | None = None,
        notes: str | None = None,
    ) -> KillSwitchStatus:
        status = await self.evaluate(
            {reason},
            source="TELEGRAM_MANUAL",
            checked_at=activated_at,
            details={"notes": notes} if notes else None,
            created_by_telegram_id=telegram_id,
        )
        return status

    async def clear_manual(
        self,
        *,
        telegram_id: int,
        cleared_at: datetime | None = None,
        notes: str | None = None,
    ) -> KillSwitchStatus:
        return await self.evaluate(
            set(),
            source="TELEGRAM_MANUAL_CLEAR",
            checked_at=cleared_at,
            details={"notes": notes} if notes else None,
            created_by_telegram_id=telegram_id,
        )
