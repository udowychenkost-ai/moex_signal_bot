from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app import __main__ as app_main
from app.config import Settings
from app.db import create_engine_and_session, init_db
from app.domain import InstrumentData, OrderBookLevelData
from app.forward import format_application_status
from app.liquidity import BookLevelInput, LiquidityService, calculate_book_depths
from app.models import JobRunState, OrderBookLevel
from app.observation import DataFreshnessGuard
from app.operations import OperationalService
from app.orderbook_ingestion import OrderBookIngestionService
from app.repositories import save_orderbook_snapshot, upsert_instruments


def make_settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, enable_orderbook=True, **overrides)


def levels(
    secid: str,
    *,
    at: datetime | None = None,
    bid: float = 99.99,
    ask: float = 100.01,
    quantity: float | None = 100,
) -> list[OrderBookLevelData]:
    snapshot_at = at or datetime.now(UTC)
    return [
        OrderBookLevelData(secid, "TQBR", snapshot_at, "B", 1, bid, quantity),
        OrderBookLevelData(secid, "TQBR", snapshot_at, "S", 1, ask, quantity),
    ]


class BatchMoex:
    def __init__(
        self,
        result: dict[str, list[OrderBookLevelData]] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.result = result or {}
        self.error = error
        self.calls = 0
        self.requested: list[str] = []

    async def fetch_top_of_book_batch(
        self, secids: list[str], *, board_id: str
    ) -> dict[str, list[OrderBookLevelData]]:
        self.calls += 1
        self.requested = secids
        if self.error:
            raise self.error
        return self.result


async def database(*tickers: str):
    engine, factory = create_engine_and_session("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    async with factory() as session, session.begin():
        await upsert_instruments(
            session,
            [
                InstrumentData(
                    ticker,
                    "TQBR",
                    ticker,
                    lot_size=10,
                    last_price=100,
                    daily_turnover=1_000_000_000,
                )
                for ticker in tickers
            ],
        )
    return engine, factory


@pytest.mark.asyncio
async def test_disabled_orderbook_does_not_call_endpoint() -> None:
    engine, factory = await database("SBER")
    moex = BatchMoex({"SBER": levels("SBER")})
    service = OrderBookIngestionService(
        Settings(_env_file=None, enable_orderbook=False), factory, moex
    )

    result = await service.sync_all()

    assert result["status"] == "DISABLED"
    assert moex.calls == 0
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(OrderBookLevel)) == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_enabled_batch_saves_bids_asks_same_snapshot_and_lot_quantities() -> None:
    engine, factory = await database("SBER")
    snapshot_at = datetime.now(UTC)
    moex = BatchMoex({"SBER": levels("SBER", at=snapshot_at, quantity=25)})

    result = await OrderBookIngestionService(make_settings(), factory, moex).sync_all()

    assert result["status"] == "OK"
    assert result["levels_saved"] == 2
    assert result["quantities_saved"] == 2
    assert moex.requested == ["SBER"]
    async with factory() as session:
        rows = list(await session.scalars(select(OrderBookLevel).order_by(OrderBookLevel.side)))
    assert {row.side for row in rows} == {"B", "S"}
    assert {row.snapshot_at for row in rows} == {snapshot_at.replace(tzinfo=None)}
    assert {row.quantity for row in rows} == {25}
    calculated = calculate_book_depths(
        tuple(BookLevelInput(row.side, row.price, row.quantity) for row in rows),
        direction="BUY",
        lot_size=10,
        bands=(0.005,),
    )[0]
    assert calculated.bid_depth == pytest.approx(99.99 * 25 * 10)
    assert calculated.ask_depth == pytest.approx(100.01 * 25 * 10)
    await engine.dispose()


@pytest.mark.asyncio
async def test_public_level1_saves_real_spread_without_fake_depth() -> None:
    engine, factory = await database("SBER")
    moex = BatchMoex({"SBER": levels("SBER", quantity=None)})

    await OrderBookIngestionService(make_settings(), factory, moex).sync_all()
    assessment = await LiquidityService(make_settings(), factory).assess(
        "SBER",
        direction="BUY",
        now=datetime.now(UTC),
        market_open=True,
    )

    assert assessment.book_fresh
    assert assessment.spread_pct is not None
    assert assessment.relevant_depth is None
    assert assessment.depth_cap is None
    async with factory() as session:
        rows = list(await session.scalars(select(OrderBookLevel)))
    assert rows and all(row.quantity is None for row in rows)
    await engine.dispose()


@pytest.mark.asyncio
async def test_missing_or_malformed_ticker_does_not_break_valid_ticker() -> None:
    engine, factory = await database("SBER", "GAZP")
    malformed = [OrderBookLevelData("GAZP", "TQBR", datetime.now(UTC), "B", 1, -1, 10)]
    moex = BatchMoex({"SBER": levels("SBER"), "GAZP": malformed})

    result = await OrderBookIngestionService(make_settings(), factory, moex).sync_all()

    assert result["status"] == "PARTIAL"
    assert result["success"] == 1
    assert result["failed"] == 1
    async with factory() as session:
        saved = list(await session.scalars(select(OrderBookLevel)))
    assert {row.secid for row in saved} == {"SBER"}
    await engine.dispose()


@pytest.mark.asyncio
async def test_timeout_and_empty_response_preserve_previous_snapshot() -> None:
    engine, factory = await database("SBER")
    old_at = datetime.now(UTC) - timedelta(minutes=1)
    async with factory() as session, session.begin():
        await save_orderbook_snapshot(session, levels("SBER", at=old_at, bid=90, ask=91))

    timed_out = await OrderBookIngestionService(
        make_settings(), factory, BatchMoex(error=TimeoutError("temporary"))
    ).sync_all()
    empty = await OrderBookIngestionService(make_settings(), factory, BatchMoex({})).sync_all()

    assert timed_out["status"] == "ERROR"
    assert empty["status"] == "ERROR"
    async with factory() as session:
        rows = list(await session.scalars(select(OrderBookLevel)))
    assert len(rows) == 2
    assert {row.price for row in rows} == {90, 91}
    await engine.dispose()


@pytest.mark.asyncio
async def test_successful_snapshot_replaces_old_snapshot() -> None:
    engine, factory = await database("SBER")
    old_at = datetime.now(UTC) - timedelta(minutes=1)
    new_at = datetime.now(UTC)
    async with factory() as session, session.begin():
        await save_orderbook_snapshot(session, levels("SBER", at=old_at, bid=90, ask=91))

    await OrderBookIngestionService(
        make_settings(),
        factory,
        BatchMoex({"SBER": levels("SBER", at=new_at, bid=100, ask=101)}),
    ).sync_all()

    async with factory() as session:
        rows = list(await session.scalars(select(OrderBookLevel)))
    assert len(rows) == 2
    assert {row.price for row in rows} == {100, 101}
    assert {row.snapshot_at for row in rows} == {new_at.replace(tzinfo=None)}
    await engine.dispose()


@pytest.mark.asyncio
async def test_repository_rejects_mixed_or_empty_snapshot_before_delete() -> None:
    engine, factory = await database("SBER", "GAZP")
    old = levels("SBER", bid=90, ask=91)
    async with factory() as session, session.begin():
        await save_orderbook_snapshot(session, old)
    mixed = [levels("SBER")[0], levels("GAZP")[1]]

    async with factory() as session:
        with pytest.raises(ValueError, match="one instrument"):
            await save_orderbook_snapshot(session, mixed)
        assert await save_orderbook_snapshot(session, []) == 0
        await session.rollback()
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(OrderBookLevel)) == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_summary_logging_and_status_health(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, factory = await database("SBER", "GAZP")
    logged: list[str] = []

    def capture(message: str, *args: object) -> None:
        logged.append(message % args)

    monkeypatch.setattr("app.orderbook_ingestion.logger.info", capture)
    now = datetime.now(UTC)
    result = await OrderBookIngestionService(
        make_settings(), factory, BatchMoex({"SBER": levels("SBER", at=now)})
    ).sync_all()
    async with factory() as session, session.begin():
        session.add(
            JobRunState(
                job_name="order_book_ingestion",
                started_at=now,
                finished_at=now,
                success=False,
                details='{"status":"PARTIAL"}',
            )
        )

    status = await OperationalService(
        make_settings(), factory, DataFreshnessGuard(make_settings(), factory)
    ).status(now=now)
    rendered = format_application_status(status, timezone="Europe/Moscow")

    assert result["status"] == "PARTIAL"
    assert any(
        "orderbook ingestion completed checked=2 success=1 failed=1" in item for item in logged
    )
    assert status.orderbook.fresh_instruments == 1
    assert status.orderbook.monitored_instruments == 2
    assert status.orderbook.stale_instruments == 1
    assert status.orderbook.last_job == "PARTIAL"
    assert "Order Book: <b>ENABLED</b>" in rendered
    assert "Order Book last job: <b>PARTIAL</b>" in rendered
    await engine.dispose()


def test_cli_ingest_orderbook_runs_one_pass_and_returns_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def run_once() -> int:
        nonlocal calls
        calls += 1
        return 0

    monkeypatch.setattr(app_main, "ingest_orderbook_once", run_once)
    monkeypatch.setattr(sys, "argv", ["python", "ingest-orderbook"])

    with pytest.raises(SystemExit) as exit_info:
        app_main.main()

    assert exit_info.value.code == 0
    assert calls == 1
