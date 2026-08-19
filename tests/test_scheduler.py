from __future__ import annotations

import pytest

from app.config import Settings
from app.scheduler import ScheduledJobs, build_scheduler


class StubScanner:
    def __init__(self) -> None:
        self.calls = 0

    async def scan(self) -> dict[str, int]:
        self.calls += 1
        return {"ideas_created": 1}


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
    assert {job.id for job in scheduler.get_jobs()} == {"market_scan", "idea_reporting"}
    assert scheduler.get_job("market_scan").next_run_time is not None
