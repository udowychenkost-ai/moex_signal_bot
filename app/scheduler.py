from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import Settings
from app.operations import OperationalService
from app.orderbook_ingestion import OrderBookIngestionService
from app.scanner import MarketScanner
from app.scheduler_v24 import V24SchedulerCoordinator

logger = logging.getLogger(__name__)


class ScheduledJobs:
    def __init__(
        self,
        settings: Settings,
        scanner: MarketScanner,
        reporting: Any,
        bot: Bot,
        operations: OperationalService | None = None,
        orderbooks: OrderBookIngestionService | None = None,
        v24_coordinator: V24SchedulerCoordinator | None = None,
    ) -> None:
        self.settings = settings
        self.scanner = scanner
        self.reporting = reporting
        self.bot = bot
        self.operations = operations
        self.orderbooks = orderbooks
        self.v24_coordinator = v24_coordinator

    async def _run(
        self,
        job_name: str,
        operation: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        if self.operations is not None:
            await self.operations.job_started(job_name)
        try:
            result = await operation()
            reported_errors = sum(
                int(value)
                for key, value in result.items()
                if "error" in key and key != "ai_attempt_errors" and isinstance(value, int | float)
            )
            successful = reported_errors == 0
            if self.operations is not None:
                await self.operations.job_finished(
                    job_name,
                    success=successful,
                    details=result,
                    error=(
                        f"Operation reported {reported_errors} error(s)" if reported_errors else ""
                    ),
                )
            log = logger.info if successful else logger.error
            log("Scheduled job %s complete: %s", job_name, result)
            return result
        except Exception as error:
            if self.operations is not None:
                await self.operations.job_finished(
                    job_name,
                    success=False,
                    error=f"{type(error).__name__}: {error}",
                )
            logger.exception("Scheduled job %s failed", job_name)
            return {"errors": 1}

    async def ingest_market(self) -> dict[str, Any]:
        return await self._run("market_ingestion", self.scanner.ingest)

    async def ingest_orderbooks(self) -> dict[str, Any]:
        if self.orderbooks is None:
            return {"enabled": False, "status": "DISABLED", "errors": 0}
        return await self._run("order_book_ingestion", self.orderbooks.sync_all)

    async def scan_market(self) -> dict[str, Any]:
        result = await self._run("idea_scanning", self.scanner.scan_ideas)
        logger.info(
            "scan completed checked=%s candidates=%s pass=%s weak=%s reject=%s "
            "ai_approve=%s ai_wait=%s ai_reject=%s ai_error=%s published=%s",
            result.get("checked_instruments", 0),
            result.get("quant_candidates", 0),
            result.get("quality_pass", 0),
            result.get("quality_weak", 0),
            result.get("quality_reject", 0),
            int(result.get("ai_approve", 0)) + int(result.get("ai_strong_approve", 0)),
            result.get("ai_wait", 0),
            result.get("ai_rejected", 0),
            int(result.get("ai_errors", 0)) + int(result.get("ai_not_reviewed", 0)),
            result.get("published", result.get("ideas_created", 0)),
        )
        return result

    async def intraday_v24_cycle(self) -> dict[str, Any]:
        if self.v24_coordinator is None:
            return {"enabled": False, "status": "NOT_CONFIGURED", "errors": 0}
        return await self._run("intraday_v24_cycle", self.v24_coordinator.run)

    async def track_lifecycle(self) -> dict[str, Any]:
        async def track_and_paper() -> dict[str, Any]:
            tracking = await self.scanner.track_lifecycle()
            paper = await self.scanner.sync_paper()
            return {**tracking, "paper_open": paper["open"], "paper_closed": paper["closed"]}

        return await self._run("lifecycle_tracking", track_and_paper)

    async def dispatch_reports(self) -> dict[str, Any]:
        operation = getattr(self.reporting, "dispatch_notifications", None)
        if operation is None:
            operation = self.reporting.dispatch_due
        return await self._run("telegram_reporting", lambda: operation(self.bot))

    async def daily_summary(self) -> dict[str, Any]:
        operation = getattr(self.reporting, "dispatch_daily_summary", None)
        if operation is None:
            return {"sent": 0, "errors": 0}
        return await self._run("daily_summary", lambda: operation(self.bot))

    async def v24_market_summary(self) -> dict[str, Any]:
        operation = getattr(self.reporting, "dispatch_v24_market_summary", None)
        if operation is None:
            return {"queued": 0, "sent": 0, "errors": 0}
        return await self._run("v24_market_summary", lambda: operation(self.bot))


def build_scheduler(settings: Settings, jobs: ScheduledJobs) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=settings.scheduler_timezone)
    now = datetime.now(UTC)
    common = {
        "max_instances": 1,
        "coalesce": True,
        "misfire_grace_time": 180,
    }
    scheduler.add_job(
        jobs.ingest_market,
        trigger="cron",
        day_of_week="mon-fri",
        hour="9-19",
        minute=f"*/{settings.ingestion_interval_minutes}",
        second=0,
        id="market_ingestion",
        next_run_time=now,
        **common,
    )
    if settings.enable_orderbook:
        scheduler.add_job(
            jobs.ingest_orderbooks,
            trigger="cron",
            day_of_week="mon-fri",
            hour="7-23",
            minute=f"*/{settings.orderbook_interval_minutes}",
            second=10,
            id="order_book_ingestion",
            next_run_time=now + timedelta(seconds=5),
            **common,
        )
    scheduler.add_job(
        jobs.track_lifecycle,
        trigger="cron",
        day_of_week="mon-fri",
        hour="10-19",
        minute=f"*/{settings.lifecycle_interval_minutes}",
        second=20,
        id="lifecycle_tracking",
        next_run_time=now + timedelta(seconds=20),
        **common,
    )
    scheduler.add_job(
        jobs.scan_market,
        trigger="cron",
        day_of_week="mon-fri",
        hour="10-18",
        minute=f"*/{settings.scanning_interval_minutes}",
        second=45,
        id="idea_scanning",
        next_run_time=now + timedelta(seconds=45),
        **common,
    )
    if settings.intraday_v24_enabled and jobs.v24_coordinator is not None:
        scheduler.add_job(
            jobs.intraday_v24_cycle,
            trigger="cron",
            day_of_week="mon-fri",
            hour="10-18",
            minute=f"*/{settings.scanning_interval_minutes}",
            second=35,
            id="intraday_v24_cycle",
            next_run_time=now + timedelta(seconds=35),
            **common,
        )
    scheduler.add_job(
        jobs.dispatch_reports,
        trigger="interval",
        minutes=settings.reporting_interval_minutes,
        id="telegram_reporting",
        next_run_time=now + timedelta(seconds=10),
        **common,
    )
    scheduler.add_job(
        jobs.daily_summary,
        trigger="cron",
        day_of_week="mon-fri",
        hour=settings.daily_summary_hour,
        minute=settings.daily_summary_minute,
        id="daily_summary",
        **common,
    )
    if settings.intraday_v24_enabled:
        scheduler.add_job(
            jobs.v24_market_summary,
            trigger="cron",
            day_of_week="mon-fri",
            hour=settings.intraday_v24_market_summary_hour,
            minute=0,
            id="v24_market_summary",
            **common,
        )
    return scheduler
