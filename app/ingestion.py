from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.domain import InstrumentData, MoexApiError, UnknownTickerError
from app.moex import MoexClient
from app.repositories import (
    deactivate_instruments_except,
    get_active_instrument,
    latest_candle_begin,
    list_active_instruments,
    save_orderbook_snapshot,
    upsert_candles,
    upsert_instruments,
)

logger = logging.getLogger(__name__)

LOOKBACK = {
    "5m": timedelta(days=3),
    "15m": timedelta(days=7),
    "1h": timedelta(days=120),
    "4h": timedelta(days=500),
    "1d": timedelta(days=500),
    "1w": timedelta(days=1800),
}
OVERLAP = {
    "5m": timedelta(days=1),
    "15m": timedelta(days=1),
    "1h": timedelta(days=3),
    "4h": timedelta(days=14),
    "1d": timedelta(days=7),
    "1w": timedelta(days=21),
}


def classify_echelon(item: InstrumentData, settings: Settings) -> int:
    if item.secid in settings.blue_chip_list:
        return 1
    if (
        item.market_cap is not None
        and item.daily_turnover is not None
        and item.free_float is not None
        and item.market_cap >= settings.echelon1_min_market_cap
        and item.daily_turnover >= settings.echelon1_min_daily_turnover
        and item.free_float >= settings.echelon1_min_free_float
    ):
        return 1
    if (item.daily_turnover or 0) >= settings.echelon2_min_daily_turnover:
        return 2
    return 0


class IngestionService:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        moex: MoexClient,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.moex = moex
        self._semaphore = asyncio.Semaphore(settings.moex_request_concurrency)
        self._write_lock = asyncio.Lock()

    async def sync_universe(self) -> list[str]:
        items = await self.moex.fetch_instruments("TQBR")
        if not items:
            raise MoexApiError("MOEX returned an empty TQBR universe")
        blue_order = {secid: rank for rank, secid in enumerate(self.settings.blue_chip_list)}
        for item in items:
            item.echelon = classify_echelon(item, self.settings)
        eligible = [item for item in items if item.echelon > 0]
        eligible.sort(
            key=lambda item: (
                0 if item.secid in blue_order else 1,
                blue_order.get(item.secid, 10_000),
                -(item.daily_turnover or 0),
            )
        )
        selected = eligible[: self.settings.universe_size]
        if not selected:
            raise MoexApiError("No instruments matched the configured universe filters")
        async with self._write_lock:
            async with self.session_factory() as session, session.begin():
                await upsert_instruments(session, selected)
                await deactivate_instruments_except(session, [item.secid for item in selected])
        logger.info("Universe synced: %s instruments", len(selected))
        return [item.secid for item in selected]

    async def sync_all(self, *, include_orderbook: bool | None = None) -> dict[str, int]:
        if include_orderbook is None:
            include_orderbook = self.settings.enable_orderbook
        async with self.session_factory() as session:
            instruments = await list_active_instruments(session)
        if not instruments:
            await self.sync_universe()
            async with self.session_factory() as session:
                instruments = await list_active_instruments(session)

        async def sync_one(secid: str, board_id: str) -> tuple[int, int, int]:
            candle_count = 0
            orderbook_count = 0
            error_count = 0
            try:
                async with self._semaphore:
                    for timeframe in self.settings.timeframe_list:
                        candle_count += await self.sync_candles(secid, timeframe, board_id=board_id)
                    if include_orderbook:
                        try:
                            levels = await self.moex.fetch_orderbook(secid, board_id=board_id)
                            async with self._write_lock:
                                async with self.session_factory() as session, session.begin():
                                    orderbook_count += await save_orderbook_snapshot(
                                        session, levels
                                    )
                        except Exception:
                            error_count += 1
                            logger.exception("Order book unavailable for %s", secid)
            except Exception:
                error_count += 1
                logger.exception("Failed to ingest %s", secid)
            return candle_count, orderbook_count, error_count

        results = await asyncio.gather(
            *(sync_one(item.secid, item.board_id) for item in instruments)
        )
        counters = {
            "candles": sum(result[0] for result in results),
            "orderbook_levels": sum(result[1] for result in results),
            "errors": sum(result[2] for result in results),
        }
        logger.info("Market data sync complete: %s", counters)
        return counters

    async def sync_candles(self, secid: str, timeframe: str, *, board_id: str = "TQBR") -> int:
        now = datetime.now(UTC)
        async with self.session_factory() as session:
            latest = await latest_candle_begin(session, secid, timeframe)
        date_from = latest - OVERLAP[timeframe] if latest else now - LOOKBACK[timeframe]
        candles = await self.moex.fetch_candles(secid, timeframe, date_from, board_id=board_id)
        async with self._write_lock:
            async with self.session_factory() as session, session.begin():
                return await upsert_candles(session, candles)

    async def refresh_ticker(self, secid: str, timeframe: str) -> int:
        secid = secid.upper()
        async with self.session_factory() as session:
            instrument = await get_active_instrument(session, secid)
        if instrument is None:
            await self.sync_universe()
            async with self.session_factory() as session:
                instrument = await get_active_instrument(session, secid)
        if instrument is None:
            raise UnknownTickerError(f"Тикер {secid} не входит в текущую вселенную MVP")
        async with self._semaphore:
            return await self.sync_candles(secid, timeframe, board_id=instrument.board_id)
