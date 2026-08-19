from __future__ import annotations

import logging
from datetime import UTC, datetime

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import Settings
from app.reporting import ReportingService
from app.scanner import MarketScanner

logger = logging.getLogger(__name__)


class ScheduledJobs:
    def __init__(
        self,
        settings: Settings,
        scanner: MarketScanner,
        reporting: ReportingService,
        bot: Bot,
    ) -> None:
        self.settings = settings
        self.scanner = scanner
        self.reporting = reporting
        self.bot = bot

    async def scan_market(self) -> None:
        try:
            result = await self.scanner.scan()
            logger.info("Market scan complete: %s", result)
        except Exception:
            logger.exception("Scheduled market scan failed")

    async def dispatch_reports(self) -> None:
        try:
            result = await self.reporting.dispatch_due(self.bot)
            logger.info("Idea reporting complete: %s", result)
        except Exception:
            logger.exception("Scheduled idea reporting failed")


def build_scheduler(settings: Settings, jobs: ScheduledJobs) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=settings.scheduler_timezone)
    scheduler.add_job(
        jobs.scan_market,
        trigger="cron",
        day_of_week="mon-fri",
        hour="10-18",
        minute=f"*/{settings.ingestion_interval_minutes}",
        id="market_scan",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=120,
        next_run_time=datetime.now(UTC),
    )
    scheduler.add_job(
        jobs.dispatch_reports,
        trigger="cron",
        day_of_week="mon-fri",
        hour="10-19",
        minute="*/5",
        id="idea_reporting",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=120,
    )
    return scheduler
