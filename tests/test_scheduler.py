from __future__ import annotations

import pytest

from app.config import Settings
from app.scheduler import ScheduledJobs, build_scheduler


class StubScanner:
    def __init__(self) -> None:
        self.calls = 0

    async def scan_ideas(self) -> dict[str, int]:
        self.calls += 1
        return {"ideas_created": 1}

    async def ingest(self) -> dict[str, int]:
        return {"candles": 1, "errors": 0}

    async def track_lifecycle(self) -> dict[str, int]:
        return {"evaluated": 0, "transitions": 0}

    async def sync_paper(self) -> dict[str, int]:
        return {"open": 0, "closed": 0}


class StubReporting:
    def __init__(self) -> None:
        self.calls = 0

    async def dispatch_due(self, bot: object) -> dict[str, int]:
        self.calls += 1
        return {"reports_sent": 1}


@pytest.mark.asyncio
async def test_scanning_and_reporting_are_independent_jobs() -> None:
    scanner = StubScanner()
    reporting = StubReporting()
    jobs = ScheduledJobs(Settings(_env_file=None), scanner, reporting, object())

    await jobs.scan_market()
    assert scanner.calls == 1
    assert reporting.calls == 0

    await jobs.dispatch_reports()
    assert scanner.calls == 1
    assert reporting.calls == 1


def test_one_scheduler_contains_separate_scan_and_report_tasks() -> None:
    jobs = ScheduledJobs(
        Settings(_env_file=None),
        StubScanner(),
        StubReporting(),
        object(),
    )
    scheduler = build_scheduler(Settings(_env_file=None), jobs)
    assert {job.id for job in scheduler.get_jobs()} == {
        "market_ingestion",
        "idea_scanning",
        "lifecycle_tracking",
        "telegram_reporting",
        "daily_summary",
    }
    assert scheduler.get_job("idea_scanning").next_run_time is not None
