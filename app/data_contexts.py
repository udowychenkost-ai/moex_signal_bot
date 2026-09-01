from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ContextRecordV24
from app.v24_domain import ContextType, DataAvailability, DataConfidence, SourceClass


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _canonical_json(value: Mapping[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class ContextFact:
    context_type: ContextType
    subject: str
    source: str
    source_class: SourceClass
    publication_time: datetime
    available_from: datetime
    fetched_at: datetime
    source_url: str | None
    data_confidence: DataConfidence
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        if not self.subject.strip() or not self.source.strip():
            raise ValueError("Context subject and source are required")
        publication = _aware_utc(self.publication_time)
        available = _aware_utc(self.available_from)
        fetched = _aware_utc(self.fetched_at)
        if available < publication:
            raise ValueError("available_from cannot precede publication_time")
        if fetched < available:
            raise ValueError("fetched_at cannot precede available_from")

    @property
    def payload_json(self) -> str:
        return _canonical_json(self.payload)

    @property
    def payload_hash(self) -> str:
        return hashlib.sha256(self.payload_json.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class FundamentalContext:
    records: tuple[ContextFact, ...]
    availability: DataAvailability
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class NewsContext:
    records: tuple[ContextFact, ...]
    availability: DataAvailability
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class CorporateEventContext:
    records: tuple[ContextFact, ...]
    availability: DataAvailability
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class CrossAssetContext:
    records: tuple[ContextFact, ...]
    availability: DataAvailability
    reason: str | None = None


class FundamentalContextProvider(Protocol):
    async def get_fundamental_context(
        self, subject: str, *, as_of: datetime
    ) -> FundamentalContext: ...


class NewsContextProvider(Protocol):
    async def get_news_context(self, subject: str, *, as_of: datetime) -> NewsContext: ...


class CorporateEventContextProvider(Protocol):
    async def get_corporate_event_context(
        self, subject: str, *, as_of: datetime
    ) -> CorporateEventContext: ...


class CrossAssetContextProvider(Protocol):
    async def get_cross_asset_context(
        self, subject: str, *, as_of: datetime
    ) -> CrossAssetContext: ...


class UnavailableContextProvider:
    """Fail-safe provider used until a reliable production source is configured."""

    def __init__(self, reason: str = "BLOCKED_BY_DATA_SOURCE") -> None:
        self.reason = reason

    async def get_fundamental_context(self, subject: str, *, as_of: datetime) -> FundamentalContext:
        return FundamentalContext((), DataAvailability.DATA_NOT_AVAILABLE, self.reason)

    async def get_news_context(self, subject: str, *, as_of: datetime) -> NewsContext:
        return NewsContext((), DataAvailability.DATA_NOT_AVAILABLE, self.reason)

    async def get_corporate_event_context(
        self, subject: str, *, as_of: datetime
    ) -> CorporateEventContext:
        return CorporateEventContext((), DataAvailability.DATA_NOT_AVAILABLE, self.reason)

    async def get_cross_asset_context(self, subject: str, *, as_of: datetime) -> CrossAssetContext:
        return CrossAssetContext((), DataAvailability.DATA_NOT_AVAILABLE, self.reason)


class ContextRepositoryV24:
    async def save(self, session: AsyncSession, fact: ContextFact) -> ContextRecordV24:
        key = (
            fact.context_type.value,
            fact.subject.strip().upper(),
            fact.source.strip(),
            _aware_utc(fact.available_from),
            fact.payload_hash,
        )
        existing = await session.scalar(
            select(ContextRecordV24).where(
                ContextRecordV24.context_type == key[0],
                ContextRecordV24.subject == key[1],
                ContextRecordV24.source == key[2],
                ContextRecordV24.available_from == key[3],
                ContextRecordV24.payload_hash == key[4],
            )
        )
        if existing is not None:
            return existing
        record = ContextRecordV24(
            context_type=key[0],
            subject=key[1],
            source=key[2],
            source_class=fact.source_class.value,
            publication_time=_aware_utc(fact.publication_time),
            available_from=key[3],
            fetched_at=_aware_utc(fact.fetched_at),
            source_url=fact.source_url,
            data_confidence=fact.data_confidence.value,
            payload=fact.payload_json,
            payload_hash=key[4],
        )
        session.add(record)
        await session.flush()
        return record

    async def list_as_of(
        self,
        session: AsyncSession,
        *,
        context_type: ContextType,
        subject: str,
        as_of: datetime,
        limit: int = 100,
    ) -> list[ContextFact]:
        decision_time = _aware_utc(as_of)
        rows: Sequence[ContextRecordV24] = (
            await session.scalars(
                select(ContextRecordV24)
                .where(
                    ContextRecordV24.context_type == context_type.value,
                    ContextRecordV24.subject == subject.strip().upper(),
                    ContextRecordV24.available_from <= decision_time,
                    ContextRecordV24.fetched_at <= decision_time,
                )
                .order_by(ContextRecordV24.available_from.desc(), ContextRecordV24.id.desc())
                .limit(limit)
            )
        ).all()
        return [
            ContextFact(
                context_type=ContextType(row.context_type),
                subject=row.subject,
                source=row.source,
                source_class=SourceClass(row.source_class),
                publication_time=row.publication_time,
                available_from=row.available_from,
                fetched_at=row.fetched_at,
                source_url=row.source_url,
                data_confidence=DataConfidence(row.data_confidence),
                payload=json.loads(row.payload),
            )
            for row in rows
        ]
