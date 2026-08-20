from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.db import create_engine_and_session, init_db
from app.domain import IdeaHorizon, InstrumentData
from app.repositories import upsert_instruments
from app.scanner import MarketScanner


class StubIngestion:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def sync_universe(self) -> list[str]:
        self.calls.append("universe")
        return ["SBER"]

    async def sync_all(self) -> dict[str, int]:
        self.calls.append("ingestion")
        return {"candles": 10, "orderbook_levels": 0, "errors": 0}


class StubTracker:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def track_all(self) -> dict[str, int]:
        self.calls.append("tracking")
        return {"evaluated": 3, "transitions": 1, "expired": 0}


class StubIdeas:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def generate(self, ticker: str, horizon: IdeaHorizon):
        self.calls.append(f"idea:{ticker}:{horizon.value}")
        if horizon == IdeaHorizon.INTRADAY_1D:
            return SimpleNamespace(created=True, materially_changed=True)
        if horizon == IdeaHorizon.SWING_5D:
            return SimpleNamespace(created=False, materially_changed=True)
        return None


@pytest.mark.asyncio
async def test_market_scanning_runs_independently_from_reporting() -> None:
    engine, factory = create_engine_and_session("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    async with factory() as session, session.begin():
        await upsert_instruments(session, [InstrumentData("SBER", "TQBR", "Сбербанк")])
    calls: list[str] = []
    scanner = MarketScanner(
        factory,
        StubIngestion(calls),
        StubIdeas(calls),
        StubTracker(calls),
    )

    result = await scanner.scan()

    assert calls[:3] == ["universe", "ingestion", "tracking"]
    assert result == {
        "candles": 10,
        "ingestion_errors": 0,
        "tracked_candles": 3,
        "transitions": 1,
        "ideas_created": 1,
        "ideas_updated": 1,
        "ideas_skipped": 1,
        "ideas_stale": 0,
        "idea_errors": 0,
        "paper_open": 0,
        "paper_closed": 0,
    }
    await engine.dispose()
