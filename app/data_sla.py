from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime

from app.config import Settings
from app.v24_domain import (
    ConfigurationStatus,
    DataSLAResult,
    DecisionAction,
    SourceClass,
)

IMMEDIATE_ACTIONS = {
    DecisionAction.ENTER_NOW,
    DecisionAction.MOVE_STOP,
    DecisionAction.CLOSE_NOW,
}


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class DataSLAConfig:
    version: str
    max_latency_enter_now: float | None
    max_latency_position_management: float | None
    max_latency_intraday_analysis: float | None
    required_quote_source_class: SourceClass | None
    required_volume_source_class: SourceClass | None
    required_orderbook_source_class: SourceClass | None

    @property
    def status(self) -> ConfigurationStatus:
        required = (
            self.max_latency_enter_now,
            self.max_latency_position_management,
            self.max_latency_intraday_analysis,
            self.required_quote_source_class,
            self.required_volume_source_class,
            self.required_orderbook_source_class,
        )
        return (
            ConfigurationStatus.CONFIGURED
            if all(value is not None for value in required)
            else ConfigurationStatus.NOT_CONFIGURED
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> DataSLAConfig:
        def source_class(value: str | None) -> SourceClass | None:
            return SourceClass(value.upper()) if value else None

        return cls(
            version=settings.data_sla_version,
            max_latency_enter_now=settings.max_latency_enter_now,
            max_latency_position_management=settings.max_latency_position_management,
            max_latency_intraday_analysis=settings.max_latency_intraday_analysis,
            required_quote_source_class=source_class(settings.required_quote_source_class),
            required_volume_source_class=source_class(settings.required_volume_source_class),
            required_orderbook_source_class=source_class(settings.required_orderbook_source_class),
        )


@dataclass(frozen=True, slots=True)
class MarketDataPoint:
    name: str
    value: float | None
    source: str | None
    source_class: SourceClass | None
    source_timestamp: datetime | None
    fetched_at: datetime | None


@dataclass(frozen=True, slots=True)
class DataSLAObservation:
    current_price: MarketDataPoint
    volume: MarketDataPoint | None = None
    orderbook: MarketDataPoint | None = None


@dataclass(frozen=True, slots=True)
class DataSLAIssue:
    code: str
    data_point: str | None
    details: str


@dataclass(frozen=True, slots=True)
class DataSLAAssessment:
    config_version: str
    status: ConfigurationStatus
    result: DataSLAResult
    action: DecisionAction
    issues: tuple[DataSLAIssue, ...]
    computed_delays_seconds: dict[str, float]
    checked_at: datetime

    @property
    def allows_requested_action(self) -> bool:
        if self.action in IMMEDIATE_ACTIONS:
            return (
                self.status is ConfigurationStatus.CONFIGURED and self.result is DataSLAResult.PASS
            )
        return self.result is not DataSLAResult.FAIL


class DataSLAService:
    def __init__(self, config: DataSLAConfig) -> None:
        self.config = config

    def _latency_limit(self, action: DecisionAction) -> float | None:
        if action is DecisionAction.ENTER_NOW:
            return self.config.max_latency_enter_now
        if action in {DecisionAction.MOVE_STOP, DecisionAction.CLOSE_NOW}:
            return self.config.max_latency_position_management
        return self.config.max_latency_intraday_analysis

    def _required_points(
        self, action: DecisionAction, observation: DataSLAObservation
    ) -> tuple[tuple[MarketDataPoint | None, SourceClass | None], ...]:
        quote = (observation.current_price, self.config.required_quote_source_class)
        volume = (observation.volume, self.config.required_volume_source_class)
        orderbook = (observation.orderbook, self.config.required_orderbook_source_class)
        if action is DecisionAction.ENTER_NOW:
            return quote, volume, orderbook
        if action in {DecisionAction.MOVE_STOP, DecisionAction.CLOSE_NOW}:
            return quote, orderbook
        return quote, volume

    def evaluate(
        self,
        action: DecisionAction,
        observation: DataSLAObservation,
        *,
        now: datetime | None = None,
    ) -> DataSLAAssessment:
        checked_at = _aware_utc(now or datetime.now(UTC))
        if self.config.status is ConfigurationStatus.NOT_CONFIGURED:
            missing = [
                name
                for name, value in (
                    ("MAX_LATENCY_ENTER_NOW", self.config.max_latency_enter_now),
                    (
                        "MAX_LATENCY_POSITION_MANAGEMENT",
                        self.config.max_latency_position_management,
                    ),
                    (
                        "MAX_LATENCY_INTRADAY_ANALYSIS",
                        self.config.max_latency_intraday_analysis,
                    ),
                    (
                        "REQUIRED_QUOTE_SOURCE_CLASS",
                        self.config.required_quote_source_class,
                    ),
                    (
                        "REQUIRED_VOLUME_SOURCE_CLASS",
                        self.config.required_volume_source_class,
                    ),
                    (
                        "REQUIRED_ORDERBOOK_SOURCE_CLASS",
                        self.config.required_orderbook_source_class,
                    ),
                )
                if value is None
            ]
            return DataSLAAssessment(
                config_version=self.config.version,
                status=ConfigurationStatus.NOT_CONFIGURED,
                result=DataSLAResult.NOT_CONFIGURED,
                action=action,
                issues=(
                    DataSLAIssue(
                        code="SLA_NOT_CONFIGURED",
                        data_point=None,
                        details=", ".join(missing),
                    ),
                ),
                computed_delays_seconds={},
                checked_at=checked_at,
            )

        latency_limit = self._latency_limit(action)
        if latency_limit is None:
            raise RuntimeError("Configured Data SLA has no latency limit for action")
        issues: list[DataSLAIssue] = []
        delays: dict[str, float] = {}
        for point, required_class in self._required_points(action, observation):
            if point is None:
                issues.append(
                    DataSLAIssue("MISSING_DATA_POINT", None, "Required observation is absent")
                )
                continue
            if point.value is None or not math.isfinite(point.value):
                issues.append(DataSLAIssue("MISSING_VALUE", point.name, "Market value is absent"))
            if not point.source:
                issues.append(
                    DataSLAIssue("MISSING_SOURCE", point.name, "Source is not identified")
                )
            if point.source_class is None or point.source_class is not required_class:
                actual = point.source_class.value if point.source_class else "UNKNOWN"
                expected = required_class.value if required_class else "UNCONFIGURED"
                issues.append(
                    DataSLAIssue(
                        "SOURCE_CLASS_MISMATCH",
                        point.name,
                        f"actual={actual} required={expected}",
                    )
                )
            if point.source_timestamp is None or point.fetched_at is None:
                issues.append(
                    DataSLAIssue(
                        "MISSING_TIMESTAMP",
                        point.name,
                        "source_timestamp and fetched_at are required",
                    )
                )
                continue
            source_time = _aware_utc(point.source_timestamp)
            fetched_at = _aware_utc(point.fetched_at)
            if fetched_at < source_time:
                issues.append(
                    DataSLAIssue(
                        "FETCH_PRECEDES_SOURCE",
                        point.name,
                        "fetched_at precedes source_timestamp",
                    )
                )
            delay = (checked_at - source_time).total_seconds()
            delays[point.name] = delay
            if delay < 0:
                issues.append(DataSLAIssue("FUTURE_TIMESTAMP", point.name, f"delay={delay:.3f}s"))
            elif delay > latency_limit:
                issues.append(
                    DataSLAIssue(
                        "MAX_LATENCY_EXCEEDED",
                        point.name,
                        f"delay={delay:.3f}s limit={latency_limit:.3f}s",
                    )
                )

        result = DataSLAResult.FAIL if issues else DataSLAResult.PASS
        return DataSLAAssessment(
            config_version=self.config.version,
            status=ConfigurationStatus.CONFIGURED,
            result=result,
            action=action,
            issues=tuple(issues),
            computed_delays_seconds=delays,
            checked_at=checked_at,
        )
