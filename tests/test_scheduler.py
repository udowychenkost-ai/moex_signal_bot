from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.config import Settings
from app.db import create_engine_and_session, init_db
from app.domain import GeneratedSignal, InstrumentData
from app.repositories import add_watchlist_item, ensure_user, upsert_instruments
from app.scheduler import ScheduledJobs


class StubSignals:
    def __init__(self, signal: GeneratedSignal) -> None:
        self.signal = signal
        self.calls: list[tuple[str, str]] = []

    async def generate(self, secid: str, timeframe: str) -> GeneratedSignal:
        self.calls.append((secid, timeframe))
        return self.signal


class StubBot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        self.messages.append((chat_id, text))


@pytest.mark.asyncio
async def test_scheduler_formats_each_alert_with_the_recipients_risk() -> None:
    engine, factory = create_engine_and_session("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    async with factory() as session, session.begin():
        await upsert_instruments(session, [InstrumentData("SBER", "TQBR", "Сбер")])
        await ensure_user(session, 1001, "one", "1h", 0.5)
        await ensure_user(session, 1002, "two", "1h", 2.0)
        await add_watchlist_item(session, 1001, "SBER")
        await add_watchlist_item(session, 1002, "SBER")

    generated = GeneratedSignal(
        secid="SBER",
        timeframe="1h",
        horizon="intraday",
        action="BUY",
        technical_score=50,
        total_score=50,
        confidence=72.5,
        entry_price=100,
        stop_loss=97,
        take_profit=106,
        risk_pct=1.0,
        reward_risk_ratio=2.0,
        rationale=["Тест"],
        candle_begin=datetime(2026, 8, 19, tzinfo=UTC),
    )
    signals = StubSignals(generated)
    bot = StubBot()
    jobs = ScheduledJobs(Settings(_env_file=None), factory, None, signals, bot)

    await jobs.alert_watchlists()

    assert signals.calls == [("SBER", "1h")]
    assert len(bot.messages) == 2
    messages = dict(bot.messages)
    assert "Риск на сделку: 0.50%" in messages[1001]
    assert "Риск на сделку: 2.00%" in messages[1002]
    await engine.dispose()
