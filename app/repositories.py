from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain import CandleData, InstrumentData, OrderBookLevelData
from app.models import Candle, Instrument, OrderBookLevel, SignalRecord, TelegramUser, WatchlistItem


def _upsert_statement(session: AsyncSession, model: type, values: list[dict]):
    dialect = session.bind.dialect.name if session.bind else ""
    if dialect == "postgresql":
        return postgresql_insert(model).values(values)
    if dialect == "sqlite":
        return sqlite_insert(model).values(values)
    raise RuntimeError(f"Unsupported database dialect for upsert: {dialect}")


async def upsert_instruments(session: AsyncSession, items: list[InstrumentData]) -> int:
    if not items:
        return 0
    now = datetime.now(UTC)
    values = [
        {
            "secid": item.secid,
            "board_id": item.board_id,
            "instrument_type": "share",
            "short_name": item.short_name,
            "full_name": item.full_name,
            "isin": item.isin,
            "lot_size": item.lot_size,
            "last_price": item.last_price,
            "market_cap": item.market_cap,
            "daily_turnover": item.daily_turnover,
            "free_float": item.free_float,
            "echelon": item.echelon,
            "is_active": True,
            "updated_at": now,
        }
        for item in items
    ]
    statement = _upsert_statement(session, Instrument, values)
    excluded = statement.excluded
    statement = statement.on_conflict_do_update(
        index_elements=["secid"],
        set_={
            "board_id": excluded.board_id,
            "short_name": excluded.short_name,
            "full_name": excluded.full_name,
            "isin": excluded.isin,
            "lot_size": excluded.lot_size,
            "last_price": excluded.last_price,
            "market_cap": excluded.market_cap,
            "daily_turnover": excluded.daily_turnover,
            "free_float": excluded.free_float,
            "echelon": excluded.echelon,
            "is_active": excluded.is_active,
            "updated_at": excluded.updated_at,
        },
    )
    await session.execute(statement)
    return len(values)


async def list_active_instruments(session: AsyncSession) -> list[Instrument]:
    result = await session.scalars(
        select(Instrument).where(Instrument.is_active.is_(True)).order_by(Instrument.secid)
    )
    return list(result)


async def deactivate_instruments_except(session: AsyncSession, secids: list[str]) -> int:
    if not secids:
        raise ValueError("Refusing to deactivate the complete universe")
    result = await session.execute(
        update(Instrument)
        .where(Instrument.secid.not_in([secid.upper() for secid in secids]))
        .values(is_active=False)
    )
    return int(result.rowcount or 0)


async def get_instrument(session: AsyncSession, secid: str) -> Instrument | None:
    return await session.get(Instrument, secid.upper())


async def get_active_instrument(session: AsyncSession, secid: str) -> Instrument | None:
    return await session.scalar(
        select(Instrument).where(Instrument.secid == secid.upper(), Instrument.is_active.is_(True))
    )


async def latest_candle_begin(session: AsyncSession, secid: str, timeframe: str) -> datetime | None:
    return await session.scalar(
        select(func.max(Candle.begin)).where(
            Candle.secid == secid.upper(), Candle.timeframe == timeframe
        )
    )


async def upsert_candles(session: AsyncSession, candles: list[CandleData]) -> int:
    if not candles:
        return 0
    values = [
        {
            "secid": item.secid,
            "board_id": item.board_id,
            "timeframe": item.timeframe,
            "begin": item.begin,
            "end": item.end,
            "open": item.open,
            "high": item.high,
            "low": item.low,
            "close": item.close,
            "volume": item.volume,
            "value": item.value,
        }
        for item in candles
    ]
    statement = _upsert_statement(session, Candle, values)
    excluded = statement.excluded
    update_values = {
        "end": excluded.end,
        "open": excluded.open,
        "high": excluded.high,
        "low": excluded.low,
        "close": excluded.close,
        "volume": excluded.volume,
        "value": excluded.value,
    }
    if session.bind.dialect.name == "postgresql":
        statement = statement.on_conflict_do_update(constraint="uq_candle_key", set_=update_values)
    else:
        statement = statement.on_conflict_do_update(
            index_elements=["secid", "board_id", "timeframe", "begin"],
            set_=update_values,
        )
    await session.execute(statement)
    return len(values)


async def get_candles(
    session: AsyncSession,
    secid: str,
    timeframe: str,
    *,
    limit: int = 500,
) -> list[Candle]:
    result = await session.scalars(
        select(Candle)
        .where(Candle.secid == secid.upper(), Candle.timeframe == timeframe)
        .order_by(Candle.begin.desc())
        .limit(limit)
    )
    return list(reversed(list(result)))


async def get_candles_after(
    session: AsyncSession,
    secid: str,
    timeframe: str,
    after: datetime,
    *,
    limit: int = 2_000,
) -> list[Candle]:
    result = await session.scalars(
        select(Candle)
        .where(
            Candle.secid == secid.upper(),
            Candle.timeframe == timeframe,
            Candle.begin > after,
        )
        .order_by(Candle.begin.asc())
        .limit(limit)
    )
    return list(result)


async def save_orderbook_snapshot(
    session: AsyncSession, levels: list[OrderBookLevelData], retention_hours: int = 24
) -> int:
    if not levels:
        return 0
    await session.execute(
        delete(OrderBookLevel).where(
            OrderBookLevel.snapshot_at < datetime.now(UTC) - timedelta(hours=retention_hours)
        )
    )
    session.add_all(
        [
            OrderBookLevel(
                secid=item.secid,
                board_id=item.board_id,
                snapshot_at=item.snapshot_at,
                side=item.side,
                level=item.level,
                price=item.price,
                quantity=item.quantity,
            )
            for item in levels
        ]
    )
    return len(levels)


async def ensure_user(
    session: AsyncSession,
    telegram_id: int,
    username: str | None,
    default_timeframe: str,
    default_risk_pct: float,
    default_report_frequency: str = "hourly",
    default_idea_horizon: str = "all",
    default_minimum_confidence: float = 70.0,
) -> TelegramUser:
    user = await session.get(TelegramUser, telegram_id)
    if user is None:
        user = TelegramUser(
            telegram_id=telegram_id,
            username=username,
            default_timeframe=default_timeframe,
            risk_per_trade_pct=default_risk_pct,
            report_frequency=default_report_frequency,
            idea_horizon=default_idea_horizon,
            minimum_confidence=default_minimum_confidence,
        )
        session.add(user)
    else:
        user.username = username
        user.is_active = True
    await session.flush()
    return user


async def add_watchlist_item(session: AsyncSession, telegram_id: int, secid: str) -> bool:
    exists = await session.scalar(
        select(WatchlistItem.id).where(
            WatchlistItem.telegram_id == telegram_id, WatchlistItem.secid == secid.upper()
        )
    )
    if exists:
        return False
    session.add(WatchlistItem(telegram_id=telegram_id, secid=secid.upper()))
    return True


async def remove_watchlist_item(session: AsyncSession, telegram_id: int, secid: str) -> bool:
    result = await session.execute(
        delete(WatchlistItem).where(
            WatchlistItem.telegram_id == telegram_id, WatchlistItem.secid == secid.upper()
        )
    )
    return bool(result.rowcount)


async def get_watchlist(session: AsyncSession, telegram_id: int) -> list[str]:
    result = await session.scalars(
        select(WatchlistItem.secid)
        .where(WatchlistItem.telegram_id == telegram_id)
        .order_by(WatchlistItem.secid)
    )
    return list(result)


async def list_subscriptions(session: AsyncSession) -> list[tuple[int, str, str, float]]:
    rows = await session.execute(
        select(
            WatchlistItem.telegram_id,
            WatchlistItem.secid,
            TelegramUser.default_timeframe,
            TelegramUser.risk_per_trade_pct,
        )
        .join(TelegramUser, TelegramUser.telegram_id == WatchlistItem.telegram_id)
        .join(Instrument, Instrument.secid == WatchlistItem.secid)
        .where(
            TelegramUser.is_active.is_(True),
            Instrument.is_active.is_(True),
        )
    )
    return [(int(row[0]), str(row[1]), str(row[2]), float(row[3])) for row in rows]


async def update_user_settings(
    session: AsyncSession,
    telegram_id: int,
    *,
    timeframe: str | None = None,
    risk_pct: float | None = None,
    report_frequency: str | None = None,
    idea_horizon: str | None = None,
    minimum_confidence: float | None = None,
) -> None:
    values: dict[str, object] = {}
    if timeframe is not None:
        values["default_timeframe"] = timeframe
    if risk_pct is not None:
        values["risk_per_trade_pct"] = risk_pct
    if report_frequency is not None:
        values["report_frequency"] = report_frequency
    if idea_horizon is not None:
        values["idea_horizon"] = idea_horizon
    if minimum_confidence is not None:
        values["minimum_confidence"] = minimum_confidence
    if values:
        await session.execute(
            update(TelegramUser).where(TelegramUser.telegram_id == telegram_id).values(**values)
        )


async def list_report_users(session: AsyncSession) -> list[TelegramUser]:
    rows = await session.scalars(
        select(TelegramUser)
        .where(
            TelegramUser.is_active.is_(True),
            TelegramUser.report_frequency != "off",
        )
        .order_by(TelegramUser.telegram_id)
    )
    return list(rows)


async def mark_report_sent(
    session: AsyncSession,
    telegram_id: int,
    sent_at: datetime,
) -> None:
    await session.execute(
        update(TelegramUser)
        .where(TelegramUser.telegram_id == telegram_id)
        .values(last_report_at=sent_at)
    )


async def latest_signal(session: AsyncSession, secid: str, timeframe: str) -> SignalRecord | None:
    return await session.scalar(
        select(SignalRecord)
        .where(SignalRecord.secid == secid.upper(), SignalRecord.timeframe == timeframe)
        .order_by(SignalRecord.created_at.desc())
        .limit(1)
    )
