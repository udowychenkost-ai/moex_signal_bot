from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.config import Settings
from app.data_contexts import ContextFact, ContextRepositoryV24, UnavailableContextProvider
from app.data_integrity import DataIntegrityService, SourceConflict, SourceObservation
from app.data_sla import (
    DataSLAConfig,
    DataSLAObservation,
    DataSLAService,
    MarketDataPoint,
)
from app.db import create_engine_and_session, init_db
from app.microstructure import CorporateActionFlags, MicrostructureGuard, MicrostructureInput
from app.v24_domain import (
    ConfigurationStatus,
    ContextType,
    DataAvailability,
    DataConfidence,
    DataSLAResult,
    DecisionAction,
    GateResult,
    JournalDirection,
    MicrostructureStatus,
    SourceClass,
    TradingSessionState,
)

NOW = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)


def _point(
    name: str,
    value: float | None,
    *,
    source_class: SourceClass = SourceClass.OFFICIAL_REALTIME,
    age_seconds: int = 1,
) -> MarketDataPoint:
    timestamp = NOW - timedelta(seconds=age_seconds)
    return MarketDataPoint(
        name=name,
        value=value,
        source="test-source",
        source_class=source_class,
        source_timestamp=timestamp,
        fetched_at=timestamp + timedelta(milliseconds=100),
    )


def _configured_sla() -> DataSLAConfig:
    return DataSLAConfig(
        version="operator-policy-1",
        max_latency_enter_now=5,
        max_latency_position_management=10,
        max_latency_intraday_analysis=60,
        required_quote_source_class=SourceClass.OFFICIAL_REALTIME,
        required_volume_source_class=SourceClass.OFFICIAL_REALTIME,
        required_orderbook_source_class=SourceClass.OFFICIAL_REALTIME,
    )


def _sla_observation(*, age_seconds: int = 1) -> DataSLAObservation:
    return DataSLAObservation(
        current_price=_point("current_price", 268.5, age_seconds=age_seconds),
        volume=_point("volume", 100_000.0, age_seconds=age_seconds),
        orderbook=_point("orderbook", 1.0, age_seconds=age_seconds),
    )


def test_data_sla_is_not_configured_by_default_and_blocks_enter_now() -> None:
    settings = Settings(_env_file=None)
    service = DataSLAService(DataSLAConfig.from_settings(settings))

    assessment = service.evaluate(
        DecisionAction.ENTER_NOW,
        _sla_observation(),
        now=NOW,
    )

    assert assessment.status is ConfigurationStatus.NOT_CONFIGURED
    assert assessment.result is DataSLAResult.NOT_CONFIGURED
    assert assessment.allows_requested_action is False
    assert assessment.issues[0].code == "SLA_NOT_CONFIGURED"


def test_configured_data_sla_passes_only_fresh_matching_sources() -> None:
    service = DataSLAService(_configured_sla())

    passed = service.evaluate(DecisionAction.ENTER_NOW, _sla_observation(), now=NOW)
    stale = service.evaluate(
        DecisionAction.ENTER_NOW,
        _sla_observation(age_seconds=6),
        now=NOW,
    )
    mismatch_observation = _sla_observation()
    mismatch_observation = DataSLAObservation(
        current_price=_point("current_price", 268.5, source_class=SourceClass.AGGREGATED),
        volume=mismatch_observation.volume,
        orderbook=mismatch_observation.orderbook,
    )
    mismatch = service.evaluate(
        DecisionAction.ENTER_NOW,
        mismatch_observation,
        now=NOW,
    )

    assert passed.result is DataSLAResult.PASS
    assert passed.allows_requested_action is True
    assert stale.result is DataSLAResult.FAIL
    assert stale.allows_requested_action is False
    assert {issue.code for issue in stale.issues} == {"MAX_LATENCY_EXCEEDED"}
    assert mismatch.result is DataSLAResult.FAIL
    assert "SOURCE_CLASS_MISMATCH" in {issue.code for issue in mismatch.issues}


def test_data_integrity_marks_critical_conflict_and_missing_fields_as_fail() -> None:
    service = DataIntegrityService()
    observation = SourceObservation(
        source="MOEX ISS",
        source_class=SourceClass.OFFICIAL_PUBLIC,
        available=True,
        source_timestamp=NOW - timedelta(seconds=2),
        fetched_at=NOW - timedelta(seconds=1),
        fields={"price": 268.5},
        required_fields=("price", "volume"),
        max_age_seconds=30,
    )

    assessment = service.assess(
        (observation,),
        conflicts=(
            SourceConflict(
                field="price",
                sources=("MOEX ISS", "secondary"),
                details="material disagreement confirmed by caller",
            ),
        ),
        now=NOW,
    )

    assert assessment.status is GateResult.FAIL
    assert assessment.confidence is DataConfidence.LOW
    assert {issue.code for issue in assessment.issues} == {
        "MISSING_CRITICAL_FIELDS",
        "CONFLICTING_SOURCES",
    }
    assert assessment.allows_trade_decision is False


def test_data_integrity_clean_official_source_is_high_confidence() -> None:
    assessment = DataIntegrityService().assess(
        (
            SourceObservation(
                source="MOEX ISS",
                source_class=SourceClass.OFFICIAL_PUBLIC,
                available=True,
                source_timestamp=NOW - timedelta(seconds=2),
                fetched_at=NOW - timedelta(seconds=1),
                fields={"price": 268.5, "volume": 100_000.0},
                required_fields=("price", "volume"),
                max_age_seconds=30,
            ),
        ),
        now=NOW,
    )

    assert assessment.status is GateResult.PASS
    assert assessment.confidence is DataConfidence.HIGH


def _microstructure_input(**overrides: object) -> MicrostructureInput:
    values: dict[str, object] = {
        "direction": JournalDirection.LONG,
        "session_state": TradingSessionState.CONTINUOUS,
        "current_price": 100.05,
        "tick_size": 0.01,
        "lot_size": 10,
        "best_bid": 100.0,
        "best_ask": 100.1,
        "orderbook_timestamp": NOW - timedelta(seconds=1),
        "orderbook_quality": "PASS",
        "lower_price_band": 90.0,
        "upper_price_band": 110.0,
        "corporate_actions": CorporateActionFlags(
            dividend_ex_date=False,
            split_or_reverse_split=False,
            additional_issue=False,
            buyback_or_tender=False,
            conversion=False,
            reorganization=False,
            delisting=False,
        ),
    }
    values.update(overrides)
    return MicrostructureInput(**values)  # type: ignore[arg-type]


def test_microstructure_guard_passes_known_inputs_and_fails_halt() -> None:
    guard = MicrostructureGuard(max_spread_pct=0.002, max_orderbook_age_seconds=5)

    passed = guard.evaluate(_microstructure_input(), now=NOW)
    halted = guard.evaluate(
        _microstructure_input(session_state=TradingSessionState.HALTED),
        now=NOW,
    )

    assert passed.status is MicrostructureStatus.PASS
    assert passed.allows_enter_now is True
    assert halted.status is MicrostructureStatus.FAIL
    assert halted.allows_enter_now is False


def test_microstructure_missing_or_unconfirmed_short_data_is_not_fabricated() -> None:
    guard = MicrostructureGuard()
    unknown = guard.evaluate(
        _microstructure_input(
            direction=JournalDirection.SHORT,
            corporate_actions=None,
            best_bid=None,
            best_ask=None,
            orderbook_timestamp=None,
            orderbook_quality=None,
            lower_price_band=None,
            upper_price_band=None,
            short_available=None,
            borrow_carry_pct=None,
        ),
        now=NOW,
    )
    unavailable_short = guard.evaluate(
        _microstructure_input(
            direction=JournalDirection.SHORT,
            short_available=False,
            borrow_carry_pct=None,
        ),
        now=NOW,
    )

    assert unknown.status is MicrostructureStatus.DATA_NOT_AVAILABLE
    assert unknown.allows_enter_now is False
    assert "short_availability" in unknown.unavailable_fields
    assert unavailable_short.status is MicrostructureStatus.FAIL


async def test_context_repository_enforces_point_in_time_queries(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'contexts.db').as_posix()}"
    engine, factory = create_engine_and_session(database_url)
    await init_db(engine)
    repository = ContextRepositoryV24()
    try:
        visible = ContextFact(
            context_type=ContextType.NEWS,
            subject="SBER",
            source="official-disclosure",
            source_class=SourceClass.OFFICIAL_PUBLIC,
            publication_time=NOW - timedelta(hours=2),
            available_from=NOW - timedelta(hours=2),
            fetched_at=NOW - timedelta(hours=1),
            source_url="https://example.invalid/disclosure/1",
            data_confidence=DataConfidence.HIGH,
            payload={"event": "board_decision"},
        )
        future = ContextFact(
            context_type=ContextType.NEWS,
            subject="SBER",
            source="official-disclosure",
            source_class=SourceClass.OFFICIAL_PUBLIC,
            publication_time=NOW + timedelta(hours=1),
            available_from=NOW + timedelta(hours=1),
            fetched_at=NOW + timedelta(hours=1),
            source_url="https://example.invalid/disclosure/2",
            data_confidence=DataConfidence.HIGH,
            payload={"event": "future_event"},
        )
        async with factory() as session, session.begin():
            await repository.save(session, visible)
            await repository.save(session, future)
        async with factory() as session:
            facts = await repository.list_as_of(
                session,
                context_type=ContextType.NEWS,
                subject="SBER",
                as_of=NOW,
            )

        assert [fact.payload["event"] for fact in facts] == ["board_decision"]
    finally:
        await engine.dispose()


async def test_unavailable_context_provider_returns_typed_fail_safe() -> None:
    provider = UnavailableContextProvider()

    news = await provider.get_news_context("SBER", as_of=NOW)
    cross_asset = await provider.get_cross_asset_context("RUB", as_of=NOW)

    assert news.records == ()
    assert news.availability is DataAvailability.DATA_NOT_AVAILABLE
    assert news.reason == "BLOCKED_BY_DATA_SOURCE"
    assert cross_asset.availability is DataAvailability.DATA_NOT_AVAILABLE
