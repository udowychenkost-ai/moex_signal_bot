from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import asdict, dataclass
from time import perf_counter

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.domain import OrderBookLevelData
from app.models import Instrument
from app.moex import MoexClient
from app.repositories import list_active_instruments, save_orderbook_snapshot

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OrderBookIngestionSummary:
    enabled: bool
    status: str
    source: str
    checked: int
    success: int
    failed: int
    levels_saved: int
    quantities_saved: int
    duration_ms: int

    def as_dict(self) -> dict[str, object]:
        result = asdict(self)
        # ScheduledJobs has a shared error-counter convention.
        result["errors"] = self.failed or int(self.status == "ERROR")
        return result


class OrderBookIngestionService:
    """One coherent order-book path for scheduler, CLI, and one-off ingestion."""

    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        moex: MoexClient,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.moex = moex
        self._semaphore = asyncio.Semaphore(settings.orderbook_request_concurrency)
        self._write_lock = asyncio.Lock()

    async def sync_all(self) -> dict[str, object]:
        started = perf_counter()
        if not self.settings.enable_orderbook:
            summary = OrderBookIngestionSummary(
                enabled=False,
                status="DISABLED",
                source="NONE",
                checked=0,
                success=0,
                failed=0,
                levels_saved=0,
                quantities_saved=0,
                duration_ms=round((perf_counter() - started) * 1000),
            )
            logger.info("orderbook ingestion skipped enabled=false")
            return summary.as_dict()

        async with self.session_factory() as session:
            instruments = await list_active_instruments(session)
        checked = len(instruments)
        if not instruments:
            summary = OrderBookIngestionSummary(
                enabled=True,
                status="ERROR",
                source="NONE",
                checked=0,
                success=0,
                failed=0,
                levels_saved=0,
                quantities_saved=0,
                duration_ms=round((perf_counter() - started) * 1000),
            )
            logger.warning("orderbook ingestion skipped: active universe is empty")
            return summary.as_dict()

        levels_by_ticker: dict[str, list[OrderBookLevelData]] = {}
        source = "MOEX_ISS_MARKETDATA_LEVEL1"
        # Full depth is an ISS subscription product. Only probe it when an
        # authorization token is configured; public deployments go straight to
        # one filtered batch request and avoid a denied request per ticker.
        if self.settings.moex_api_token:
            levels_by_ticker = await self._fetch_subscribed_depth(instruments)
            if levels_by_ticker:
                source = "MOEX_ISS_ORDERBOOK_DEPTH"

        missing = [item for item in instruments if item.secid not in levels_by_ticker]
        if missing:
            fallback = await self._fetch_level1(missing)
            levels_by_ticker.update(fallback)
            if fallback:
                source = (
                    "MOEX_ISS_MIXED"
                    if levels_by_ticker.keys() - fallback.keys()
                    else "MOEX_ISS_MARKETDATA_LEVEL1"
                )

        success = 0
        levels_saved = 0
        quantities_saved = 0
        for instrument in instruments:
            levels = levels_by_ticker.get(instrument.secid, [])
            if not levels:
                logger.warning(
                    "orderbook ingestion unavailable secid=%s reason=no_valid_bid_ask",
                    instrument.secid,
                )
                continue
            try:
                async with self._write_lock:
                    async with self.session_factory() as session, session.begin():
                        saved = await save_orderbook_snapshot(session, levels)
                success += 1
                levels_saved += saved
                quantities_saved += sum(item.quantity is not None for item in levels)
            except (ValueError, RuntimeError) as error:
                logger.warning(
                    "orderbook snapshot rejected secid=%s error=%s",
                    instrument.secid,
                    error,
                )
            except Exception as error:  # database failures stay isolated per ticker
                logger.warning(
                    "orderbook snapshot save failed secid=%s error=%s: %s",
                    instrument.secid,
                    type(error).__name__,
                    error,
                )

        failed = checked - success
        status = "OK" if success == checked else ("PARTIAL" if success else "ERROR")
        summary = OrderBookIngestionSummary(
            enabled=True,
            status=status,
            source=source,
            checked=checked,
            success=success,
            failed=failed,
            levels_saved=levels_saved,
            quantities_saved=quantities_saved,
            duration_ms=round((perf_counter() - started) * 1000),
        )
        logger.info(
            "orderbook ingestion completed checked=%s success=%s failed=%s "
            "levels_saved=%s duration_ms=%s source=%s",
            summary.checked,
            summary.success,
            summary.failed,
            summary.levels_saved,
            summary.duration_ms,
            summary.source,
        )
        return summary.as_dict()

    async def _fetch_subscribed_depth(
        self, instruments: list[Instrument]
    ) -> dict[str, list[OrderBookLevelData]]:
        async def fetch_one(instrument: Instrument) -> tuple[str, list[OrderBookLevelData]]:
            secid = instrument.secid
            board_id = instrument.board_id
            try:
                async with self._semaphore:
                    levels = await self.moex.fetch_orderbook(secid, board_id=board_id)
                if {item.side for item in levels} != {"B", "S"}:
                    return secid, []
                return secid, levels
            except Exception as error:
                logger.warning(
                    "orderbook depth unavailable secid=%s error=%s: %s; using level1 fallback",
                    secid,
                    type(error).__name__,
                    error,
                )
                return secid, []

        first = await fetch_one(instruments[0])
        if not first[1]:
            return {}
        fetched = [first]
        fetched.extend(await asyncio.gather(*(fetch_one(item) for item in instruments[1:])))
        return {secid: levels for secid, levels in fetched if levels}

    async def _fetch_level1(
        self, instruments: list[Instrument]
    ) -> dict[str, list[OrderBookLevelData]]:
        grouped: dict[str, list[str]] = defaultdict(list)
        for instrument in instruments:
            grouped[instrument.board_id].append(instrument.secid)
        result: dict[str, list[OrderBookLevelData]] = {}
        for board_id, secids in grouped.items():
            try:
                async with self._semaphore:
                    batch = await self.moex.fetch_top_of_book_batch(secids, board_id=board_id)
                result.update(batch)
            except Exception as error:
                logger.warning(
                    "orderbook level1 batch unavailable board=%s checked=%s error=%s: %s",
                    board_id,
                    len(secids),
                    type(error).__name__,
                    error,
                )
        return result
