from __future__ import annotations

import logging
from collections import defaultdict
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai_analyst import AIAnalystService, AIReviewResult, apply_ai_review
from app.config import Settings
from app.domain import (
    AIVerdict,
    IdeaHorizon,
    QualityGateDecision,
    StaleMarketDataError,
    TradingIdeaData,
)
from app.experiments import (
    CandidateExperimentTracker,
    candidate_exists,
    candidate_experiment_key,
    cooldown_reason,
    published_today_count,
    save_candidate_experiment,
)
from app.fundamentals import FundamentalIngestionService
from app.idea_tracker import IdeaTracker
from app.ideas import TradingIdeaGenerator
from app.ingestion import IngestionService
from app.models import TelegramUser, TradingIdea
from app.paper import PaperTradingService
from app.quality import QualityGate, QualityGateResult, apply_quality_result
from app.repositories import list_active_instruments

logger = logging.getLogger(__name__)


class MarketScanner:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        ingestion: IngestionService,
        ideas: TradingIdeaGenerator,
        tracker: IdeaTracker,
        paper: PaperTradingService | None = None,
        fundamentals: FundamentalIngestionService | None = None,
        *,
        settings: Settings | None = None,
        quality_gate: QualityGate | None = None,
        ai_analyst: AIAnalystService | None = None,
        experiment_tracker: CandidateExperimentTracker | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.ingestion = ingestion
        self.ideas = ideas
        self.tracker = tracker
        self.paper = paper
        self.fundamentals = fundamentals
        self.settings = settings
        self.quality_gate = quality_gate
        self.ai_analyst = ai_analyst
        self.experiment_tracker = experiment_tracker

    async def ingest(self) -> dict[str, int]:
        await self.ingestion.sync_universe()
        result = await self.ingestion.sync_all()
        result["fundamental_reports"] = (
            await self.fundamentals.sync() if self.fundamentals is not None else 0
        )
        return result

    async def track_lifecycle(self) -> dict[str, int]:
        result = await self.tracker.track_all()
        if self.experiment_tracker is not None:
            result.update(await self.experiment_tracker.track_all())
        return result

    async def scan_ideas(self) -> dict[str, int | float]:
        if (
            self.settings is not None
            and self.settings.quality_gate_enabled
            and self.quality_gate is not None
        ):
            return await self._scan_ideas_v2()
        return await self._scan_ideas_legacy()

    async def _scan_ideas_legacy(self) -> dict[str, int]:
        async with self.session_factory() as session:
            instruments = await list_active_instruments(session)

        created = 0
        updated = 0
        skipped = 0
        stale = 0
        errors = 0
        for instrument in instruments:
            for horizon in IdeaHorizon:
                try:
                    result = await self.ideas.generate(instrument.secid, horizon)
                    if result is None:
                        skipped += 1
                    elif result.created:
                        created += 1
                    elif result.materially_changed:
                        updated += 1
                    else:
                        skipped += 1
                except StaleMarketDataError as error:
                    stale += 1
                    logger.warning("Idea generation blocked by freshness guard: %s", error)
                except Exception:
                    errors += 1
                    logger.exception("Idea scan failed for %s %s", instrument.secid, horizon.value)
        return {
            "ideas_created": created,
            "ideas_updated": updated,
            "ideas_skipped": skipped,
            "ideas_stale": stale,
            "idea_errors": errors,
        }

    async def _already_recorded(self, candidate: TradingIdeaData) -> bool:
        assert self.settings is not None
        key = candidate_experiment_key(candidate, self.settings.strategy_version)
        async with self.session_factory() as session:
            return await candidate_exists(session, key)

    async def _save_experiment(
        self,
        candidate: TradingIdeaData,
        quality: QualityGateResult,
        *,
        review: AIReviewResult | None = None,
        published: bool = False,
        publish_reason: str = "",
        published_idea_id: int | None = None,
    ) -> None:
        async with self.session_factory() as session, session.begin():
            await save_candidate_experiment(
                session,
                candidate,
                quality,
                review=review,
                published=published,
                publish_reason=publish_reason,
                published_idea_id=published_idea_id,
            )

    async def _scan_ideas_v2(self) -> dict[str, int | float]:
        assert self.settings is not None
        assert self.quality_gate is not None
        async with self.session_factory() as session:
            instruments = await list_active_instruments(session)

        counters: dict[str, int | float] = {
            "quant_candidates": 0,
            "quality_pass": 0,
            "quality_weak": 0,
            "quality_reject": 0,
            "cooldown_suppressed": 0,
            "rank_suppressed": 0,
            "ai_requests": 0,
            "ai_approved": 0,
            "ai_wait": 0,
            "ai_rejected": 0,
            "ai_errors": 0,
            "ai_fallbacks": 0,
            "ai_input_tokens": 0,
            "ai_output_tokens": 0,
            "ai_estimated_cost_usd": 0.0,
            "ideas_created": 0,
            "ideas_updated": 0,
            "ideas_skipped": 0,
            "ideas_stale": 0,
            "idea_errors": 0,
        }
        grouped: dict[IdeaHorizon, list[tuple[TradingIdeaData, QualityGateResult]]] = defaultdict(
            list
        )
        owner_ai_disabled = False
        if self.settings.admin_chat_ids:
            async with self.session_factory() as session:
                owner_preference = await session.scalar(
                    select(TelegramUser.ai_filter_enabled).where(
                        TelegramUser.telegram_id == self.settings.admin_chat_ids[0]
                    )
                )
            owner_ai_disabled = owner_preference is False
        for instrument in instruments:
            for horizon in IdeaHorizon:
                try:
                    candidate = await self.ideas.generate_candidate(instrument.secid, horizon)
                    if candidate is None:
                        counters["ideas_skipped"] += 1
                        continue
                    counters["quant_candidates"] += 1
                    quality = self.quality_gate.evaluate(candidate, data_fresh=True)
                    apply_quality_result(
                        candidate,
                        quality,
                        strategy_version=self.settings.strategy_version,
                    )
                    if await self._already_recorded(candidate):
                        counters["ideas_skipped"] += 1
                        continue
                    counters[f"quality_{quality.decision.value.lower()}"] += 1
                    if quality.decision != QualityGateDecision.PASS:
                        await self._save_experiment(
                            candidate,
                            quality,
                            publish_reason=f"QUALITY_{quality.decision.value}",
                        )
                        continue
                    grouped[horizon].append((candidate, quality))
                except StaleMarketDataError as error:
                    counters["ideas_stale"] += 1
                    logger.warning("Idea generation blocked by freshness guard: %s", error)
                except Exception:
                    counters["idea_errors"] += 1
                    logger.exception(
                        "V2 candidate scan failed for %s %s", instrument.secid, horizon
                    )

        for horizon, rows in grouped.items():
            rows.sort(key=lambda item: item[1].final_quality_score, reverse=True)
            ai_limit = self.settings.quality_ai_candidates_per_horizon
            ai_rows: list[tuple[TradingIdeaData, QualityGateResult]] = []
            for candidate, quality in rows:
                async with self.session_factory() as session:
                    reason = await cooldown_reason(
                        session,
                        candidate,
                        cooldown_hours=self.settings.idea_cooldown_hours(horizon),
                        confidence_delta=self.settings.idea_material_confidence_delta,
                    )
                if reason is not None:
                    counters["cooldown_suppressed"] += 1
                    await self._save_experiment(candidate, quality, publish_reason=reason)
                elif len(ai_rows) >= ai_limit:
                    counters["rank_suppressed"] += 1
                    await self._save_experiment(candidate, quality, publish_reason="AI_RANK_LIMIT")
                else:
                    ai_rows.append((candidate, quality))

            approved: list[tuple[TradingIdeaData, QualityGateResult, AIReviewResult | None]] = []
            for candidate, quality in ai_rows:
                if owner_ai_disabled:
                    approved.append((candidate, quality, None))
                    continue
                if not self.settings.ai_filter_enabled:
                    if self.settings.ai_allow_unreviewed_fallback:
                        approved.append((candidate, quality, None))
                    else:
                        counters["ai_wait"] += 1
                        await self._save_experiment(
                            candidate,
                            quality,
                            publish_reason="AI_DISABLED_WAIT",
                        )
                    continue
                if self.ai_analyst is None:
                    counters["ai_wait"] += 1
                    counters["ai_errors"] += 1
                    await self._save_experiment(
                        candidate,
                        quality,
                        publish_reason="AI_UNAVAILABLE",
                    )
                    continue
                review = await self.ai_analyst.review(candidate, quality)
                counters["ai_requests"] += review.request_count
                counters["ai_fallbacks"] += int(review.fallback_used)
                counters["ai_input_tokens"] += review.input_tokens
                counters["ai_output_tokens"] += review.output_tokens
                counters["ai_estimated_cost_usd"] += review.estimated_cost_usd
                apply_ai_review(candidate, review)
                counters["ai_errors"] += review.error_count
                if review.approved:
                    counters["ai_approved"] += 1
                    approved.append((candidate, quality, review))
                else:
                    key = (
                        "ai_rejected" if review.analysis.verdict == AIVerdict.REJECT else "ai_wait"
                    )
                    counters[key] += 1
                    await self._save_experiment(
                        candidate,
                        quality,
                        review=review,
                        publish_reason=f"AI_{review.analysis.verdict}",
                    )

            local_now = datetime.now(UTC).astimezone(ZoneInfo(self.settings.scheduler_timezone))
            day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
            async with self.session_factory() as session:
                published_today = await published_today_count(
                    session,
                    horizon=horizon.value,
                    strategy_version=self.settings.strategy_version,
                    day_start_utc=day_start,
                )
            daily_remaining = max(
                0,
                self.settings.max_new_ideas_per_day(horizon) - published_today,
            )
            scan_remaining = self.settings.max_new_ideas_per_scan(horizon)
            for candidate, quality, review in approved:
                async with self.session_factory() as session:
                    open_same = await session.scalar(
                        select(TradingIdea.id)
                        .where(
                            TradingIdea.ticker == candidate.ticker,
                            TradingIdea.horizon == horizon.value,
                            TradingIdea.direction == candidate.direction.value,
                            TradingIdea.status.in_(("PENDING_ENTRY", "ACTIVE")),
                        )
                        .limit(1)
                    )
                is_update = open_same is not None
                if not is_update and (scan_remaining <= 0 or daily_remaining <= 0):
                    counters["rank_suppressed"] += 1
                    await self._save_experiment(
                        candidate,
                        quality,
                        review=review,
                        publish_reason="TOP_N_LIMIT",
                    )
                    continue
                result = await self.ideas.persist_candidate(candidate)
                await self._save_experiment(
                    candidate,
                    quality,
                    review=review,
                    published=True,
                    publish_reason="PUBLISHED" if result.created else "REASSESSED",
                    published_idea_id=result.idea.id,
                )
                if result.created:
                    counters["ideas_created"] += 1
                    scan_remaining -= 1
                    daily_remaining -= 1
                elif result.materially_changed:
                    counters["ideas_updated"] += 1
                else:
                    counters["ideas_skipped"] += 1
        counters["ai_estimated_cost_usd"] = round(float(counters["ai_estimated_cost_usd"]), 8)
        return counters

    async def sync_paper(self) -> dict[str, int]:
        return await self.paper.sync_all() if self.paper is not None else {"open": 0, "closed": 0}

    async def scan(self) -> dict[str, int]:
        """Compatibility orchestration for one-off runs; scheduler uses independent jobs."""
        ingestion_result = await self.ingest()
        tracking_result = await self.track_lifecycle()
        idea_result = await self.scan_ideas()
        paper_result = await self.sync_paper()
        return {
            "candles": ingestion_result["candles"],
            "ingestion_errors": ingestion_result["errors"],
            "tracked_candles": tracking_result["evaluated"],
            "transitions": tracking_result["transitions"],
            **idea_result,
            "paper_open": paper_result["open"],
            "paper_closed": paper_result["closed"],
        }
