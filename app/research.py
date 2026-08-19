from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import replace
from datetime import UTC, date, datetime, time
from pathlib import Path

from app.config import get_settings
from app.db import create_engine_and_session
from app.logging_config import configure_logging
from app.migrations import migrate_database
from app.moex import MoexClient
from app.research_data import ResearchDataService, load_research_config


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )


def _date_end(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = date.fromisoformat(value)
    return datetime.combine(parsed, time.max, tzinfo=UTC)


async def ingest_dataset(
    *,
    tickers: list[str] | None = None,
    timeframes: list[str] | None = None,
    date_to: datetime | None = None,
) -> dict[str, object]:
    settings = get_settings()
    configure_logging(settings.log_level)
    config = load_research_config(settings.research_config_path)
    if tickers:
        config = replace(config, tickers=tuple(dict.fromkeys(item.upper() for item in tickers)))
    if timeframes:
        selected = set(timeframes)
        missing = selected - set(config.start_dates)
        if missing:
            raise ValueError(
                f"timeframes absent from research config: {', '.join(sorted(missing))}"
            )
        config = replace(
            config,
            start_dates={
                key: value for key, value in config.start_dates.items() if key in selected
            },
        )

    await migrate_database(settings.research_database_url)
    engine, session_factory = create_engine_and_session(settings.research_database_url)
    try:
        async with MoexClient(
            settings.moex_base_url,
            timeout_seconds=settings.moex_request_timeout_seconds,
            max_retries=settings.moex_max_retries,
            api_token=settings.moex_api_token,
        ) as moex:
            result = await ResearchDataService(
                config,
                session_factory,
                moex,
                concurrency=min(5, settings.moex_request_concurrency),
            ).sync(date_to=date_to)
        write_json(Path(settings.research_output_dir) / "dataset_manifest.json", result)
        return result
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="MOEX validation and calibration research")
    subparsers = parser.add_subparsers(dest="command", required=True)
    ingest = subparsers.add_parser("ingest", help="incrementally update the research dataset")
    ingest.add_argument("--tickers", nargs="+")
    ingest.add_argument("--timeframes", nargs="+")
    ingest.add_argument("--date-to", help="inclusive YYYY-MM-DD cutoff")
    args = parser.parse_args()

    if args.command == "ingest":
        result = asyncio.run(
            ingest_dataset(
                tickers=args.tickers,
                timeframes=args.timeframes,
                date_to=_date_end(args.date_to),
            )
        )
        print(
            json.dumps(
                {
                    "dataset": result["dataset"],
                    "available_tickers": result["available_tickers"],
                    "missing_tickers": result["missing_tickers"],
                    "errors": result["errors"],
                    "coverage_rows": len(result["coverage"]),
                },
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
