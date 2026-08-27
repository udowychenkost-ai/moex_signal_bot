from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.config import Settings
from app.db import create_engine_and_session, init_db
from app.domain import CandleData, InstrumentData, OrderBookLevelData
from app.idea_repository import create_or_update_idea
from app.ideas import idea_material_hash
from app.liquidity import (
    BookLevelInput,
    LiquidityInput,
    LiquidityRating,
    LiquidityService,
    calculate_book_depths,
    calculate_liquidity,
    round_liquidity_size_down,
)
from app.liquidity_ux import format_liquidity_compact, format_liquidity_details
from app.models import Instrument, TradingIdea, TradingIdeaEvent
from app.quality import QualityGate
from app.repositories import save_orderbook_snapshot, upsert_candles, upsert_instruments
from app.telegram_ui import idea_context_keyboard, liquidity_context_keyboard
from tests.test_v2_quality_ai import candidate

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)


def settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)


def liquid_book(*, quantity: float = 6_000, spread: float = 0.02) -> tuple[BookLevelInput, ...]:
    half = spread / 2
    return (
        BookLevelInput("B", 100 - half, quantity),
        BookLevelInput("B", 100 - half - 0.20, quantity / 2),
        BookLevelInput("S", 100 + half, quantity),
        BookLevelInput("S", 100 + half + 0.20, quantity / 2),
    )


def inputs(**overrides: object) -> LiquidityInput:
    values: dict[str, object] = {
        "ticker": "SBER",
        "instrument_name": "Сбербанк",
        "direction": "BUY",
        "price": 100.0,
        "turnover_today": 1_600_000_000.0,
        "last_daily_turnover": 2_000_000_000.0,
        "adv20": 2_000_000_000.0,
        "adv_days_target": 20,
        "adv_days_used": 20,
        "lot_size": 10,
        "volatility": "NORMAL",
        "book_levels": liquid_book(),
        "book_snapshot_at": NOW - timedelta(seconds=30),
        "market_open": True,
        "now": NOW,
    }
    values.update(overrides)
    return LiquidityInput(**values)  # type: ignore[arg-type]


def test_high_liquidity_uses_turnover_spread_depth_and_freshness() -> None:
    result = calculate_liquidity(inputs(), settings())

    assert result.liquidity_rating is LiquidityRating.HIGH
    assert result.calculation_basis == "TURNOVER_AND_BOOK"
    assert result.book_fresh
    assert result.turnover_cap == pytest.approx(5_000_000)
    assert result.comfortable_size_rounded == 750_000
    assert {
        "ticker",
        "turnover_today",
        "adv20",
        "relative_turnover",
        "spread_pct",
        "book_age_seconds",
        "entry_depth",
        "exit_depth",
        "relevant_depth",
        "turnover_cap",
        "depth_cap",
        "volatility_modifier",
        "spread_modifier",
        "comfortable_size",
        "liquidity_rating",
    } <= result.debug_values().keys()


def test_medium_liquidity() -> None:
    result = calculate_liquidity(
        inputs(
            adv20=300_000_000,
            turnover_today=120_000_000,
            book_levels=liquid_book(quantity=2_500, spread=0.20),
        ),
        settings(),
    )

    assert result.liquidity_rating is LiquidityRating.MEDIUM


def test_low_liquidity() -> None:
    result = calculate_liquidity(
        inputs(
            adv20=30_000_000,
            turnover_today=5_000_000,
            book_levels=liquid_book(quantity=50, spread=0.80),
        ),
        settings(),
    )

    assert result.liquidity_rating is LiquidityRating.LOW


def test_no_order_book_uses_turnover_only_without_crashing() -> None:
    result = calculate_liquidity(
        inputs(book_levels=(), book_snapshot_at=None),
        settings(),
    )

    assert not result.book_fresh
    assert result.depth_cap is None
    assert result.calculation_basis == "TURNOVER_ONLY"
    assert result.comfortable_size is not None


def test_stale_order_book_is_never_used_as_current_depth() -> None:
    result = calculate_liquidity(
        inputs(book_snapshot_at=NOW - timedelta(minutes=10)),
        settings(liquidity_orderbook_freshness_seconds=300),
    )

    assert not result.book_fresh
    assert result.depths == ()
    assert result.depth_cap is None
    assert result.calculation_basis == "TURNOVER_ONLY"
    assert "Стакан устарел" in format_liquidity_details(result)


def test_no_adv20_returns_no_pseudo_precise_size() -> None:
    result = calculate_liquidity(inputs(adv20=None, adv_days_used=0), settings())

    assert result.turnover_cap is None
    assert result.comfortable_size is None
    assert result.comfortable_size_rounded is None
    assert "Недостаточно данных" in format_liquidity_details(result)


def test_wide_spread_applies_conservative_modifier() -> None:
    result = calculate_liquidity(
        inputs(book_levels=liquid_book(quantity=12_000, spread=0.80)),
        settings(),
    )

    assert result.spread_pct is not None and result.spread_pct > 0.005
    assert result.spread_modifier == pytest.approx(0.35)


@pytest.mark.parametrize(
    ("volatility", "modifier"),
    [("HIGH", 0.70), ("EXTREME", 0.45)],
)
def test_existing_high_and_extreme_volatility_modifiers(
    volatility: str,
    modifier: float,
) -> None:
    result = calculate_liquidity(inputs(volatility=volatility), settings())

    assert result.volatility_modifier == pytest.approx(modifier)


def test_buy_depth_uses_asks_for_entry_and_bids_for_exit() -> None:
    levels = (
        BookLevelInput("B", 99.99, 100),
        BookLevelInput("S", 100.01, 40),
    )
    depth = calculate_book_depths(levels, direction="BUY", lot_size=1, bands=(0.005,))[0]

    assert depth.entry_depth == pytest.approx(100.01 * 40)
    assert depth.exit_depth == pytest.approx(99.99 * 100)


def test_sell_depth_uses_bids_for_entry_and_asks_for_exit() -> None:
    levels = (
        BookLevelInput("B", 99.99, 100),
        BookLevelInput("S", 100.01, 40),
    )
    depth = calculate_book_depths(levels, direction="SELL", lot_size=1, bands=(0.005,))[0]

    assert depth.entry_depth == pytest.approx(99.99 * 100)
    assert depth.exit_depth == pytest.approx(100.01 * 40)


def test_lot_size_and_rub_notional_are_applied_once() -> None:
    levels = (
        BookLevelInput("B", 100, 10),
        BookLevelInput("S", 101, 10),
    )
    depth = calculate_book_depths(levels, direction="BUY", lot_size=10, bands=(0.02,))[0]

    assert depth.bid_depth == pytest.approx(100 * 10 * 10)
    assert depth.ask_depth == pytest.approx(101 * 10 * 10)


def test_relevant_depth_is_conservative_minimum_of_entry_and_exit() -> None:
    levels = (
        BookLevelInput("B", 99.99, 1_000),
        BookLevelInput("S", 100.01, 200),
    )
    depth = calculate_book_depths(levels, direction="BUY", lot_size=1, bands=(0.005,))[0]

    assert depth.relevant_depth == min(depth.entry_depth, depth.exit_depth)


@pytest.mark.parametrize(
    ("raw", "rounded"),
    [(783_427, 750_000), (2_749_999, 2_500_000), (99_999, 75_000)],
)
def test_comfortable_size_rounds_down(raw: float, rounded: float) -> None:
    assert round_liquidity_size_down(raw) == rounded
    assert round_liquidity_size_down(raw) <= raw  # type: ignore[operator]


def test_closed_market_ignores_book_and_uses_historical_turnover_context() -> None:
    result = calculate_liquidity(
        inputs(
            market_open=False,
            turnover_today=None,
            last_daily_turnover=1_800_000_000,
        ),
        settings(),
    )
    text = format_liquidity_details(result)

    assert not result.book_fresh
    assert result.relative_turnover == pytest.approx(0.9)
    assert result.calculation_basis == "TURNOVER_ONLY"
    assert "Биржа сейчас закрыта" in text
    assert "Без учёта текущего стакана" in text


def test_telegram_compact_block_uses_required_risk_wording() -> None:
    text = format_liquidity_compact(calculate_liquidity(inputs(), settings()))

    assert "💧 <b>Ликвидность</b>" in text
    assert "Оборот сегодня" in text
    assert "Средний оборот" in text
    assert "Комфортный размер по ликвидности" in text
    assert "безопасный размер" not in text.lower()


def test_telegram_detailed_screen_and_navigation() -> None:
    assessment = calculate_liquidity(inputs(), settings())
    text = format_liquidity_details(assessment)
    keyboard = liquidity_context_keyboard(42)
    callbacks = [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data
    ]

    assert "Ликвидность — Сбербанк (SBER)" in text
    assert "±0.25%" in text and "±0.50%" in text and "±1.00%" in text
    assert "Это не рекомендация по размеру риска" in text
    assert callbacks == ["idea:42", "home"]


def test_missing_data_does_not_crash_and_renders_unknown() -> None:
    result = calculate_liquidity(
        inputs(
            price=None,
            turnover_today=None,
            last_daily_turnover=None,
            adv20=None,
            adv_days_used=0,
            book_levels=(),
            book_snapshot_at=None,
            volatility=None,
        ),
        settings(),
    )

    assert result.liquidity_rating is LiquidityRating.UNKNOWN
    assert "нет данных" in format_liquidity_compact(result)
    assert "Не определена" in format_liquidity_details(result)


def test_liquidity_does_not_change_quality_gate() -> None:
    quant = candidate()
    gate = QualityGate(settings())
    before_candidate = asdict(quant)
    before = gate.evaluate(quant)

    calculate_liquidity(inputs(), settings())
    after = gate.evaluate(quant)

    assert before == after
    assert asdict(quant) == before_candidate


@pytest.mark.asyncio
async def test_db_first_service_reuses_existing_data_without_lifecycle_writes() -> None:
    engine, factory = create_engine_and_session("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    quant = candidate()
    async with factory() as session, session.begin():
        await upsert_instruments(
            session,
            [
                InstrumentData(
                    "SBER",
                    "TQBR",
                    "Сбербанк",
                    lot_size=10,
                    last_price=100,
                    daily_turnover=1_600_000_000,
                )
            ],
        )
        instrument = await session.get(Instrument, "SBER")
        assert instrument is not None
        instrument.updated_at = NOW - timedelta(minutes=1)
        daily = []
        for offset in range(20, 0, -1):
            begin = (NOW - timedelta(days=offset)).replace(hour=0)
            daily.append(
                CandleData(
                    "SBER",
                    "TQBR",
                    "1d",
                    begin,
                    begin + timedelta(hours=23, minutes=59),
                    99,
                    101,
                    98,
                    100,
                    20_000_000,
                    2_000_000_000,
                )
            )
        await upsert_candles(session, daily)
        await save_orderbook_snapshot(
            session,
            [
                OrderBookLevelData(
                    "SBER", "TQBR", NOW - timedelta(seconds=30), "B", 1, 99.99, 12_000
                ),
                OrderBookLevelData(
                    "SBER", "TQBR", NOW - timedelta(seconds=30), "S", 1, 100.01, 12_000
                ),
            ],
        )
        created = await create_or_update_idea(
            session,
            quant,
            material_hash=idea_material_hash(quant),
            confidence_delta=7.5,
        )
        idea_id = created.idea.id

    async with factory() as session:
        idea = await session.get(TradingIdea, idea_id)
        before_events = await session.scalar(
            select(func.count()).select_from(TradingIdeaEvent).where(
                TradingIdeaEvent.idea_id == idea_id
            )
        )
        assert idea is not None
        before_status = idea.status

    assessment = await LiquidityService(settings(), factory).assess_idea(
        idea, now=NOW, market_open=True
    )

    async with factory() as session:
        unchanged = await session.get(TradingIdea, idea_id)
        after_events = await session.scalar(
            select(func.count()).select_from(TradingIdeaEvent).where(
                TradingIdeaEvent.idea_id == idea_id
            )
        )
    assert assessment.adv20 == pytest.approx(2_000_000_000)
    assert assessment.book_fresh
    assert assessment.relevant_depth is not None
    assert unchanged is not None and unchanged.status == before_status
    assert after_events == before_events
    await engine.dispose()


def test_idea_keyboard_exposes_liquidity_action() -> None:
    idea = TradingIdea(
        id=42,
        ticker="SBER",
        instrument_name="Сбербанк",
        direction="BUY",
        horizon="SWING_5D",
        status="ACTIVE",
    )
    labels = [
        button.text
        for row in idea_context_keyboard(idea, watched=False, followed=False).inline_keyboard
        for button in row
    ]

    assert "💧 Ликвидность" in labels
