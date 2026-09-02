from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ingestion import IngestionService
from app.journal import append_trade_event
from app.journal_health import JournalHealthService
from app.kill_switch import KillSwitchService
from app.model_execution_v24 import ModelExecutionRequest, ModelOrderType, simulate_model_execution
from app.models import ActualTradeJournal, IdeaJournal, ModelTradeJournal, TradeEventJournal
from app.orchestrator_v24 import IntradayV24Orchestrator
from app.repositories import get_candles_after
from app.v24_domain import (
    JournalDirection,
    KillSwitchReason,
    KillSwitchState,
    TradeEventType,
)

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


class V24ModelLifecycleService:
    """Replay newly ingested candles into mutable model outcomes and append-only events."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def track(self) -> dict[str, Any]:
        async with self.session_factory() as session:
            active = list(
                await session.scalars(
                    select(ModelTradeJournal).where(ModelTradeJournal.final_exit_time.is_(None))
                )
            )
        counters = {"model_active_pending": len(active), "updated": 0, "closed": 0, "errors": 0}
        for current in active:
            try:
                async with self.session_factory() as session, session.begin():
                    model = await session.get(ModelTradeJournal, current.model_trade_id)
                    idea = await session.get(IdeaJournal, current.trade_id)
                    if model is None or idea is None:
                        continue
                    if model.initial_stop is None or model.tp1 is None:
                        continue
                    entry = model.model_entry or idea.optimal_entry
                    if entry is None:
                        continue
                    candles = await get_candles_after(
                        session,
                        idea.ticker,
                        "5m",
                        idea.signal_datetime,
                    )
                    lower = await get_candles_after(
                        session,
                        idea.ticker,
                        "1m",
                        idea.signal_datetime,
                    )
                    if not candles:
                        continue
                    result = simulate_model_execution(
                        ModelExecutionRequest(
                            direction=JournalDirection(idea.direction),
                            order_type=ModelOrderType(model.model_order_type or "LIMIT"),
                            entry=entry,
                            stop=model.initial_stop,
                            tp1=model.tp1,
                            candles=tuple(candles),
                            lower_timeframe_candles=tuple(lower),
                            reliable_fill_data=False,
                        )
                    )
                    old_entry = model.model_entry_time
                    old_exit = model.final_exit_time
                    model.model_fill_status = result.fill_status.value
                    model.model_entry_time = result.entry_time
                    model.model_entry = result.entry_fill
                    model.ambiguous_execution = result.ambiguous_execution
                    model.calibration_eligible = result.calibration_eligible
                    model.gap_slippage = result.gap_slippage
                    if result.exit_time is not None and result.exit_fill is not None:
                        model.final_exit_time = result.exit_time
                        model.final_exit = result.exit_fill
                        model.exit_reason = result.exit_reason
                        if result.entry_fill is not None:
                            multiplier = 1 if idea.direction == "LONG" else -1
                            movement = multiplier * (result.exit_fill - result.entry_fill)
                            risk = abs(result.entry_fill - model.initial_stop)
                            model.return_pct = movement / result.entry_fill * 100
                            model.result_r = movement / risk if risk > 0 else None
                            model.win_1_0 = int(movement > 0)
                            model.tp_before_sl_1_0 = int(result.exit_reason == "TP1")
                            if model.stated_probability is not None:
                                model.brier_score = (
                                    model.stated_probability - model.tp_before_sl_1_0
                                ) ** 2
                    if old_entry is None and result.entry_time is not None:
                        await append_trade_event(
                            session,
                            trade_id=idea.trade_id,
                            model_trade_id=model.model_trade_id,
                            event_type=TradeEventType.ENTRY,
                            event_datetime=result.entry_time,
                            values={
                                "current_price": result.entry_fill,
                                "reason": "MODEL_LIMIT_TOUCH",
                            },
                        )
                    if old_exit is None and result.exit_time is not None:
                        await append_trade_event(
                            session,
                            trade_id=idea.trade_id,
                            model_trade_id=model.model_trade_id,
                            event_type=TradeEventType.FULL_EXIT,
                            event_datetime=result.exit_time,
                            values={
                                "current_price": result.exit_fill,
                                "reason": result.exit_reason or "MODEL_EXIT",
                            },
                        )
                    counters["updated"] += 1
                    counters["closed"] += int(old_exit is None and result.exit_time is not None)
            except Exception:
                counters["errors"] += 1
                logger.exception("v24 model lifecycle failed model=%s", current.model_trade_id)
        return counters

    async def model_active_pending(self) -> dict[str, Any]:
        async with self.session_factory() as session:
            rows = list(
                await session.scalars(
                    select(ModelTradeJournal).where(ModelTradeJournal.final_exit_time.is_(None))
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
        orchestrator: IntradayV24Orchestrator,
    ) -> V24SchedulerCoordinator:
        recovery = V24JournalRecoveryService(session_factory)
        lifecycle = V24ModelLifecycleService(session_factory)

        async def ingest_and_scan() -> dict[str, Any]:
            ingestion_result = await ingestion.sync_intraday_v24()
            scan_result = await orchestrator.scan()
            return {
                "ingestion": ingestion_result,
                **scan_result,
                "errors": int(ingestion_result.get("errors", 0))
                + int(scan_result.get("errors", 0)),
            }

        return cls(
            journal_health=JournalHealthService(session_factory),
            kill_switch=KillSwitchService(session_factory),
            actual_handler=recovery.actual_open,
            model_handler=lifecycle.track,
            scan_handler=ingest_and_scan,
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
            "errors": (
                (0 if health.available else 1)
                + int(actual.get("errors", 0))
                + int(model.get("errors", 0))
                + int(scan.get("errors", 0))
            ),
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
