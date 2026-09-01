from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from app.v24_domain import DataConfidence, GateResult, SourceClass

TRUSTED_SOURCE_CLASSES = {
    SourceClass.OFFICIAL_REALTIME,
    SourceClass.OFFICIAL_PUBLIC,
    SourceClass.BROKER_REALTIME,
    SourceClass.LICENSED_REALTIME,
    SourceClass.MANUAL_CONFIRMED,
}


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _is_missing(value: object) -> bool:
    return value is None or (isinstance(value, float) and not math.isfinite(value))


@dataclass(frozen=True, slots=True)
class SourceObservation:
    source: str
    source_class: SourceClass
    available: bool
    source_timestamp: datetime | None
    fetched_at: datetime | None
    fields: Mapping[str, object]
    required_fields: tuple[str, ...] = ()
    critical: bool = True
    max_age_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class SourceConflict:
    field: str
    sources: tuple[str, ...]
    details: str
    critical: bool = True


@dataclass(frozen=True, slots=True)
class IntegrityIssue:
    code: str
    source: str | None
    details: str
    critical: bool


@dataclass(frozen=True, slots=True)
class DataIntegrityAssessment:
    status: GateResult
    confidence: DataConfidence
    issues: tuple[IntegrityIssue, ...]
    available_sources: tuple[str, ...]
    checked_at: datetime

    @property
    def allows_trade_decision(self) -> bool:
        return self.status is not GateResult.FAIL


class DataIntegrityService:
    def assess(
        self,
        observations: tuple[SourceObservation, ...],
        *,
        conflicts: tuple[SourceConflict, ...] = (),
        now: datetime | None = None,
    ) -> DataIntegrityAssessment:
        checked_at = _aware_utc(now or datetime.now(UTC))
        issues: list[IntegrityIssue] = []
        available: list[SourceObservation] = []
        for observation in observations:
            source = observation.source.strip() or "UNNAMED_SOURCE"
            if observation.max_age_seconds is not None and observation.max_age_seconds < 0:
                raise ValueError("max_age_seconds cannot be negative")
            if not observation.available:
                issues.append(
                    IntegrityIssue(
                        code="SOURCE_UNAVAILABLE",
                        source=source,
                        details="Required source did not return data",
                        critical=observation.critical,
                    )
                )
                continue
            available.append(observation)
            missing_fields = [
                field
                for field in observation.required_fields
                if field not in observation.fields or _is_missing(observation.fields[field])
            ]
            if missing_fields:
                issues.append(
                    IntegrityIssue(
                        code="MISSING_CRITICAL_FIELDS",
                        source=source,
                        details=", ".join(sorted(missing_fields)),
                        critical=observation.critical,
                    )
                )
            if observation.source_timestamp is None or observation.fetched_at is None:
                issues.append(
                    IntegrityIssue(
                        code="MISSING_TIMESTAMP",
                        source=source,
                        details="source_timestamp and fetched_at are required",
                        critical=observation.critical,
                    )
                )
            else:
                source_time = _aware_utc(observation.source_timestamp)
                fetched_at = _aware_utc(observation.fetched_at)
                if source_time > checked_at:
                    issues.append(
                        IntegrityIssue(
                            code="FUTURE_TIMESTAMP",
                            source=source,
                            details="Source timestamp is later than decision time",
                            critical=observation.critical,
                        )
                    )
                if fetched_at < source_time:
                    issues.append(
                        IntegrityIssue(
                            code="FETCH_PRECEDES_SOURCE",
                            source=source,
                            details="fetched_at precedes source_timestamp",
                            critical=observation.critical,
                        )
                    )
                age_seconds = (checked_at - source_time).total_seconds()
                if (
                    observation.max_age_seconds is not None
                    and age_seconds > observation.max_age_seconds
                ):
                    issues.append(
                        IntegrityIssue(
                            code="STALE_SOURCE",
                            source=source,
                            details=(
                                f"age={age_seconds:.3f}s limit={observation.max_age_seconds:.3f}s"
                            ),
                            critical=observation.critical,
                        )
                    )
            if observation.source_class is SourceClass.UNKNOWN:
                issues.append(
                    IntegrityIssue(
                        code="UNKNOWN_SOURCE_CLASS",
                        source=source,
                        details="Source confidence cannot be established",
                        critical=False,
                    )
                )

        for conflict in conflicts:
            issues.append(
                IntegrityIssue(
                    code="CONFLICTING_SOURCES",
                    source=",".join(conflict.sources),
                    details=f"{conflict.field}: {conflict.details}",
                    critical=conflict.critical,
                )
            )

        if any(issue.critical for issue in issues):
            status = GateResult.FAIL
        elif issues:
            status = GateResult.WARN
        else:
            status = GateResult.PASS

        if not available:
            confidence = DataConfidence.UNKNOWN
        elif status is GateResult.FAIL:
            confidence = DataConfidence.LOW
        elif status is GateResult.WARN:
            confidence = (
                DataConfidence.MEDIUM
                if any(item.source_class in TRUSTED_SOURCE_CLASSES for item in available)
                else DataConfidence.LOW
            )
        elif all(item.source_class in TRUSTED_SOURCE_CLASSES for item in available):
            confidence = DataConfidence.HIGH
        else:
            confidence = DataConfidence.MEDIUM

        return DataIntegrityAssessment(
            status=status,
            confidence=confidence,
            issues=tuple(issues),
            available_sources=tuple(item.source for item in available),
            checked_at=checked_at,
        )
