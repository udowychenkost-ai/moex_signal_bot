from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.migrations import HEAD_REVISION
from app.models import JournalHealthProbe

REQUIRED_JOURNAL_TABLES = frozenset(
    {
        "idea_journals",
        "decision_snapshots_v24",
        "model_trade_journals",
        "actual_trade_journals",
        "trade_event_journal",
        "journal_health_probes",
    }
)


@dataclass(frozen=True, slots=True)
class JournalStorageHealth:
    available: bool
    writable: bool
    migration_current: bool
    revision: str | None
    missing_tables: tuple[str, ...]
    checked_at: datetime
    error: str | None = None


class JournalHealthService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def check(self) -> JournalStorageHealth:
        checked_at = datetime.now(UTC)
        revision: str | None = None
        missing: tuple[str, ...] = tuple(sorted(REQUIRED_JOURNAL_TABLES))
        migration_current = False
        writable = False
        try:
            async with self.session_factory() as session:
                connection = await session.connection()
                tables = await connection.run_sync(
                    lambda sync_connection: set(inspect(sync_connection).get_table_names())
                )
                missing = tuple(sorted(REQUIRED_JOURNAL_TABLES - tables))
                if "alembic_version" in tables:
                    revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
                migration_current = revision == HEAD_REVISION
                await session.rollback()

                transaction = await session.begin()
                try:
                    session.add(JournalHealthProbe(checked_at=checked_at))
                    await session.flush()
                    writable = True
                finally:
                    await transaction.rollback()
        except Exception as error:
            return JournalStorageHealth(
                available=False,
                writable=False,
                migration_current=migration_current,
                revision=revision,
                missing_tables=missing,
                checked_at=checked_at,
                error=f"{type(error).__name__}: journal persistence probe failed",
            )
        return JournalStorageHealth(
            available=writable and migration_current and not missing,
            writable=writable,
            migration_current=migration_current,
            revision=revision,
            missing_tables=missing,
            checked_at=checked_at,
        )
