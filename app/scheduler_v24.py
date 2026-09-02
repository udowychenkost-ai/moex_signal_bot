from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ingestion import IngestionService
from app.journal_health import JournalHealthService
from app.kill_switch import KillSwitchService
from app.models import ActualTradeJournal, ModelTradeJournal, TradeEventJournal
from app.v24_domain import KillSwitchReason, KillSwitchState, TradeEventType

logger = logging.getLogger(__name__)


PriorityHandler = Callable[[], Awaitable[dict[str, Any]]]


class V24JournalRecoveryService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def actual_open(self) -> dict[str, Any]:
        async with self.session_factory() as session:
            actual = list(await session.scalars(select(ActualTradeJournal)))
            closed_ids = set(
                await session.scalars(
                    select(TradeEventJournal.actual_trade_id).where(
                        TradeEventJournal.actual_trade_id.is_not(None),
                        TradeEventJournal.event_type.in_(
                            (TradeEventType.FULL_EXIT.value, TradeEventType.CANCEL.value)
                        ),
                    )
                )
            )
        return {
            "actual_open": sum(item.actual_trade_id not in closed_ids for item in actual),
            "recovered": True,
        }

    async def model_active_pending(self) -> dict[str, Any]:
        async with self.session_factory() as session:
            rows = list(
                await session.scalars(
                    select(ModelTradeJournal).where(
                        ModelTradeJournal.final_exit_time.is_(None)
                    )
                )
            )
        return {
            "model_active_pending": len(rows),
            "recovered": True,
        }


class V24SchedulerCoordinator:
    """Priority is actual positions, model trades, then the isolated v2.4 scan."""

    def __init__(
        self,
        *,
        journal_health: JournalHealthService,
        kill_switch: KillSwitchService,
        actual_handler: PriorityHandler,
        model_handler: PriorityHandler,
        scan_handler: PriorityHandler,
    ) -> None:
        self.journal_health = journal_health
        self.kill_switch = kill_switch
        self.actual_handler = actual_handler
        self.model_handler = model_handler
        self.scan_handler = scan_handler

    @classmethod
    def from_services(
        cls,
        session_factory: async_sessionmaker[AsyncSession],
        ingestion: IngestionService,
    ) -> V24SchedulerCoordinator:
        recovery = V24JournalRecoveryService(session_factory)
        return cls(
            journal_health=JournalHealthService(session_factory),
            kill_switch=KillSwitchService(session_factory),
            actual_handler=recovery.actual_open,
            model_handler=recovery.model_active_pending,
            scan_handler=ingestion.sync_intraday_v24,
        )

    async def run(self) -> dict[str, Any]:
        health = await self.journal_health.check()
        current = await self.kill_switch.current()
        reasons = set(current.reasons)
        if not health.available:
            reasons.add(KillSwitchReason.JOURNAL_ERROR)
        else:
            reasons.discard(KillSwitchReason.JOURNAL_ERROR)
        kill = await self.kill_switch.evaluate(
            reasons,
            source="V24_SCHEDULER_PREFLIGHT",
            checked_at=datetime.now(UTC),
            details={
                "journal_available": health.available,
                "alembic_revision": health.revision,
            },
        )
        order: list[str] = []
        actual = await self.actual_handler()
        order.append("ACTUAL_OPEN")
        model = await self.model_handler()
        order.append("MODEL_ACTIVE_PENDING")
        scan: dict[str, Any]
        if kill.state is KillSwitchState.CAPITAL_PRESERVATION:
            scan = {"skipped": True, "reason": "CAPITAL_PRESERVATION"}
        else:
            scan = await self.scan_handler()
            order.append("NEW_SCAN")
        result = {
            "priority_order": order,
            "journal": "AVAILABLE" if health.available else "ERROR",
            "kill_switch": kill.state.value,
            "actual": actual,
            "model": model,
            "scan": scan,
            "errors": 0 if health.available else 1,
        }
        logger.info(
            "v24 priority cycle actual_open=%s model_active_pending=%s scan_skipped=%s "
            "journal=%s kill_switch=%s",
            actual.get("actual_open", 0),
            model.get("model_active_pending", 0),
            scan.get("skipped", False),
            result["journal"],
            result["kill_switch"],
        )
        return result
