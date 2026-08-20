from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain import IdeaHorizon, StaleMarketDataError
from app.fundamentals import FundamentalIngestionService
from app.idea_tracker import IdeaTracker
from app.ideas import TradingIdeaGenerator
from app.ingestion import IngestionService
from app.paper import PaperTradingService
from app.repositories import list_active_instruments

logger = logging.getLogger(__name__)


class MarketScanner:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        ingestion: IngestionService,
        ideas: TradingIdeaGenerator,
        tracker: IdeaTracker,
        paper: PaperTradingService | None = None,
        fundamentals: FundamentalIngestionService | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.ingestion = ingestion
        self.ideas = ideas
        self.tracker = tracker
        self.paper = paper
        self.fundamentals = fundamentals

    async def ingest(self) -> dict[str, int]:
        await self.ingestion.sync_universe()
        result = await self.ingestion.sync_all()
        result["fundamental_reports"] = (
            await self.fundamentals.sync() if self.fundamentals is not None else 0
        )
        return result

    async def track_lifecycle(self) -> dict[str, int]:
        return await self.tracker.track_all()

    async def scan_ideas(self) -> dict[str, int]:
        async with self.session_factory() as session:
            instruments = await list_active_instruments(session)

        created = 0
        updated = 0
        skipped = 0
        stale = 0
        errors = 0
        for instrument in instruments:
            for horizon in IdeaHorizon:
                try:
                    result = await self.ideas.generate(instrument.secid, horizon)
                    if result is None:
                        skipped += 1
                    elif result.created:
                        created += 1
                    elif result.materially_changed:
                        updated += 1
                    else:
                        skipped += 1
                except StaleMarketDataError as error:
                    stale += 1
                    logger.warning("Idea generation blocked by freshness guard: %s", error)
                except Exception:
                    errors += 1
                    logger.exception("Idea scan failed for %s %s", instrument.secid, horizon.value)
        return {
            "ideas_created": created,
            "ideas_updated": updated,
            "ideas_skipped": skipped,
            "ideas_stale": stale,
            "idea_errors": errors,
        }

    async def sync_paper(self) -> dict[str, int]:
        return await self.paper.sync_all() if self.paper is not None else {"open": 0, "closed": 0}

    async def scan(self) -> dict[str, int]:
        """Compatibility orchestration for one-off runs; scheduler uses independent jobs."""
        ingestion_result = await self.ingest()
        tracking_result = await self.track_lifecycle()
        idea_result = await self.scan_ideas()
        paper_result = await self.sync_paper()
        return {
            "candles": ingestion_result["candles"],
            "ingestion_errors": ingestion_result["errors"],
            "tracked_candles": tracking_result["evaluated"],
            "transitions": tracking_result["transitions"],
            **idea_result,
            "paper_open": paper_result["open"],
            "paper_closed": paper_result["closed"],
        }
