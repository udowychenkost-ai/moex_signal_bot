from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import replace

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.domain import InsufficientDataError, MoexApiError
from app.ingestion import IngestionService
from app.models import TelegramUser
from app.repositories import latest_signal, list_subscriptions
from app.signals import SignalService, format_signal

logger = logging.getLogger(__name__)


class ScheduledJobs:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        ingestion: IngestionService,
        signals: SignalService,
        bot: Bot,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.ingestion = ingestion
        self.signals = signals
        self.bot = bot

    async def ingest_and_alert(self) -> None:
        try:
            await self.ingestion.sync_universe()
            await self.ingestion.sync_all()
            await self.alert_watchlists()
        except Exception:
            logger.exception("Scheduled ingestion failed")

    async def alert_watchlists(self) -> None:
        async with self.session_factory() as session:
            subscriptions = await list_subscriptions(session)
        grouped: dict[tuple[str, str], list[tuple[int, float]]] = defaultdict(list)
        for telegram_id, secid, timeframe, risk_pct in subscriptions:
            grouped[(secid, timeframe)].append((telegram_id, risk_pct))

        for (secid, timeframe), recipients in grouped.items():
            try:
                async with self.session_factory() as session:
                    previous = await latest_signal(session, secid, timeframe)
                generated = await self.signals.generate(secid, timeframe)
                if generated.action == "HOLD" or (
                    previous is not None
                    and previous.action == generated.action
                    and previous.candle_begin == generated.candle_begin
                ):
                    continue
                for chat_id, risk_pct in recipients:
                    try:
                        personalized = replace(generated, risk_pct=risk_pct)
                        await self.bot.send_message(chat_id, format_signal(personalized))
                    except TelegramForbiddenError:
                        async with self.session_factory() as session, session.begin():
                            await session.execute(
                                update(TelegramUser)
                                .where(TelegramUser.telegram_id == chat_id)
                                .values(is_active=False)
                            )
            except (InsufficientDataError, MoexApiError):
                logger.info("Signal skipped for %s %s: data unavailable", secid, timeframe)
            except Exception:
                logger.exception("Watchlist alert failed for %s %s", secid, timeframe)


def build_scheduler(settings: Settings, jobs: ScheduledJobs) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=settings.scheduler_timezone)
    scheduler.add_job(
        jobs.ingest_and_alert,
        trigger="cron",
        day_of_week="mon-fri",
        hour="10-18",
        minute=f"*/{settings.ingestion_interval_minutes}",
        id="market_ingestion",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=120,
    )
    return scheduler
