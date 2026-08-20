from __future__ import annotations

import json
import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain import DEFAULT_SECTORS, FundamentalScoreData
from app.models import FundamentalReport, Instrument
from app.observation import aware_utc

SUPPORTED_METRICS = {
    "pe",
    "pb",
    "ev_ebitda",
    "roe",
    "roa",
    "operating_margin",
    "net_margin",
    "debt_ebitda",
    "net_debt_ebitda",
    "revenue_yoy",
    "earnings_yoy",
    "eps_growth",
    "fcf",
    "fcf_growth",
    "fcf_yield",
    "dividend_yield",
    "dividend_consistency",
    "payout_ratio",
}
OFFICIAL_HOST_SUFFIXES = (
    "moex.com",
    "e-disclosure.ru",
    "cbr.ru",
    "sberbank.com",
    "gazprom.com",
    "lukoil.com",
    "yandex.com",
    "nornickel.com",
    "rosneft.com",
    "novatek.ru",
    "tatneft.ru",
    "mts.ru",
    "polyus.com",
    "magnit.com",
    "severstal.com",
    "nlmk.com",
    "alrosa.ru",
    "vtb.com",
    "gazprom-neft.ru",
    "phosagro.com",
    "interrao.ru",
    "surgutneftegas.ru",
)


@dataclass(frozen=True, slots=True)
class FundamentalReportData:
    ticker: str
    sector: str
    report_period: str
    publication_date: datetime
    available_from: datetime
    source: str
    source_url: str
    metrics: dict[str, float]


class FundamentalDataProvider(Protocol):
    async def fetch(self) -> list[FundamentalReportData]: ...


def _parse_timestamp(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return aware_utc(parsed)


def _official_or_issuer_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return (
        parsed.scheme == "https"
        and bool(host)
        and any(host == suffix or host.endswith(f".{suffix}") for suffix in OFFICIAL_HOST_SUFFIXES)
    )


class OfficialDisclosureJsonProvider:
    """Validated import adapter for normalized official disclosure facts.

    Public disclosure sites publish documents rather than a stable normalized
    ratios API. This adapter deliberately imports reviewed facts with their
    original URL and availability timestamp instead of silently scraping or
    fabricating missing ratios.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    async def fetch(self) -> list[FundamentalReportData]:
        if not self.path.exists():
            return []
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        raw_reports = payload.get("reports") if isinstance(payload, dict) else None
        if not isinstance(raw_reports, list):
            raise ValueError("fundamental JSON must contain a reports list")
        reports: list[FundamentalReportData] = []
        for index, raw in enumerate(raw_reports):
            if not isinstance(raw, dict):
                raise ValueError(f"reports[{index}] must be an object")
            ticker = str(raw["ticker"]).upper()
            publication = _parse_timestamp(raw["publication_date"])
            available = _parse_timestamp(raw["available_from"])
            if available < publication:
                raise ValueError(
                    f"{ticker} {raw['report_period']}: available_from precedes publication_date"
                )
            source_url = str(raw["source_url"])
            if not _official_or_issuer_url(source_url):
                raise ValueError(f"{ticker}: source_url is not an official/issuer host")
            raw_metrics = raw.get("metrics")
            if not isinstance(raw_metrics, dict) or not raw_metrics:
                raise ValueError(f"{ticker}: metrics must be a non-empty object")
            unknown = set(raw_metrics) - SUPPORTED_METRICS
            if unknown:
                raise ValueError(f"{ticker}: unsupported metrics: {', '.join(sorted(unknown))}")
            metrics: dict[str, float] = {}
            for name, value in raw_metrics.items():
                number = float(value)
                if not math.isfinite(number):
                    raise ValueError(f"{ticker}: metric {name} is not finite")
                metrics[name] = number
            reports.append(
                FundamentalReportData(
                    ticker=ticker,
                    sector=str(raw.get("sector") or DEFAULT_SECTORS.get(ticker, "Unknown")),
                    report_period=str(raw["report_period"]),
                    publication_date=publication,
                    available_from=available,
                    source=str(raw["source"]),
                    source_url=source_url,
                    metrics=metrics,
                )
            )
        return reports


class FundamentalIngestionService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        providers: Iterable[FundamentalDataProvider],
    ) -> None:
        self.session_factory = session_factory
        self.providers = tuple(providers)

    async def sync(self) -> int:
        reports: list[FundamentalReportData] = []
        for provider in self.providers:
            reports.extend(await provider.fetch())
        if not reports:
            return 0
        values = [
            {
                "ticker": report.ticker,
                "sector": report.sector,
                "report_period": report.report_period,
                "publication_date": report.publication_date,
                "available_from": report.available_from,
                "source": report.source,
                "source_url": report.source_url,
                "metrics": json.dumps(report.metrics, ensure_ascii=False, sort_keys=True),
                "ingested_at": datetime.now(UTC),
            }
            for report in reports
        ]
        async with self.session_factory() as session, session.begin():
            dialect = session.bind.dialect.name if session.bind else ""
            statement = (
                postgresql_insert(FundamentalReport).values(values)
                if dialect == "postgresql"
                else sqlite_insert(FundamentalReport).values(values)
            )
            statement = statement.on_conflict_do_update(
                index_elements=["ticker", "report_period", "available_from", "source"],
                set_={
                    "sector": statement.excluded.sector,
                    "publication_date": statement.excluded.publication_date,
                    "source_url": statement.excluded.source_url,
                    "metrics": statement.excluded.metrics,
                    "ingested_at": statement.excluded.ingested_at,
                },
            )
            await session.execute(statement)
        return len(values)


def _latest_known(
    reports: Iterable[FundamentalReport], as_of: datetime
) -> dict[str, FundamentalReport]:
    latest: dict[str, FundamentalReport] = {}
    cutoff = aware_utc(as_of)
    for report in reports:
        if aware_utc(report.available_from) > cutoff:
            continue
        current = latest.get(report.ticker)
        if current is None or aware_utc(report.available_from) > aware_utc(current.available_from):
            latest[report.ticker] = report
    return latest


def _percentile_score(value: float, peers: list[float], *, lower_is_better: bool) -> float | None:
    valid = sorted(item for item in peers if math.isfinite(item))
    if len(valid) < 2:
        return None
    below = sum(item < value for item in valid)
    equal = sum(item == value for item in valid)
    percentile = (below + equal * 0.5) / len(valid)
    score = (percentile - 0.5) * 200
    return -score if lower_is_better else score


COMPONENT_METRICS = {
    "valuation": (("pe", True), ("pb", True), ("ev_ebitda", True)),
    "profitability": (
        ("roe", False),
        ("roa", False),
        ("operating_margin", False),
        ("net_margin", False),
    ),
    "debt": (("debt_ebitda", True), ("net_debt_ebitda", True)),
    "growth": (("revenue_yoy", False), ("earnings_yoy", False), ("eps_growth", False)),
    "cashflow": (("fcf_yield", False), ("fcf_growth", False)),
    "dividend": (("dividend_yield", False), ("dividend_consistency", False)),
}


class FundamentalAnalysisService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def score_at(self, ticker: str, as_of: datetime) -> FundamentalScoreData:
        ticker = ticker.upper()
        cutoff = aware_utc(as_of)
        async with self.session_factory() as session:
            instrument = await session.get(Instrument, ticker)
            sector = instrument.sector if instrument else DEFAULT_SECTORS.get(ticker, "Unknown")
            reports = list(
                await session.scalars(
                    select(FundamentalReport)
                    .where(
                        FundamentalReport.sector == sector,
                        FundamentalReport.available_from <= cutoff,
                    )
                    .order_by(FundamentalReport.available_from)
                )
            )
        latest = _latest_known(reports, cutoff)
        own = latest.get(ticker)
        if own is None:
            return FundamentalScoreData(
                score=0.0,
                components={},
                metrics={},
                sector=sector,
                publications=[],
                label="нет данных",
                as_of=cutoff,
            )
        own_metrics = json.loads(own.metrics)
        peer_metrics = {key: json.loads(report.metrics) for key, report in latest.items()}
        component_scores: dict[str, float] = {}
        for component, definitions in COMPONENT_METRICS.items():
            metric_scores: list[float] = []
            for metric, lower_is_better in definitions:
                value = own_metrics.get(metric)
                if value is None:
                    continue
                if metric in {"pe", "pb", "ev_ebitda"} and float(value) <= 0:
                    # Loss-making/negative-equity multiples are not "cheap" and
                    # cannot safely participate in a lower-is-better percentile.
                    continue
                peer_values = [
                    float(metrics[metric])
                    for metrics in peer_metrics.values()
                    if metrics.get(metric) is not None
                    and not (metric in {"pe", "pb", "ev_ebitda"} and float(metrics[metric]) <= 0)
                ]
                score = _percentile_score(
                    float(value), peer_values, lower_is_better=lower_is_better
                )
                if score is not None:
                    metric_scores.append(score)
            if metric_scores:
                component_scores[component] = round(sum(metric_scores) / len(metric_scores), 4)
        # Raw FCF and payout are diagnostics; they do not create a cross-company
        # score because scale and accounting context make direct ranking unsafe.
        score = sum(component_scores.values()) / len(component_scores) if component_scores else 0.0
        label = "позитивный" if score >= 20 else ("негативный" if score <= -20 else "нейтральный")
        publication = {
            "report_period": own.report_period,
            "publication_date": aware_utc(own.publication_date).isoformat(),
            "available_from": aware_utc(own.available_from).isoformat(),
            "source": own.source,
            "source_url": own.source_url,
        }
        return FundamentalScoreData(
            score=round(score, 4),
            components=component_scores,
            metrics={name: float(value) for name, value in own_metrics.items()},
            sector=sector,
            publications=[publication],
            label=label,
            as_of=cutoff,
        )

    async def coverage(
        self,
        tickers: Iterable[str],
        as_of: datetime | None = None,
    ) -> dict[str, object]:
        cutoff = aware_utc(as_of or datetime.now(UTC))
        rows = {ticker.upper(): await self.score_at(ticker, cutoff) for ticker in tickers}
        covered = [ticker for ticker, score in rows.items() if score.publications]
        return {
            "as_of": cutoff.isoformat(),
            "requested": len(rows),
            "covered": len(covered),
            "coverage_pct": round(len(covered) / len(rows) * 100, 2) if rows else 0.0,
            "covered_tickers": covered,
            "missing_tickers": [ticker for ticker in rows if ticker not in covered],
        }
