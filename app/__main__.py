from __future__ import annotations

import argparse
import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.bot import BotServices, create_router
from app.config import get_settings
from app.db import create_engine_and_session, init_db
from app.idea_tracker import IdeaTracker
from app.ideas import TradingIdeaGenerator
from app.ingestion import IngestionService
from app.logging_config import configure_logging
from app.moex import MoexClient
from app.reporting import ReportingService
from app.scanner import MarketScanner
from app.scheduler import ScheduledJobs, build_scheduler
from app.signals import SignalService

logger = logging.getLogger(__name__)


async def ingest_once() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    engine, session_factory = create_engine_and_session(settings.database_url)
    await init_db(engine)
    try:
        async with MoexClient(
            settings.moex_base_url,
            timeout_seconds=settings.moex_request_timeout_seconds,
            max_retries=settings.moex_max_retries,
            api_token=settings.moex_api_token,
        ) as moex:
            ingestion = IngestionService(settings, session_factory, moex)
            await ingestion.sync_universe()
            result = await ingestion.sync_all()
            logger.info("One-off ingestion result: %s", result)
    finally:
        await engine.dispose()


async def run_bot() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required in run mode")

    engine, session_factory = create_engine_and_session(settings.database_url)
    await init_db(engine)
    bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    scheduler = None
    try:
        async with MoexClient(
            settings.moex_base_url,
            timeout_seconds=settings.moex_request_timeout_seconds,
            max_retries=settings.moex_max_retries,
            api_token=settings.moex_api_token,
        ) as moex:
            ingestion = IngestionService(settings, session_factory, moex)
            signals = SignalService(settings, session_factory)
            ideas = TradingIdeaGenerator(settings, session_factory, signals)
            tracker = IdeaTracker(session_factory)
            reporting = ReportingService(
                session_factory,
                timezone=settings.scheduler_timezone,
            )
            scanner = MarketScanner(session_factory, ingestion, ideas, tracker)
            services = BotServices(settings, session_factory, ingestion, signals, reporting)
            jobs = ScheduledJobs(settings, scanner, reporting, bot)
            scheduler = build_scheduler(settings, jobs)
            scheduler.start()

            await ingestion.sync_universe()
            dispatcher = Dispatcher()
            dispatcher.include_router(create_router(services))
            logger.info("Bot polling started")
            await dispatcher.start_polling(bot)
    finally:
        if scheduler is not None and scheduler.running:
            scheduler.shutdown(wait=False)
        await bot.session.close()
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="MOEX signal bot")
    parser.add_argument("command", choices=("run", "ingest"), nargs="?", default="run")
    args = parser.parse_args()
    asyncio.run(run_bot() if args.command == "run" else ingest_once())


if __name__ == "__main__":
    main()
