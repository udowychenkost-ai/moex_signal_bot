from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.db import create_engine_and_session, init_db
from app.domain import InstrumentData
from app.fundamentals import (
    FundamentalAnalysisService,
    FundamentalIngestionService,
    OfficialDisclosureJsonProvider,
)
from app.repositories import upsert_instruments


def _report(
    ticker: str,
    available: str,
    *,
    pe: float,
    roe: float,
    period: str,
) -> dict[str, object]:
    return {
        "ticker": ticker,
        "sector": "Financials",
        "report_period": period,
        "publication_date": available,
        "available_from": available,
        "source": "moex_official",
        "source_url": "https://www.moex.com/a2920",
        "metrics": {"pe": pe, "roe": roe},
    }


@pytest.mark.asyncio
async def test_fundamental_scoring_has_no_publication_lookahead(tmp_path: Path) -> None:
    path = tmp_path / "fundamentals.json"
    path.write_text(
        json.dumps(
            {
                "reports": [
                    _report("SBER", "2025-04-01T00:00:00+00:00", pe=20, roe=5, period="2024"),
                    _report("SBER", "2025-08-01T00:00:00+00:00", pe=5, roe=35, period="2025-H1"),
                    _report("VTBR", "2025-04-01T00:00:00+00:00", pe=12, roe=10, period="2024"),
                    _report("MOEX", "2025-04-01T00:00:00+00:00", pe=15, roe=8, period="2024"),
                ]
            }
        ),
        encoding="utf-8",
    )
    engine, factory = create_engine_and_session("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    async with factory() as session, session.begin():
        await upsert_instruments(
            session,
            [
                InstrumentData("SBER", "TQBR", "Sber"),
                InstrumentData("VTBR", "TQBR", "VTB"),
                InstrumentData("MOEX", "TQBR", "MOEX"),
            ],
        )
    inserted = await FundamentalIngestionService(
        factory, [OfficialDisclosureJsonProvider(path)]
    ).sync()
    service = FundamentalAnalysisService(factory)

    before = await service.score_at("SBER", datetime(2025, 7, 1, tzinfo=UTC))
    after = await service.score_at("SBER", datetime(2025, 9, 1, tzinfo=UTC))

    assert inserted == 4
    assert before.metrics["pe"] == 20
    assert before.publications[0]["report_period"] == "2024"
    assert after.metrics["pe"] == 5
    assert after.publications[0]["report_period"] == "2025-H1"
    assert after.score > before.score
    await engine.dispose()


@pytest.mark.asyncio
async def test_provider_rejects_available_before_publication(tmp_path: Path) -> None:
    path = tmp_path / "invalid.json"
    report = _report("SBER", "2025-04-01T00:00:00+00:00", pe=9, roe=20, period="2024")
    report["publication_date"] = "2025-05-01T00:00:00+00:00"
    path.write_text(json.dumps({"reports": [report]}), encoding="utf-8")

    with pytest.raises(ValueError, match="precedes publication_date"):
        await OfficialDisclosureJsonProvider(path).fetch()


@pytest.mark.asyncio
async def test_provider_rejects_untrusted_or_insecure_source_url(tmp_path: Path) -> None:
    path = tmp_path / "untrusted.json"
    report = _report("SBER", "2025-04-01T00:00:00+00:00", pe=9, roe=20, period="2024")
    report["source_url"] = "http://example.invalid/copied-ratios"
    path.write_text(json.dumps({"reports": [report]}), encoding="utf-8")

    with pytest.raises(ValueError, match="not an official"):
        await OfficialDisclosureJsonProvider(path).fetch()
