from __future__ import annotations

import argparse
import asyncio
import json
import logging
from dataclasses import asdict

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.backtest import BacktestEngine
from app.bot import BotServices, create_router
from app.config import get_settings
from app.db import create_engine_and_session
from app.domain import IdeaHorizon
from app.forward import ForwardReportingService
from app.fundamentals import (
    FundamentalAnalysisService,
    FundamentalIngestionService,
    OfficialDisclosureJsonProvider,
)
from app.horizons import get_horizon_profile
from app.idea_tracker import IdeaTracker
from app.ideas import TradingIdeaGenerator
from app.ingestion import IngestionService
from app.logging_config import configure_logging
from app.market_context import MarketRegimeService
from app.migrations import migrate_database
from app.moex import MoexClient
from app.observation import DataFreshnessGuard
from app.operations import OperationalService
from app.paper import PaperTradingService
from app.reporting import ReportingService
from app.repositories import get_active_instrument, get_candles
from app.scanner import MarketScanner
from app.scheduler import ScheduledJobs, build_scheduler
from app.signals import SignalService

logger = logging.getLogger(__name__)


async def ingest_once() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    await migrate_database(settings.database_url)
    engine, session_factory = create_engine_and_session(settings.database_url)
    try:
        async with MoexClient(
            settings.moex_base_url,
            timeout_seconds=settings.moex_request_timeout_seconds,
            max_retries=settings.moex_max_retries,
            api_token=settings.moex_api_token,
        ) as moex:
            ingestion = IngestionService(settings, session_factory, moex)
            fundamental_ingestion = FundamentalIngestionService(
                session_factory,
                (
                    [OfficialDisclosureJsonProvider(settings.fundamental_json_path)]
                    if settings.fundamental_enabled
                    else []
                ),
            )
            await ingestion.sync_universe()
            result = await ingestion.sync_all()
            result["fundamental_reports"] = await fundamental_ingestion.sync()
            logger.info("One-off ingestion result: %s", result)
    finally:
        await engine.dispose()


async def run_bot() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required in run mode")

    await migrate_database(settings.database_url)
    engine, session_factory = create_engine_and_session(settings.database_url)
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
            market_context = MarketRegimeService(
                session_factory,
                benchmark=settings.market_benchmark,
            )
            signals = SignalService(settings, session_factory, market_context)
            tracker = IdeaTracker(session_factory)
            freshness = DataFreshnessGuard(settings, session_factory)
            ideas = TradingIdeaGenerator(
                settings,
                session_factory,
                signals,
                freshness=freshness,
                fundamentals=(
                    FundamentalAnalysisService(session_factory)
                    if settings.fundamental_enabled
                    else None
                ),
            )
            reporting = ReportingService(
                session_factory,
                timezone=settings.scheduler_timezone,
            )
            paper = PaperTradingService(settings, session_factory)
            fundamental_ingestion = FundamentalIngestionService(
                session_factory,
                (
                    [OfficialDisclosureJsonProvider(settings.fundamental_json_path)]
                    if settings.fundamental_enabled
                    else []
                ),
            )
            scanner = MarketScanner(
                session_factory,
                ingestion,
                ideas,
                tracker,
                paper,
                fundamentals=fundamental_ingestion,
            )
            operations = OperationalService(settings, session_factory, freshness)
            forward_reporting = ForwardReportingService(
                settings,
                session_factory,
                operations,
            )
            services = BotServices(
                settings,
                session_factory,
                ingestion,
                signals,
                reporting,
                paper,
                operations,
            )
            recovery_tracking = await tracker.track_all()
            recovery_paper = await paper.sync_all()
            logger.info(
                "Startup recovery complete: lifecycle=%s paper=%s",
                recovery_tracking,
                recovery_paper,
            )
            jobs = ScheduledJobs(settings, scanner, forward_reporting, bot, operations)
            scheduler = build_scheduler(settings, jobs)
            operations.attach_scheduler(scheduler)
            scheduler.start()

            dispatcher = Dispatcher()
            dispatcher.include_router(create_router(services))
            logger.info("Bot polling started")
            await dispatcher.start_polling(bot)
    finally:
        if scheduler is not None and scheduler.running:
            scheduler.shutdown(wait=False)
        await bot.session.close()
        await engine.dispose()


async def run_backtest(ticker: str, horizon_value: str) -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    horizon = IdeaHorizon(horizon_value)
    await migrate_database(settings.database_url)
    engine, session_factory = create_engine_and_session(settings.database_url)
    try:
        profile = get_horizon_profile(horizon)
        async with session_factory() as session:
            instrument = await get_active_instrument(session, ticker)
            if instrument is None:
                raise RuntimeError(f"Active instrument {ticker.upper()} was not found")
            candles_by_timeframe = {
                timeframe: await get_candles(
                    session,
                    ticker,
                    timeframe,
                    limit=100_000,
                )
                for timeframe in profile.timeframe_weights
            }
        result = BacktestEngine(settings).run(
            ticker=ticker.upper(),
            instrument_name=instrument.short_name,
            horizon=horizon,
            candles_by_timeframe=candles_by_timeframe,
            lot_size=instrument.lot_size or 1,
        )
        print(json.dumps(asdict(result.metrics), ensure_ascii=False, indent=2))
    finally:
        await engine.dispose()


async def migrate_once() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    await migrate_database(settings.database_url)
    logger.info("Database schema is at Alembic head")


async def healthcheck_once() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    engine, session_factory = create_engine_and_session(settings.database_url)
    try:
        freshness = DataFreshnessGuard(settings, session_factory)
        operations = OperationalService(settings, session_factory, freshness)
        if not await operations.database_ok():
            raise RuntimeError("Database healthcheck failed")
        logger.info("Healthcheck passed")
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="MOEX signal bot")
    parser.add_argument(
        "command",
        choices=("run", "ingest", "backtest", "migrate", "healthcheck"),
        nargs="?",
        default="run",
    )
    parser.add_argument("ticker", nargs="?")
    parser.add_argument(
        "horizon",
        nargs="?",
        choices=tuple(item.value for item in IdeaHorizon),
        default=IdeaHorizon.SWING_5D.value,
    )
    args = parser.parse_args()
    if args.command == "run":
        asyncio.run(run_bot())
    elif args.command == "ingest":
        asyncio.run(ingest_once())
    elif args.command == "backtest":
        if not args.ticker:
            parser.error("backtest requires TICKER")
        asyncio.run(run_backtest(args.ticker, args.horizon))
    elif args.command == "migrate":
        asyncio.run(migrate_once())
    else:
        asyncio.run(healthcheck_once())


if __name__ == "__main__":
    main()
