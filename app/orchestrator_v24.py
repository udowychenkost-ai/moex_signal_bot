from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import Enum
from statistics import median
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adversarial import AdversarialCheck, AdversarialInputs
from app.ai_analyst import AIAnalystService, AIReviewResult
from app.calibration import CalibrationAssessment, CalibrationCohort, CalibrationService
from app.classification_v24 import ClassificationInputs, V24ClassificationService
from app.config import Settings
from app.data_integrity import DataIntegrityService, SourceObservation
from app.data_sla import (
    DataSLAConfig,
    DataSLAObservation,
    DataSLAService,
    MarketDataPoint,
)
from app.domain import AnalysisMode
from app.execution_v24 import (
    EntryPlanV24,
    PathToTargetInput,
    PriceObstacle,
    assess_path_to_target,
    build_entry_plan,
    reassess_execution,
)
from app.final_audit import AuditAssessment, FinalAuditService
from app.intraday_setup import D1_H1_NOT_ALIGNED_REASON
from app.intraday_v24 import (
    MARKET_REGIME_DIRECTION_BLOCKED_REASON,
    IntradayPipelineConfigV24,
    IntradayPipelineResultV24,
    IntradayPipelineV24,
)
from app.journal import (
    bind_v24_candidate_claim,
    claim_v24_candidate,
    create_idea_journal,
    create_model_trade,
)
from app.journal_health import JournalHealthService
from app.kill_switch import KillSwitchService
from app.liquidity_v24 import LiquidityModelConfig, LiquidityModelInput, LiquidityModelV24
from app.microstructure import CorporateActionFlags, MicrostructureGuard, MicrostructureInput
from app.models import (
    ActualTradeJournal,
    AIRequestLog,
    Candle,
    IdeaJournal,
    Instrument,
    MarketCandle,
    ModelTradeJournal,
    OrderBookLevel,
    TelegramUser,
)
from app.opportunity import OpportunityCandidate, OpportunityCostRanker, OpportunityCostWeights
from app.reporting_v24 import V24IdeaDisplayContext, V24OutboxService, format_v24_idea_card
from app.risk_v24 import (
    CostModel,
    CostModelConfig,
    PortfolioRiskState,
    PositionCapInputs,
    PositionRiskInput,
    RiskBudgetRepository,
    RiskEngineV24,
    assess_position_risk,
    recommend_position,
)
from app.v24_domain import (
    AdversarialResult,
    AuditGate,
    AuditGateResult,
    AuditStatus,
    CalculationReliability,
    CalibrationStatus,
    CostConfigurationStatus,
    DataSLAResult,
    DecisionAction,
    EventStateV24,
    ExecutionAction,
    FillStatus,
    FinalDecision,
    GateResult,
    JournalDirection,
    JournalStatus,
    KillSwitchState,
    MicrostructureStatus,
    OpportunityRankingMode,
    PathToTargetStatus,
    RiskBudgetStatus,
    SampleType,
    SetupLifecycleStatus,
    SetupType,
    SourceClass,
    StatisticalAdmissionStatus,
    TradingSessionState,
    V24Classification,
)

logger = logging.getLogger(__name__)


def _aware_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _jsonable(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return _aware_utc(value).isoformat()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def logical_candidate_key(
    *,
    strategy_version: str,
    ticker: str,
    direction: JournalDirection,
    setup: str,
    source_time: datetime,
) -> str:
    payload = "|".join(
        (
            strategy_version.strip(),
            ticker.strip().upper(),
            direction.value,
            setup.strip().upper(),
            _aware_utc(source_time).isoformat(timespec="seconds"),
        )
    )
    return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class V24ExternalContext:
    event_state: EventStateV24 = EventStateV24.DATA_NOT_AVAILABLE
    session_state: TradingSessionState = TradingSessionState.UNKNOWN
    corporate_actions: CorporateActionFlags | None = None
    event_context: dict[str, object] | None = None
    short_available: bool | None = None
    borrow_carry_pct: float | None = None
    tick_size: float | None = None
    lower_price_band: float | None = None
    upper_price_band: float | None = None


class V24ExternalContextProvider(Protocol):
    async def context_for(
        self, instrument: Instrument, *, as_of: datetime
    ) -> V24ExternalContext: ...


class UnavailableV24ExternalContextProvider:
    async def context_for(self, instrument: Instrument, *, as_of: datetime) -> V24ExternalContext:
        del instrument, as_of
        return V24ExternalContext()


@dataclass(frozen=True, slots=True)
class V24CandidateDecision:
    candidate_key: str
    signal_datetime: datetime
    ticker: str
    direction: JournalDirection
    pipeline: IntradayPipelineResultV24
    entry_plan: EntryPlanV24
    target: float | None
    gate_results: dict[AuditGate, AuditGateResult]
    gate_details: dict[str, object]
    audit: AuditAssessment
    final_decision: FinalDecision
    classification: V24Classification
    calibration: CalibrationAssessment
    ai_review: AIReviewResult | None
    journal_values: dict[str, object]
    snapshot_values: dict[str, object]
    model_values: dict[str, object]
    model_eligible: bool

    @property
    def production_qualified(self) -> bool:
        return self.audit.status is AuditStatus.PASS and self.classification in {
            V24Classification.PRODUCTION_QUALIFIED,
            V24Classification.STATISTICALLY_QUALIFIED_70,
        }


@dataclass(frozen=True, slots=True)
class V24PersistResult:
    created: bool
    duplicate: bool
    trade_id: str | None
    model_created: bool
    notifications_queued: int
    classification: str


@dataclass(frozen=True, slots=True)
class V24TickerDiagnostic:
    ticker: str
    outcome: str
    daily_direction: str | None = None
    hourly_direction: str | None = None
    market_regime: str | None = None
    setup_type: str | None = None
    setup_direction: str | None = None


def _ticker_diagnostic(
    ticker: str,
    outcome: str,
    pipeline: IntradayPipelineResultV24 | None = None,
) -> V24TickerDiagnostic:
    setup = pipeline.setup if pipeline is not None else None
    return V24TickerDiagnostic(
        ticker=ticker,
        outcome=outcome,
        daily_direction=(
            setup.daily_direction.value if setup is not None and setup.daily_direction else None
        ),
        hourly_direction=(
            setup.hourly_direction.value if setup is not None and setup.hourly_direction else None
        ),
        market_regime=pipeline.market.regime.value if pipeline is not None else None,
        setup_type=(
            setup.setup_type.value
            if setup is not None and setup.setup_type is not SetupType.UNKNOWN
            else None
        ),
        setup_direction=setup.direction.value if setup is not None and setup.direction else None,
    )


@dataclass(frozen=True, slots=True)
class V24EvaluationOutcome:
    decision: V24CandidateDecision | None
    diagnostics: tuple[str, ...]
    ticker_diagnostic: V24TickerDiagnostic


class IntradayV24Orchestrator:
    """One fail-closed application service for v2.4 analysis, journal, model and outbox."""

    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        ai_analyst: AIAnalystService | None = None,
        external_context: V24ExternalContextProvider | None = None,
        outbox: V24OutboxService | None = None,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.ai_analyst = ai_analyst
        self.external_context = external_context or UnavailableV24ExternalContextProvider()
        self.outbox = outbox or V24OutboxService(session_factory)
        self.pipeline = IntradayPipelineV24(
            IntradayPipelineConfigV24(
                strategy_version=settings.intraday_v24_strategy_version,
                maximum_holding_trading_days=settings.intraday_v24_max_holding_trading_days,
                leverage_enabled=settings.intraday_v24_leverage_enabled,
            )
        )
        self.data_integrity = DataIntegrityService()
        self.data_sla = DataSLAService(DataSLAConfig.from_settings(settings))
        self.microstructure = MicrostructureGuard(
            max_spread_pct=settings.microstructure_max_spread_pct,
            max_orderbook_age_seconds=settings.microstructure_orderbook_max_age_seconds,
        )
        self.liquidity = LiquidityModelV24(LiquidityModelConfig.from_settings(settings))
        self.costs = CostModel(
            CostModelConfig(
                broker_commission_pct=settings.intraday_v24_broker_commission_pct,
                exchange_fee_pct=settings.intraday_v24_exchange_fee_pct,
                entry_slippage_bps=settings.intraday_v24_entry_slippage_bps,
                exit_slippage_bps=settings.intraday_v24_exit_slippage_bps,
                stop_slippage_bps=settings.intraday_v24_stop_slippage_bps,
                short_carry_pct_per_day=settings.intraday_v24_short_carry_pct,
            )
        )
        self.risk_policies = RiskBudgetRepository()
        self.risk_engine = RiskEngineV24()
        self.calibration = CalibrationService(session_factory)
        self.adversarial = AdversarialCheck()
        self.audit = FinalAuditService()
        self.classifier = V24ClassificationService()
        self.journal_health = JournalHealthService(session_factory)
        self.kill_switch = KillSwitchService(session_factory)

    async def scan(self, *, as_of: datetime | None = None) -> dict[str, Any]:
        checked_at = _aware_utc(as_of or datetime.now(UTC))
        if not self.settings.intraday_v24_processing_enabled:
            return {"enabled": False, "status": "DISABLED", "errors": 0}
        async with self.session_factory() as session:
            instruments = list(
                await session.scalars(
                    select(Instrument)
                    .where(Instrument.is_active.is_(True), Instrument.echelon.in_((1, 2)))
                    .order_by(Instrument.secid)
                    .limit(self.settings.universe_size)
                )
            )
        counters: dict[str, Any] = {
            "enabled": True,
            "production_enabled": self.settings.intraday_v24_enabled,
            "shadow_enabled": self.settings.intraday_v24_shadow_enabled,
            "checked": len(instruments),
            "candidates": 0,
            "journaled": 0,
            "model_created": 0,
            "published": 0,
            "duplicates": 0,
            "rejected": 0,
            "missing_mtf": 0,
            "d1_h1_not_aligned": 0,
            "market_regime_direction_blocked": 0,
            "no_deterministic_setup": 0,
            "setup_detected": 0,
            "errors": 0,
            "rejection_reasons": {},
            "ticker_diagnostics": [],
        }
        ticker_diagnostics: list[dict[str, str | None]] = counters["ticker_diagnostics"]
        for instrument in instruments:
            diagnostic_index: int | None = None
            try:
                outcome = await self._evaluate_instrument(instrument, as_of=checked_at)
                for diagnostic in outcome.diagnostics:
                    counters[diagnostic] += 1
                diagnostic_index = len(ticker_diagnostics)
                ticker_diagnostics.append(asdict(outcome.ticker_diagnostic))
                decision = outcome.decision
                if decision is None:
                    continue
                counters["candidates"] += 1
                persisted = await self.persist_decision(decision)
                counters["journaled"] += int(persisted.created)
                counters["duplicates"] += int(persisted.duplicate)
                counters["model_created"] += int(persisted.model_created)
                counters["published"] += persisted.notifications_queued
                counters["rejected"] += int(not decision.production_qualified)
                for gate, result in decision.gate_results.items():
                    if result is AuditGateResult.FAIL:
                        reasons = counters["rejection_reasons"]
                        reasons[gate.value] = int(reasons.get(gate.value, 0)) + 1
            except Exception:
                counters["errors"] += 1
                error_diagnostic = _ticker_diagnostic(instrument.secid, "ERROR")
                if diagnostic_index is None:
                    ticker_diagnostics.append(asdict(error_diagnostic))
                else:
                    previous = ticker_diagnostics[diagnostic_index]
                    ticker_diagnostics[diagnostic_index] = {
                        **previous,
                        "outcome": error_diagnostic.outcome,
                    }
                logger.exception("v24 candidate failed ticker=%s", instrument.secid)
        logger.info(
            "v24 scan completed checked=%s candidates=%s journaled=%s model=%s "
            "published=%s duplicates=%s rejected=%s missing_mtf=%s "
            "d1_h1_not_aligned=%s market_regime_direction_blocked=%s "
            "no_deterministic_setup=%s setup_detected=%s errors=%s",
            counters["checked"],
            counters["candidates"],
            counters["journaled"],
            counters["model_created"],
            counters["published"],
            counters["duplicates"],
            counters["rejected"],
            counters["missing_mtf"],
            counters["d1_h1_not_aligned"],
            counters["market_regime_direction_blocked"],
            counters["no_deterministic_setup"],
            counters["setup_detected"],
            counters["errors"],
        )
        return counters

    async def _load_market_data(
        self, ticker: str
    ) -> tuple[dict[str, list[Candle]], dict[str, list[MarketCandle]], list[OrderBookLevel]]:
        async with self.session_factory() as session:
            candle_sets = {
                timeframe: list(
                    reversed(
                        list(
                            await session.scalars(
                                select(Candle)
                                .where(Candle.secid == ticker, Candle.timeframe == timeframe)
                                .order_by(Candle.begin.desc())
                                .limit(500)
                            )
                        )
                    )
                )
                for timeframe in self.settings.intraday_v24_timeframe_list
            }
            benchmark_sets = {
                timeframe: list(
                    reversed(
                        list(
                            await session.scalars(
                                select(MarketCandle)
                                .where(
                                    MarketCandle.symbol == self.settings.market_benchmark,
                                    MarketCandle.timeframe == timeframe,
                                )
                                .order_by(MarketCandle.begin.desc())
                                .limit(500)
                            )
                        )
                    )
                )
                for timeframe in self.settings.intraday_v24_timeframe_list
            }
            latest_book = await session.scalar(
                select(func.max(OrderBookLevel.snapshot_at)).where(OrderBookLevel.secid == ticker)
            )
            book = (
                list(
                    await session.scalars(
                        select(OrderBookLevel).where(
                            OrderBookLevel.secid == ticker,
                            OrderBookLevel.snapshot_at == latest_book,
                        )
                    )
                )
                if latest_book is not None
                else []
            )
        return candle_sets, benchmark_sets, book

    @staticmethod
    def _target(pipeline: IntradayPipelineResultV24) -> float | None:
        direction = pipeline.setup.direction
        if direction is None:
            return None
        snapshot = pipeline.snapshots["5m"]
        price = snapshot.current_price
        if direction is JournalDirection.LONG:
            levels = [value for value in snapshot.resistance_levels if value > price]
            return min(levels, default=None)
        levels = [value for value in snapshot.support_levels if value < price]
        return max(levels, default=None)

    async def _portfolio_state(self) -> PortfolioRiskState | None:
        async with self.session_factory() as session:
            actual_count = int(
                await session.scalar(select(func.count()).select_from(ActualTradeJournal)) or 0
            )
            filled_model_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(ModelTradeJournal)
                    .where(ModelTradeJournal.model_fill_status == FillStatus.FILLED.value)
                )
                or 0
            )
        if actual_count or filled_model_count:
            return None
        return PortfolioRiskState(
            daily_loss_rub=0.0,
            portfolio_heat_rub=0.0,
            sector_heat_rub=0.0,
            factor_heat_rub={},
            allocated_capital_rub=0.0,
        )

    def _opportunity_weights(self) -> OpportunityCostWeights | None:
        raw = (
            self.settings.intraday_v24_opportunity_weight_structural,
            self.settings.intraday_v24_opportunity_weight_risk,
            self.settings.intraday_v24_opportunity_weight_capital,
            self.settings.intraday_v24_opportunity_weight_holding,
            self.settings.intraday_v24_opportunity_weight_liquidity,
            self.settings.intraday_v24_opportunity_weight_factor,
            self.settings.intraday_v24_opportunity_weight_event,
            self.settings.intraday_v24_opportunity_weight_overnight,
        )
        if any(value is None for value in raw):
            return None
        return OpportunityCostWeights(*(float(value) for value in raw if value is not None))

    async def evaluate_instrument(
        self, instrument: Instrument, *, as_of: datetime
    ) -> V24CandidateDecision | None:
        outcome = await self._evaluate_instrument(instrument, as_of=as_of)
        return outcome.decision

    async def _evaluate_instrument(
        self, instrument: Instrument, *, as_of: datetime
    ) -> V24EvaluationOutcome:
        candles, benchmark, book = await self._load_market_data(instrument.secid)
        required = {"1d", "1h", "15m", "5m"}
        if any(not candles.get(item) or not benchmark.get(item) for item in required):
            return V24EvaluationOutcome(
                decision=None,
                diagnostics=("missing_mtf",),
                ticker_diagnostic=_ticker_diagnostic(instrument.secid, "MISSING_MTF"),
            )
        context = await self.external_context.context_for(instrument, as_of=as_of)
        pipeline = self.pipeline.analyze(
            echelon=instrument.echelon,
            candles_by_timeframe=candles,
            benchmark_by_timeframe=benchmark,
            event_state=context.event_state,
            as_of=as_of,
            execution_1m_quality_pass=False,
        )
        if pipeline.setup.direction is None or pipeline.setup.setup_type is SetupType.UNKNOWN:
            diagnostic = (
                "d1_h1_not_aligned"
                if D1_H1_NOT_ALIGNED_REASON in pipeline.setup.evidence
                else "no_deterministic_setup"
            )
            outcome = (
                "D1_H1_NOT_ALIGNED"
                if diagnostic == "d1_h1_not_aligned"
                else "NO_DETERMINISTIC_SETUP"
            )
            return V24EvaluationOutcome(
                decision=None,
                diagnostics=(diagnostic,),
                ticker_diagnostic=_ticker_diagnostic(instrument.secid, outcome, pipeline),
            )
        diagnostics = ["setup_detected"]
        if any(
            reason.startswith(f"{MARKET_REGIME_DIRECTION_BLOCKED_REASON}:")
            for reason in pipeline.no_trade_reasons
        ):
            diagnostics.append("market_regime_direction_blocked")
            return V24EvaluationOutcome(
                decision=None,
                diagnostics=tuple(diagnostics),
                ticker_diagnostic=_ticker_diagnostic(
                    instrument.secid,
                    "MARKET_REGIME_DIRECTION_BLOCKED",
                    pipeline,
                ),
            )
        direction = pipeline.setup.direction
        five = pipeline.snapshots["5m"]
        source = candles["5m"][-1]
        best_bid = max(
            (row.price for row in book if row.side.upper().startswith("B")), default=None
        )
        best_ask = min(
            (row.price for row in book if row.side.upper().startswith("S")), default=None
        )
        book_time = max((_aware_utc(row.snapshot_at) for row in book), default=None)
        quote_observation = SourceObservation(
            source="MOEX_ISS_CANDLES",
            source_class=SourceClass.OFFICIAL_PUBLIC,
            available=True,
            source_timestamp=_aware_utc(source.end),
            fetched_at=_aware_utc(source.fetched_at) if source.fetched_at else None,
            fields={"close": source.close, "volume": source.volume},
            required_fields=("close", "volume"),
            critical=True,
            max_age_seconds=self.settings.max_latency_enter_now,
        )
        book_observation = SourceObservation(
            source="MOEX_ISS_ORDERBOOK",
            source_class=SourceClass.OFFICIAL_PUBLIC,
            available=bool(book),
            source_timestamp=book_time,
            fetched_at=book_time,
            fields={"best_bid": best_bid, "best_ask": best_ask},
            required_fields=("best_bid", "best_ask"),
            critical=True,
            max_age_seconds=self.settings.microstructure_orderbook_max_age_seconds,
        )
        integrity = self.data_integrity.assess((quote_observation, book_observation), now=as_of)
        quote_point = MarketDataPoint(
            name="current_price",
            value=five.current_price,
            source="MOEX_ISS_CANDLES",
            source_class=SourceClass.OFFICIAL_PUBLIC,
            source_timestamp=_aware_utc(source.end),
            fetched_at=_aware_utc(source.fetched_at) if source.fetched_at else None,
        )
        sla = self.data_sla.evaluate(
            DecisionAction.ENTER_NOW,
            DataSLAObservation(
                current_price=quote_point,
                volume=MarketDataPoint(
                    name="volume",
                    value=float(source.volume),
                    source="MOEX_ISS_CANDLES",
                    source_class=SourceClass.OFFICIAL_PUBLIC,
                    source_timestamp=_aware_utc(source.end),
                    fetched_at=_aware_utc(source.fetched_at) if source.fetched_at else None,
                ),
                orderbook=MarketDataPoint(
                    name="orderbook",
                    value=(best_bid + best_ask) / 2 if best_bid and best_ask else None,
                    source="MOEX_ISS_ORDERBOOK" if book else None,
                    source_class=SourceClass.OFFICIAL_PUBLIC if book else None,
                    source_timestamp=book_time,
                    fetched_at=book_time,
                ),
            ),
            now=as_of,
        )
        micro = self.microstructure.evaluate(
            MicrostructureInput(
                direction=direction,
                session_state=context.session_state,
                current_price=five.current_price,
                tick_size=context.tick_size,
                lot_size=instrument.lot_size,
                best_bid=best_bid,
                best_ask=best_ask,
                orderbook_timestamp=book_time,
                orderbook_quality="PUBLIC_BEST_QUOTES_ONLY" if book else None,
                lower_price_band=context.lower_price_band,
                upper_price_band=context.upper_price_band,
                corporate_actions=context.corporate_actions,
                short_available=context.short_available,
                borrow_carry_pct=context.borrow_carry_pct,
            ),
            now=as_of,
        )
        daily_values = [float(item.value) for item in candles["1d"][-20:] if item.value > 0]
        book_depth = (
            sum(
                row.price * row.quantity
                for row in book
                if row.quantity is not None and row.quantity > 0
            )
            or None
        )
        liquidity = self.liquidity.assess(
            LiquidityModelInput(
                adv20_rub=sum(daily_values) / len(daily_values) if daily_values else None,
                median_adv_rub=median(daily_values) if daily_values else None,
                current_turnover_rub=float(source.value) if source.value >= 0 else None,
                expected_remaining_turnover_rub=None,
                rvol=five.rvol,
                spread_pct=micro.spread_pct,
                number_of_trades=None,
                orderbook_depth_rub=book_depth,
                orderbook_depth_is_full=False,
                expected_slippage_bps=None,
                market_impact_bps=None,
                remaining_session_hours=None,
            )
        )
        entry_plan = build_entry_plan(
            direction=direction,
            current_price=five.current_price,
            atr=five.atr,
            preferred_reference=five.session_vwap or five.current_price,
            invalidation=pipeline.setup.invalidation,
        )
        target = self._target(pipeline)
        path = (
            assess_path_to_target(
                PathToTargetInput(
                    direction=direction,
                    entry=entry_plan.optimal_entry,
                    target=target,
                    vwap=five.session_vwap,
                    support_levels=five.support_levels,
                    resistance_levels=five.resistance_levels,
                    previous_day_high=five.previous_day_high,
                    previous_day_low=five.previous_day_low,
                    gap_levels=(),
                    volume_nodes=None,
                    liquidity_obstacles=(PriceObstacle("UNRELIABLE_LIQUIDITY", target, True),)
                    if liquidity.reliability is not CalculationReliability.RELIABLE
                    else (),
                    opposing_structure=None,
                )
            )
            if entry_plan.optimal_entry is not None and target is not None
            else None
        )
        cost = self.costs.estimate(
            direction=direction,
            position_amount_rub=1.0,
            holding_days=float(self.settings.intraday_v24_max_holding_trading_days),
        )
        execution = (
            reassess_execution(
                plan=entry_plan,
                current_price=five.current_price,
                target=target,
                path=path,
                cost_estimate=cost,
                position_amount_rub=1.0,
                assessed_at=as_of,
            )
            if path is not None
            else None
        )
        async with self.session_factory() as session:
            policy = await self.risk_policies.effective_policy(session, as_of=as_of)
        portfolio = await self._portfolio_state()
        position_risk = None
        risk = None
        recommendation = None
        if (
            policy is not None
            and policy.working_capital_rub is not None
            and entry_plan.optimal_entry is not None
            and entry_plan.initial_stop is not None
        ):
            position_risk = assess_position_risk(
                PositionRiskInput(
                    direction=direction,
                    capital_rub=policy.working_capital_rub,
                    position_amount_rub=1.0,
                    entry=entry_plan.optimal_entry,
                    stop=entry_plan.initial_stop,
                    cost_estimate=cost,
                )
            )
            if portfolio is not None:
                safe_portfolio = PortfolioRiskState(
                    daily_loss_rub=portfolio.daily_loss_rub,
                    portfolio_heat_rub=portfolio.portfolio_heat_rub,
                    sector_heat_rub=portfolio.sector_heat_rub,
                    factor_heat_rub={instrument.sector: 0.0},
                    allocated_capital_rub=portfolio.allocated_capital_rub,
                )
                risk = self.risk_engine.evaluate_budget(
                    policy=policy,
                    position_risk=position_risk,
                    portfolio=safe_portfolio,
                    shared_factors=(instrument.sector,),
                )
                recommendation = recommend_position(
                    PositionCapInputs(
                        liquidity_cap_rub=liquidity.liquidity_cap_rub,
                        risk_cap_rub=risk.risk_cap_rub,
                        available_capital_cap_rub=risk.available_capital_cap_rub,
                        portfolio_cap_rub=risk.portfolio_cap_rub,
                        correlation_cap_rub=risk.correlation_cap_rub,
                        price=five.current_price,
                        lot_size=instrument.lot_size,
                    )
                )
        rr = execution.gross_rr if execution is not None else None
        cohort = CalibrationCohort(
            setup=pipeline.setup.setup_type.value,
            direction=direction.value,
            market_regime=pipeline.market.regime.value,
            trend=five.structure_state.value,
            volatility=pipeline.market.volatility.value,
            time_of_day=f"H{as_of.hour:02d}",
            rr_bucket=(f"R{math.floor(rr)}" if rr is not None and rr >= 0 else "UNKNOWN"),
            liquidity_state=liquidity.reliability.value,
            context=context.event_state.value,
        )
        calibration = await self.calibration.assess(
            strategy_version=self.settings.intraday_v24_strategy_version,
            cohort=cohort,
            as_of=as_of,
        )
        opportunity_mode = OpportunityRankingMode.NOT_RELIABLY_CALCULABLE
        opportunity_score: float | None = None
        if position_risk is not None and recommendation is not None:
            candidate = OpportunityCandidate(
                candidate_id=instrument.secid,
                structural_quality=min(100.0, len(pipeline.setup.evidence) * 10.0),
                risk_rub=position_risk.net_stress_loss_rub or position_risk.gross_loss_to_stop_rub,
                capital_rub=recommendation.recommended_position_rub or 1.0,
                expected_holding_hours=16.0,
                liquidity_score=(
                    1.0 if liquidity.reliability is CalculationReliability.RELIABLE else 0.0
                ),
                factor_risk_score=0.0 if portfolio is not None else 1.0,
                event_risk_score=0.0 if context.event_context is not None else 1.0,
                overnight_risk_score=1.0,
                statistically_calibrated=(
                    calibration.calibration_status is CalibrationStatus.CALIBRATED
                ),
                probability_tp_before_sl=calibration.reliable_probability,
                reward_r=rr,
                loss_r=1.0,
            )
            ranked = OpportunityCostRanker(self._opportunity_weights()).rank((candidate,))
            opportunity_mode = ranked[0].mode
            opportunity_score = ranked[0].score
        journal_health = await self.journal_health.check()
        preliminary: dict[AuditGate, AuditGateResult] = {
            AuditGate.DATA: (
                AuditGateResult.PASS
                if integrity.status is GateResult.PASS
                else AuditGateResult.FAIL
            ),
            AuditGate.DATA_SLA: (
                AuditGateResult.PASS if sla.result is DataSLAResult.PASS else AuditGateResult.FAIL
            ),
            AuditGate.MICROSTRUCTURE: (
                AuditGateResult.PASS
                if micro.status in {MicrostructureStatus.PASS, MicrostructureStatus.WARN}
                else AuditGateResult.FAIL
            ),
            AuditGate.MARKET_TREND: (
                AuditGateResult.FAIL if pipeline.no_trade else AuditGateResult.PASS
            ),
            AuditGate.SETUP: AuditGateResult.PASS,
            AuditGate.FUNDAMENTAL_NEWS: (
                AuditGateResult.PASS
                if context.event_context is not None
                and context.event_state is not EventStateV24.DATA_NOT_AVAILABLE
                else AuditGateResult.FAIL
            ),
            AuditGate.LIQUIDITY: (
                AuditGateResult.PASS
                if liquidity.reliability is CalculationReliability.RELIABLE
                and liquidity.liquidity_cap_rub is not None
                and liquidity.liquidity_cap_rub > 0
                else AuditGateResult.FAIL
            ),
            AuditGate.ENTRY_STOP_TP: (
                AuditGateResult.PASS
                if entry_plan.reliability is CalculationReliability.RELIABLE and target is not None
                else AuditGateResult.FAIL
            ),
            AuditGate.PATH_TO_TP: (
                AuditGateResult.PASS
                if path is not None
                and path.status in {PathToTargetStatus.CLEAN, PathToTargetStatus.ACCEPTABLE}
                else AuditGateResult.FAIL
            ),
            AuditGate.COSTS: (
                AuditGateResult.PASS
                if cost.status is CostConfigurationStatus.CONFIGURED
                else AuditGateResult.FAIL
            ),
            AuditGate.EXECUTION: (
                AuditGateResult.PASS
                if execution is not None and execution.action is ExecutionAction.ENTER
                else AuditGateResult.FAIL
            ),
            AuditGate.RISK_BUDGET: (
                AuditGateResult.PASS
                if risk is not None
                and risk.status is RiskBudgetStatus.CONFIGURED
                and risk.full_risk_pass
                else AuditGateResult.FAIL
            ),
            AuditGate.PORTFOLIO_CORRELATION: (
                AuditGateResult.PASS
                if portfolio is not None and risk is not None
                else AuditGateResult.FAIL
            ),
            AuditGate.OPPORTUNITY_COST: (
                AuditGateResult.PASS
                if opportunity_mode is not OpportunityRankingMode.NOT_RELIABLY_CALCULABLE
                else AuditGateResult.FAIL
            ),
            AuditGate.JOURNAL: (
                AuditGateResult.PASS if journal_health.available else AuditGateResult.FAIL
            ),
            AuditGate.CALIBRATION: (
                AuditGateResult.PASS
                if calibration.calibration_status is CalibrationStatus.CALIBRATED
                and calibration.statistical_admission is StatisticalAdmissionStatus.PASS
                else AuditGateResult.NOT_REQUIRED
            ),
            AuditGate.REPRODUCIBILITY: (
                AuditGateResult.PASS
                if source.fetched_at is not None
                and self.settings.data_sla_version
                and self.settings.intraday_v24_cost_model_version
                else AuditGateResult.FAIL
            ),
            AuditGate.STRATEGY_VERSION: (
                AuditGateResult.PASS
                if pipeline.strategy_version == self.settings.intraday_v24_strategy_version
                else AuditGateResult.FAIL
            ),
            AuditGate.ADVERSARIAL: AuditGateResult.FAIL,
        }
        ai_review = None
        non_ai_failures = [
            gate
            for gate, result in preliminary.items()
            if gate not in {AuditGate.ADVERSARIAL, AuditGate.CALIBRATION}
            and result is AuditGateResult.FAIL
        ]
        ai_result = None
        if not non_ai_failures and self.ai_analyst is not None:
            ai_review = await self.ai_analyst.review_verified_snapshot(
                {
                    "ticker": instrument.secid,
                    "direction": direction.value,
                    "setup": pipeline.setup.setup_type.value,
                    "market_regime": pipeline.market.regime.value,
                    "entry": entry_plan.optimal_entry,
                    "stop": entry_plan.initial_stop,
                    "target": target,
                    "gross_rr": rr,
                    "data_sla": sla.result.value,
                    "liquidity_reliability": liquidity.reliability.value,
                    "deterministic_gates": {
                        gate.value: result.value for gate, result in preliminary.items()
                    },
                }
            )
            if ai_review.status != "OK" or ai_review.analysis.verdict == "WAIT":
                ai_result = AdversarialResult.WAIT
            elif ai_review.analysis.verdict == "REJECT":
                ai_result = AdversarialResult.FAIL
            else:
                ai_result = AdversarialResult.PASS
        adversarial = self.adversarial.evaluate(
            AdversarialInputs(
                trend_wrong=preliminary[AuditGate.MARKET_TREND] is AuditGateResult.FAIL,
                news_priced_in=None if context.event_context is None else False,
                entry_late=(execution is not None and execution.action is ExecutionAction.NO_CHASE),
                false_breakout=None,
                stop_weak=entry_plan.reliability is not CalculationReliability.RELIABLE,
                tp_unrealistic=target is None,
                path_blocked=preliminary[AuditGate.PATH_TO_TP] is AuditGateResult.FAIL,
                volume_weak=five.volume_confirmed is False,
                market_against=preliminary[AuditGate.MARKET_TREND] is AuditGateResult.FAIL,
                sector_against=None,
                liquidity_weak=preliminary[AuditGate.LIQUIDITY] is AuditGateResult.FAIL,
                probability_uncalibrated=(
                    calibration.calibration_status is not CalibrationStatus.CALIBRATED
                ),
                execution_poor=preliminary[AuditGate.EXECUTION] is AuditGateResult.FAIL,
                opportunity_cost_high=(
                    preliminary[AuditGate.OPPORTUNITY_COST] is AuditGateResult.FAIL
                ),
                corporate_action_risk=None if context.corporate_actions is None else False,
                microstructure_risk=(preliminary[AuditGate.MICROSTRUCTURE] is AuditGateResult.FAIL),
                journal_gap=preliminary[AuditGate.JOURNAL] is AuditGateResult.FAIL,
                risk_budget_missing=preliminary[AuditGate.RISK_BUDGET] is AuditGateResult.FAIL,
                created_only_for_activity=False,
            ),
            ai_result=ai_result,
            ai_reasons=((ai_review.analysis.short_summary,) if ai_review is not None else ()),
        )
        preliminary[AuditGate.ADVERSARIAL] = (
            AuditGateResult.PASS
            if adversarial.result is AdversarialResult.PASS
            else AuditGateResult.FAIL
        )
        audit = self.audit.evaluate(preliminary)
        final_decision = (
            FinalDecision.NO_TRADE
            if pipeline.no_trade or adversarial.result is AdversarialResult.FAIL
            else FinalDecision.ENTER_NOW
            if audit.status is AuditStatus.PASS
            else FinalDecision.WAIT
        )
        classification = self.classifier.classify(
            ClassificationInputs(
                final_decision=final_decision,
                audit=audit,
                adversarial=adversarial,
                calibration=calibration,
                setup_strong=bool(pipeline.setup.evidence),
                shadow_requested=self.settings.intraday_v24_shadow_enabled,
                model_candidate=entry_plan.optimal_entry is not None and target is not None,
            )
        ).classification
        candidate_key = logical_candidate_key(
            strategy_version=pipeline.strategy_version,
            ticker=instrument.secid,
            direction=direction,
            setup=pipeline.setup.setup_type.value,
            source_time=five.as_of,
        )
        gate_details = {
            "data_integrity": asdict(integrity),
            "data_sla": asdict(sla),
            "microstructure": asdict(micro),
            "liquidity": asdict(liquidity),
            "entry_plan": asdict(entry_plan),
            "path": asdict(path) if path is not None else None,
            "costs": asdict(cost),
            "risk": asdict(risk) if risk is not None else None,
            "position": asdict(recommendation) if recommendation is not None else None,
            "calibration": asdict(calibration),
            "adversarial": asdict(adversarial),
            "final_audit": asdict(audit),
            "ai": (
                {
                    "status": ai_review.status,
                    "provider": ai_review.provider,
                    "model": ai_review.model,
                    "verdict": ai_review.analysis.verdict,
                }
                if ai_review is not None
                else {"status": "NOT_APPLICABLE_AFTER_HARD_GATES"}
            ),
        }
        journal_values = {
            "setup": pipeline.setup.setup_type.value,
            "market_regime": pipeline.market.regime.value,
            "market_bias": pipeline.market.bias.value,
            "data_confidence": integrity.confidence.value,
            "data_sla_status": sla.status.value,
            "data_sla_result": sla.result.value,
            "price_as_of": five.as_of,
            "data_delay_seconds": (_aware_utc(as_of) - _aware_utc(source.end)).total_seconds(),
            "source_set": list(integrity.available_sources),
            "news_context": context.event_context,
            "setup_quality": min(100.0, len(pipeline.setup.evidence) * 10.0),
            "execution_quality": execution.execution_quality if execution else None,
            "optimal_entry": entry_plan.optimal_entry,
            "acceptable_entry": entry_plan.acceptable_entry,
            "no_chase_level": entry_plan.no_chase_level,
            "initial_stop": entry_plan.initial_stop,
            "tp1": target,
            "path_to_tp_status": path.status.value if path else PathToTargetStatus.UNKNOWN.value,
            "gross_rr": rr,
            "expected_costs": cost.total_expected_costs_rub,
            "liquidity_cap": liquidity.liquidity_cap_rub,
            "normal_exit_cap": liquidity.normal_exit_cap_rub,
            "fast_exit_cap": liquidity.fast_exit_cap_rub,
            "stress_exit_cap": liquidity.stress_exit_cap_rub,
            "risk_cap": risk.risk_cap_rub if risk else None,
            "max_safe_position": recommendation.recommended_position_rub
            if recommendation
            else None,
            "recommended_position": (
                recommendation.recommended_position_rub if recommendation else None
            ),
            "risk_to_stop_rub": position_risk.net_stress_loss_rub if position_risk else None,
            "risk_to_stop_pct_capital": (
                position_risk.net_stress_loss_pct_capital if position_risk else None
            ),
            "probability_status": calibration.probability_status.value,
            "stated_probability": calibration.reliable_probability,
            "calibration_group": cohort.key(),
            "statistical_admission_status": calibration.statistical_admission.value,
            "opportunity_cost": opportunity_score,
            "microstructure_status": micro.status.value,
            "risk_budget_status": risk.status.value
            if risk
            else RiskBudgetStatus.NOT_CONFIGURED.value,
            "journal_status": (
                JournalStatus.ACTIVE.value
                if audit.status is AuditStatus.PASS
                else JournalStatus.INCOMPLETE.value
            ),
            "audit_status": audit.status.value,
            "final_classification": classification.value,
            "final_decision": final_decision.value,
            "invalidation_reason": ", ".join(audit.reasons) or None,
            "reason_for_trade": "; ".join(pipeline.setup.evidence),
            "adversarial_result": adversarial.result.value,
        }
        snapshot_values = {
            "data_sla": asdict(sla),
            "sources": list(integrity.available_sources),
            "entry": asdict(entry_plan),
            "liquidity_inputs": asdict(liquidity),
            "risk_inputs": {
                "policy_version": policy.configuration_version if policy else None,
                "assessment": asdict(risk) if risk else None,
                "position": asdict(recommendation) if recommendation else None,
            },
            "event_context": context.event_context,
            "gate_results": gate_details,
            "evidence_status": (
                "AVAILABLE" if integrity.status is GateResult.PASS else "DATA_NOT_AVAILABLE"
            ),
        }
        model_values = {
            **cohort.model_values(),
            "model_order_type": "LIMIT",
            "initial_stop": entry_plan.initial_stop,
            "tp1": target,
            "model_position_rub": recommendation.recommended_position_rub
            if recommendation
            else None,
            "stated_probability": calibration.reliable_probability,
            "setup_quality_at_entry": journal_values["setup_quality"],
            "execution_quality_at_entry": journal_values["execution_quality"],
            "data_sla_at_entry": sla.result.value,
            "notes": "SHADOW" if not self.settings.intraday_v24_enabled else "FORWARD",
        }
        return V24EvaluationOutcome(
            decision=V24CandidateDecision(
                candidate_key=candidate_key,
                signal_datetime=five.as_of,
                ticker=instrument.secid,
                direction=direction,
                pipeline=pipeline,
                entry_plan=entry_plan,
                target=target,
                gate_results=preliminary,
                gate_details=gate_details,
                audit=audit,
                final_decision=final_decision,
                classification=classification,
                calibration=calibration,
                ai_review=ai_review,
                journal_values=journal_values,
                snapshot_values=snapshot_values,
                model_values=model_values,
                model_eligible=(
                    entry_plan.optimal_entry is not None
                    and entry_plan.initial_stop is not None
                    and target is not None
                    and classification
                    in {
                        V24Classification.PRODUCTION_QUALIFIED,
                        V24Classification.STATISTICALLY_QUALIFIED_70,
                        V24Classification.STRUCTURALLY_QUALIFIED,
                        V24Classification.SHADOW,
                        V24Classification.MODEL_CANDIDATE,
                    }
                ),
            ),
            diagnostics=tuple(diagnostics),
            ticker_diagnostic=_ticker_diagnostic(instrument.secid, "SETUP_DETECTED", pipeline),
        )

    async def persist_decision(self, decision: V24CandidateDecision) -> V24PersistResult:
        current_kill = await self.kill_switch.current()
        verified_audit = self.audit.evaluate(decision.gate_results)
        publication_allowed = bool(
            self.settings.intraday_v24_enabled
            and verified_audit.status is AuditStatus.PASS
            and decision.audit.status is AuditStatus.PASS
            and decision.classification
            in {
                V24Classification.PRODUCTION_QUALIFIED,
                V24Classification.STATISTICALLY_QUALIFIED_70,
            }
            and current_kill.state is KillSwitchState.NORMAL
            and current_kill.initialized
        )
        async with self.session_factory() as session, session.begin():
            claimed = await claim_v24_candidate(
                session,
                candidate_key=decision.candidate_key,
                strategy_version=self.settings.intraday_v24_strategy_version,
                ticker=decision.ticker,
                direction=decision.direction,
                source_time=decision.signal_datetime,
            )
            if not claimed:
                existing = await session.scalar(
                    select(IdeaJournal.trade_id).where(
                        IdeaJournal.candidate_key == decision.candidate_key
                    )
                )
                return V24PersistResult(
                    created=False,
                    duplicate=True,
                    trade_id=existing,
                    model_created=False,
                    notifications_queued=0,
                    classification=decision.classification.value,
                )
            policy_version = decision.snapshot_values.get("risk_inputs", {})
            if not isinstance(policy_version, dict):
                policy_version = {}
            idea, _ = await create_idea_journal(
                session,
                signal_datetime=decision.signal_datetime,
                ticker=decision.ticker,
                direction=decision.direction,
                strategy_version=self.settings.intraday_v24_strategy_version,
                candidate_key=decision.candidate_key,
                risk_policy_version=policy_version.get("policy_version"),
                data_sla_policy_version=self.settings.data_sla_version,
                cost_model_version=self.settings.intraday_v24_cost_model_version,
                calibration_model_version=self.settings.intraday_v24_calibration_model_version,
                journal_values=decision.journal_values,
                snapshot_values=_jsonable(decision.snapshot_values),
            )
            await bind_v24_candidate_claim(
                session,
                candidate_key=decision.candidate_key,
                trade_id=idea.trade_id,
            )
            model_created = False
            if decision.model_eligible:
                await create_model_trade(
                    session,
                    trade_id=idea.trade_id,
                    sample_type=SampleType.FORWARD,
                    fill_status=FillStatus.NOT_FILLED,
                    calibration_eligible=False,
                    values=decision.model_values,
                )
                model_created = True
            if decision.ai_review is not None:
                for attempt in decision.ai_review.attempts:
                    session.add(
                        AIRequestLog(
                            request_kind="INTRADAY_V24",
                            provider=attempt.provider,
                            model=attempt.model,
                            status=attempt.status,
                            input_tokens=attempt.input_tokens,
                            output_tokens=attempt.output_tokens,
                            estimated_cost_usd=attempt.estimated_cost_usd,
                            latency_ms=attempt.latency_ms,
                            error=attempt.error[:2_000],
                            fallback_used=attempt.fallback_used,
                            usage_json=json.dumps(
                                attempt.usage or {}, ensure_ascii=False, sort_keys=True
                            ),
                        )
                    )
            queued = 0
            if publication_allowed:
                users = list(
                    await session.scalars(
                        select(TelegramUser).where(
                            TelegramUser.is_active.is_(True),
                            TelegramUser.analysis_mode.in_(
                                (AnalysisMode.INTRADAY_V24_ONLY.value, AnalysisMode.BOTH.value)
                            ),
                            TelegramUser.notify_new_idea.is_(True),
                        )
                    )
                )
                payload = "⚡ <b>Intraday</b>\n" + format_v24_idea_card(
                    idea,
                    V24IdeaDisplayContext(
                        probability_status=decision.calibration.probability_status,
                        calibrated_probability=decision.calibration.reliable_probability,
                        calibration_status=decision.calibration.calibration_status,
                        setup_status=SetupLifecycleStatus.ACTIVE,
                    ),
                    compact=True,
                )
                for user in users:
                    queued += int(
                        await self.outbox.enqueue_in_session(
                            session,
                            telegram_id=user.telegram_id,
                            notification_key=f"idea:{idea.trade_id}:created",
                            notification_type="NEW_V24_IDEA",
                            payload=payload,
                            available_at=datetime.now(UTC),
                            trade_id=idea.trade_id,
                        )
                    )
        return V24PersistResult(
            created=True,
            duplicate=False,
            trade_id=idea.trade_id,
            model_created=model_created,
            notifications_queued=queued,
            classification=decision.classification.value,
        )
