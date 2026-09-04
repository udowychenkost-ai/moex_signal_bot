from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from html import escape
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai_ux import format_ai_idea_summary, format_historical_ai_analysis
from app.config import Settings
from app.domain import AnalysisMode, IdeaHorizon, IdeaStatus
from app.idea_repository import OPEN_IDEA_STATUSES
from app.liquidity import LiquidityAssessment, LiquidityService
from app.liquidity_ux import format_liquidity_compact
from app.market_overview import MarketOverviewService, format_market_overview
from app.models import (
    ForwardNotification,
    IdeaFollow,
    PaperTrade,
    TelegramUser,
    TradingIdea,
    TradingIdeaEvent,
    WatchlistItem,
)
from app.observation import aware_utc
from app.operations import (
    ApplicationStatus,
    IdeaHistory,
    OperationalService,
    PeriodStatistics,
    V24OpenIdea,
    V24PeriodStatistics,
    realized_r,
)
from app.reporting import HORIZON_LABELS
from app.reporting_v24 import (
    DailyJournalServiceV24,
    V24OutboxService,
    format_daily_journal_summary,
)
from app.telegram_ui import idea_context_keyboard, lifecycle_context_keyboard

logger = logging.getLogger(__name__)

NOTIFIABLE_EVENT_TYPES = {
    "CREATED",
    "ACTIVATED",
    "TP_HIT",
    "SL_HIT",
    "EXPIRED",
    "ENTRY_MISSED",
    "DIRECTION_REVERSED",
    "CANCELLED",
    "INVALIDATED",
}

EVENT_TITLES = {
    IdeaStatus.ACTIVE.value: "IDEA ACTIVATED",
    IdeaStatus.TP_HIT.value: "TP HIT",
    IdeaStatus.SL_HIT.value: "SL HIT",
    IdeaStatus.EXPIRED.value: "EXPIRED",
    IdeaStatus.INVALIDATED.value: "INVALIDATED",
    IdeaStatus.CANCELLED.value: "CANCELLED",
}


async def _send_bot_message(
    bot: Bot,
    chat_id: int,
    message: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    if reply_markup is None:
        await bot.send_message(chat_id, message)
        return
    try:
        await bot.send_message(chat_id, message, reply_markup=reply_markup)
    except TypeError as error:
        # Lightweight test/dummy bots may intentionally expose the old two-argument API.
        if "reply_markup" not in str(error):
            raise
        await bot.send_message(chat_id, message)


def _format_time(value: datetime | None, timezone: str) -> str:
    if value is None:
        return "нет"
    return aware_utc(value).astimezone(ZoneInfo(timezone)).strftime("%d.%m.%Y %H:%M:%S %Z")


def format_new_idea(
    idea: TradingIdea,
    *,
    timezone: str,
    liquidity: LiquidityAssessment | None = None,
) -> str:
    rationale = "\n".join(f"• {escape(line)}" for line in idea.rationale.splitlines()[:3] if line)
    regime_icon = {"BULL": "🟢", "BEAR": "🔴", "SIDEWAYS": "🟡"}.get(idea.market_regime or "", "⚪")
    ai_block = format_ai_idea_summary(idea)
    liquidity_block = format_liquidity_compact(liquidity)
    return (
        "🔵 <b>Классический</b>\n🔥 <b>СИЛЬНАЯ ИДЕЯ</b>\n🆕 НОВАЯ ИДЕЯ\n\n"
        f"ID: <code>{idea.id}</code>\n"
        f"{'📈' if idea.direction == 'BUY' else '📉'} <b>{idea.direction} — "
        f"{escape(idea.ticker)}</b> · {escape(idea.instrument_name)}\n\n"
        f"💰 Текущая цена: <b>{idea.current_price:.2f} ₽</b>\n"
        f"🎯 Вход: <b>{idea.entry_price_from:.2f}–{idea.entry_price_to:.2f} ₽</b>\n"
        f"✅ TP: <b>{idea.take_profit:.2f} ₽</b>\n"
        f"🛑 SL: <b>{idea.stop_loss:.2f} ₽</b>\n\n"
        f"⏱ Горизонт: <b>{HORIZON_LABELS[idea.horizon]}</b> · "
        f"{idea.observation_mode}\n"
        f"R:R: <b>1:{idea.risk_reward_ratio:.2f}</b>\n"
        f"📊 Сила модели: <b>{(idea.final_quality_score or idea.confidence):.0f}/100</b>\n"
        f"Потенциал: <b>+{idea.expected_return_pct:.2f}%</b> · "
        f"риск <b>−{idea.risk_pct:.2f}%</b>\n\n"
        f"🌍 IMOEX: {regime_icon} <b>{idea.market_regime or 'нет данных'}</b> · "
        f"vol {idea.market_volatility or 'n/a'}\n"
        f"💪 Относительная сила: <b>{escape(idea.relative_strength_label or 'недоступно')}</b>\n"
        f"📦 Объём: <b>{idea.volume_state or 'UNKNOWN'}</b> "
        f"({float(idea.volume_score or 0):+.0f}/100)\n"
        f"📉 Momentum: <b>{float(idea.momentum_extreme_score or 0):+.0f}/100</b>\n"
        f"🏢 Фундаментал: <b>{escape(idea.fundamental_label or 'нет данных')}</b>\n\n"
        f"{liquidity_block}\n\n"
        f"{ai_block}\n\n"
        f"<b>Quant rationale</b>\n{rationale}\n\n"
        f"Статус: <b>{idea.status}</b> · {_format_time(idea.created_at, timezone)}\n\n"
        "⚠️ Только наблюдение. Реальные сделки не выполняются."
    )


def _closed_result(idea: TradingIdea, paper: PaperTrade | None) -> str:
    if idea.status not in {
        IdeaStatus.TP_HIT.value,
        IdeaStatus.SL_HIT.value,
        IdeaStatus.EXPIRED.value,
        IdeaStatus.INVALIDATED.value,
        IdeaStatus.CANCELLED.value,
    }:
        return ""
    if paper is not None and paper.status == "CLOSED":
        return (
            f"\nФактический PAPER результат: <b>{paper.net_pnl:+,.2f} ₽</b>"
            f" · <b>{paper.r_multiple:+.2f}R</b>"
        )
    value = realized_r(idea)
    if value is not None:
        return f"\nФактический RESEARCH результат: <b>{value:+.2f}R</b> (без paper P&L)"
    return "\nФактический результат: <b>вход не был активирован</b>"


def format_lifecycle_event(
    idea: TradingIdea,
    event: TradingIdeaEvent,
    paper: PaperTrade | None,
    *,
    timezone: str,
) -> str:
    title = EVENT_TITLES.get(event.to_status, event.event_type)
    price = f"\nЦена события: <b>{event.price:.2f} ₽</b>" if event.price is not None else ""
    return (
        f"🔵 <b>Классический</b>\n🔔 <b>{title}</b>\n\n"
        f"ID: <code>{idea.id}</code>\n"
        f"Ticker: <b>{escape(idea.ticker)}</b>\n"
        f"Horizon: <b>{HORIZON_LABELS[idea.horizon]}</b>\n"
        f"Текущий статус: <b>{event.to_status}</b>{price}\n"
        f"Причина: {escape(event.details or event.event_type)}"
        f"{_closed_result(idea, paper)}\n"
        f"Время: {_format_time(event.occurred_at, timezone)}\n\n"
        "⚠️ Только наблюдение. Торговое поручение не создавалось."
    )


def format_application_status(status: ApplicationStatus, *, timezone: str) -> str:
    freshness = status.freshness
    orderbook = status.orderbook
    stale_note = ""
    if freshness.stale_examples:
        examples = ", ".join(
            f"{item.ticker}/{item.timeframe}" for item in freshness.stale_examples[:5]
        )
        stale_note = f"\nУстаревшие: <b>{escape(examples)}</b>"
    jobs = []
    for state in status.job_states:
        marker = "✅" if state.success else ("❌" if state.success is False else "⏳")
        jobs.append(f"{marker} {escape(state.job_name)}")
    jobs_text = ", ".join(jobs) if jobs else "ещё не запускались"
    base = (
        "🩺 <b>LIVE OBSERVATION STATUS</b>\n\n"
        f"App version: <b>{escape(status.app_version)}</b>\n"
        f"Git commit: <code>{escape(status.git_commit)}</code>\n"
        f"Database: <b>{'OK' if status.database_ok else 'ERROR'}</b>\n"
        f"Latest MOEX update: <b>{_format_time(freshness.latest_moex_update, timezone)}</b>\n"
        f"Data freshness: <b>{freshness.fresh}/{freshness.checked} fresh</b>"
        f"{stale_note}\n"
        f"Order Book: <b>{'ENABLED' if orderbook.enabled else 'DISABLED'}</b>\n"
        f"Order Book update: <b>{_format_time(orderbook.last_update, timezone)}</b>\n"
        f"Order Book fresh: <b>{orderbook.fresh_instruments}/"
        f"{orderbook.monitored_instruments}</b> · stale: <b>{orderbook.stale_instruments}</b>\n"
        f"Order Book last job: <b>{escape(orderbook.last_job)}</b>\n"
        f"Latest scan: <b>{_format_time(status.latest_scan_time, timezone)}</b>\n"
        f"Next scan: <b>{_format_time(status.next_scan_time, timezone)}</b>\n"
        f"Active ideas: <b>{status.active_ideas}</b>\n"
        f"Pending ideas: <b>{status.pending_ideas}</b>\n"
        f"Ideas closed today: <b>{status.ideas_closed_today}</b>\n"
        f"Scheduler: <b>{'RUNNING' if status.scheduler_running else 'STOPPED'}</b>\n"
        f"Jobs: {jobs_text}"
    )
    if status.v24 is None:
        return base
    v24 = status.v24
    kill_reasons = ", ".join(item.value for item in v24.kill_switch.reasons) or "нет"
    liquidity_config = "CONFIGURED" if v24.liquidity_configured else "NOT CONFIGURED"
    cost_config = "CONFIGURED" if v24.cost_configured else "NOT CONFIGURED"
    return (
        base
        + "\n\n<b>INTRADAY V2.4</b>\n"
        + f"Legacy strategy: <b>{'ENABLED' if v24.legacy_enabled else 'DISABLED'}</b>\n"
        + f"Intraday engine: <b>{'ENABLED' if v24.enabled else 'DISABLED'}</b>\n"
        + f"Intraday shadow: <b>{'ENABLED' if v24.shadow_enabled else 'DISABLED'}</b>\n"
        + "Production notifications: <b>"
        + ("ENABLED" if v24.production_notification_enabled else "DISABLED")
        + "</b>\n"
        + f"Final Audit readiness: <b>{'READY' if v24.final_audit_ready else 'NOT READY'}</b>\n"
        + "External context: <b>"
        + ("AVAILABLE" if v24.external_context_ready else "DATA NOT AVAILABLE")
        + "</b>\n"
        + f"Alembic: <b>{escape(v24.journal.revision or 'UNKNOWN')}</b>\n"
        + f"Gemini: <b>{escape(v24.gemini)}</b>\n"
        + f"Journal: <b>{'AVAILABLE' if v24.journal.available else 'ERROR'}</b>\n"
        + f"Data SLA: <b>{v24.data_sla.value}</b>\n"
        + f"Risk Budget: <b>{v24.risk_budget.value}</b>\n"
        + f"Risk policy: <b>{escape(v24.risk_policy_version or 'NOT CONFIGURED')}</b>\n"
        + f"Liquidity config: <b>{liquidity_config}</b>\n"
        + f"Cost model: <b>{cost_config}</b>\n"
        + f"Statistical Admission: <b>{v24.statistical_admission.value}</b>\n"
        + f"Calibration: <b>{v24.calibration.value}</b>\n"
        + "Kill Switch: <b>"
        + (
            "INACTIVE (NORMAL)"
            if v24.kill_switch.state.value == "NORMAL"
            else f"ACTIVE ({v24.kill_switch.state.value})"
        )
        + "</b>\n"
        + f"Kill reasons: <b>{escape(kill_reasons)}</b>\n"
        + f"MODEL / ACTUAL: <b>{v24.model_trade_count} / {v24.actual_trade_count}</b>\n"
        + f"Ambiguous execution: <b>{v24.ambiguous_execution_count}</b>\n"
        + f"Uncalibrated signals: <b>{v24.uncalibrated_signal_count}</b>\n"
        + f"Data availability failures: <b>{v24.data_availability_failure_count}</b>\n"
        + f"Journal writes / errors: <b>{v24.journal_write_count} / "
        + f"{v24.journal_error_count}</b>\n"
        + "Microstructure: <b>"
        + escape(json.dumps(v24.microstructure_distribution, sort_keys=True))
        + "</b>\n"
        + f"Audit failures: <b>{escape(json.dumps(v24.audit_failure_reasons, sort_keys=True))}</b>"
        + "\nLatest V2.4 scan: <b>"
        + f"{_format_time(v24.latest_scan_at, timezone)} / {escape(v24.latest_scan_status)}</b>"
        + f" · candidates {v24.latest_scan_candidates} · errors {v24.latest_scan_errors}"
        + "\nV2.4 pre-candidate diagnostics: <b>"
        + f"missing MTF {v24.latest_scan_missing_mtf} · "
        + f"D1/H1 not aligned {v24.latest_scan_d1_h1_not_aligned} · "
        + f"setup detected {v24.latest_scan_setup_detected} · "
        + "market blocked "
        + f"{v24.latest_scan_market_regime_direction_blocked} · "
        + f"no deterministic setup {v24.latest_scan_no_deterministic_setup}</b>"
    )


def _metric(value: float | None, *, suffix: str = "") -> str:
    if value is None:
        return "n/a"
    if value == float("inf"):
        return "∞"
    return f"{value:.2f}{suffix}"


def format_statistics(periods: tuple[PeriodStatistics, ...]) -> str:
    sections = ["🔵 <b>Классический · FORWARD STATISTICS</b>"]
    for period in periods:
        sections.append(
            f"\n<b>{period.label}</b> · experiment <code>{escape(period.strategy_version)}</code>"
        )
        for row in period.horizons:
            sample = " ⚠️ малая выборка" if row.small_sample else ""
            paper_pnl = (
                f"{row.net_paper_pnl:+,.2f} ₽"
                if row.net_paper_pnl is not None
                else "n/a (RESEARCH)"
            )
            sections.append(
                f"\n<b>{HORIZON_LABELS[row.horizon]} · {row.mode}</b>{sample}\n"
                f"Generated: {row.generated} · Activated: {row.activated}\n"
                f"TP: {row.tp} · SL: {row.sl} · Expired: {row.expired}\n"
                f"Win rate: {_metric(row.win_rate, suffix='%')} · "
                f"PF: {_metric(row.profit_factor)}\n"
                f"Average R: {_metric(row.average_r)} · Net paper P&L: {paper_pnl}"
            )
        if period.experiment_cohorts:
            sections.append("\n<b>Quant vs AI cohorts</b>")
            for horizon in IdeaHorizon:
                cohort_rows = [
                    row for row in period.experiment_cohorts if row.horizon == horizon.value
                ]
                if not cohort_rows:
                    continue
                sections.append(f"<i>{HORIZON_LABELS[horizon.value]}</i>")
                for row in cohort_rows:
                    sample = " ⚠️ малая выборка" if row.small_sample else ""
                    sections.append(
                        f"{escape(row.cohort)}: {row.generated} generated · "
                        f"{row.activated} activated · TP {row.tp}/SL {row.sl} · "
                        f"WR {_metric(row.win_rate, suffix='%')} · "
                        f"PF {_metric(row.profit_factor)} · R {_metric(row.average_r)}{sample}"
                    )
    sections.append(
        "\n<i>Win rate = TP/(TP+SL). RESEARCH R не включает комиссии; "
        "PAPER P&L включает настроенные комиссии и проскальзывание.</i>"
    )
    return "\n".join(sections)


def format_open_ideas(ideas: list[TradingIdea]) -> str:
    if not ideas:
        return "Ожидающих и активных идей сейчас нет."
    lines = ["🔵 <b>Классический · текущие идеи</b>"]
    for idea in ideas:
        lines.append(
            f"<code>{idea.id}</code> · <b>{idea.direction} {escape(idea.ticker)}</b> · "
            f"{HORIZON_LABELS[idea.horizon]} · {idea.status} · {idea.confidence:.0f}%"
        )
    lines.append("\nПолная история: <code>/idea ID</code>")
    return "\n".join(lines)


def format_v24_statistics(periods: tuple[V24PeriodStatistics, ...]) -> str:
    lines = ["⚡ <b>Intraday V2.4 · отдельная статистика</b>"]
    for period in periods:
        lines.append(f"\n<b>{escape(period.label)}</b>")
        for label, metrics in (("MODEL", period.model), ("ACTUAL", period.actual)):
            sample = " ⚠️ малая выборка" if metrics.n_trades < 30 else ""
            win_rate = metrics.win_rate * 100 if metrics.win_rate is not None else None
            lines.append(
                f"<b>{label}</b>{sample}: closed {metrics.n_trades} · "
                f"WR {_metric(win_rate, suffix='%')} · "
                f"PF {_metric(metrics.profit_factor)} · "
                f"Avg R {_metric(metrics.average_r)} · "
                f"P&L {_metric(metrics.net_pl_rub, suffix=' ₽')}"
            )
    lines.append(
        "\n<i>MODEL и ACTUAL не объединяются. Calibration использует только "
        "calibration-eligible MODEL observations.</i>"
    )
    return "\n".join(lines)


def format_v24_open_ideas(rows: tuple[V24OpenIdea, ...]) -> str:
    if not rows:
        return "⚡ <b>Intraday</b>\nТекущих pending/active идей нет."
    lines = ["⚡ <b>Intraday · текущие идеи</b>"]
    for row in rows:
        lines.append(
            f"<code>{escape(row.idea.trade_id)}</code> · "
            f"<b>{escape(row.idea.direction)} {escape(row.idea.ticker)}</b> · "
            f"{escape(row.status)} · {escape(row.idea.final_classification or 'UNCLASSIFIED')}"
        )
    lines.append("\nПолная карточка: <code>/actual TRADE_ID</code>")
    return "\n".join(lines)


def format_strategy_conflicts(legacy: list[TradingIdea], v24: tuple[V24OpenIdea, ...]) -> str:
    legacy_by_ticker = {
        item.ticker: "LONG" if item.direction == "BUY" else "SHORT" for item in legacy
    }
    conflicts = [
        (item.idea.ticker, legacy_by_ticker[item.idea.ticker], item.idea.direction)
        for item in v24
        if item.idea.ticker in legacy_by_ticker
        and legacy_by_ticker[item.idea.ticker] != item.idea.direction
    ]
    if not conflicts:
        return ""
    lines = ["⚠️ <b>Стратегии дают разные направления</b>"]
    lines.extend(
        f"{escape(ticker)} · Классический: <b>{classic}</b> · Intraday: <b>{intraday}</b>"
        for ticker, classic, intraday in conflicts
    )
    return "\n".join(lines)


def format_idea_history(
    history: IdeaHistory,
    *,
    timezone: str,
    liquidity: LiquidityAssessment | None = None,
) -> str:
    idea = history.idea
    lines = [
        format_new_idea(idea, timezone=timezone, liquidity=liquidity),
        "\n<b>Lifecycle</b>",
    ]
    for event in history.events:
        price = f" @ {event.price:.2f}" if event.price is not None else ""
        lines.append(
            f"• {_format_time(event.occurred_at, timezone)} · "
            f"{escape(event.event_type)} → <b>{event.to_status}</b>{price}"
        )
    lines.append(_closed_result(idea, history.paper_trade))
    if history.snapshot is not None:
        factors = json.loads(history.snapshot.factor_scores)
        components = factors.get("technical_components", {})
        component_text = " · ".join(
            f"{escape(str(name))} {float(value):+.0f}" for name, value in components.items()
        )
        indicators = json.loads(history.snapshot.relevant_indicators)
        primary = indicators.get(idea.primary_timeframe, {})
        publications = json.loads(history.snapshot.fundamental_publications)
        publication_text = (
            "; ".join(
                f"{escape(str(item.get('report_period')))} available "
                f"{escape(str(item.get('available_from')))} · "
                f"{escape(str(item.get('source')))}"
                for item in publications
            )
            if publications
            else "нет point-in-time данных"
        )
        lines.append(
            "\n<b>Decision snapshot</b>\n"
            f"Technical: {history.snapshot.technical_score:+.2f} · "
            f"Fundamental: {history.snapshot.fundamental_score:+.2f} · "
            f"Total: {history.snapshot.total_score:+.2f} · "
            f"ATR: {_metric(history.snapshot.atr)}\n"
            f"IMOEX: {history.snapshot.regime or 'n/a'} · "
            f"vol {history.snapshot.market_volatility or 'n/a'} · "
            f"RS {history.snapshot.relative_strength_score:+.1f}\n"
            f"RSI: {_metric(primary.get('rsi'))} · "
            f"volume ratio: {_metric(primary.get('volume_ratio'))}\n"
            f"Components: {component_text or 'n/a'}\n"
            f"Fundamental publications: {publication_text}\n"
            "Snapshot immutable: да"
        )
    return "\n".join(line for line in lines if line)


def format_ai_analysis(history: IdeaHistory) -> str:
    return format_historical_ai_analysis(history.idea)


def format_technical_analysis(history: IdeaHistory) -> str:
    snapshot = history.snapshot
    if snapshot is None:
        return "📊 <b>Теханализ</b>\n\nDecision-time snapshot недоступен."
    factors = json.loads(snapshot.factor_scores or "{}")
    components = factors.get("technical_components", {})
    indicators = json.loads(snapshot.relevant_indicators or "{}")
    primary = indicators.get(history.idea.primary_timeframe, {})
    component_text = "\n".join(
        f"• {escape(str(name))}: {float(value):+.1f}/100" for name, value in components.items()
    )
    return (
        "📊 <b>Теханализ</b>\n\n"
        f"Technical score: <b>{snapshot.technical_score:+.1f}/100</b>\n"
        f"Подтверждений: <b>{snapshot.confirmation_count}</b>\n"
        f"RSI: <b>{_metric(primary.get('rsi'))}</b> · "
        f"ADX: <b>{_metric(primary.get('adx'))}</b>\n"
        f"MACD histogram: <b>{_metric(primary.get('macd_histogram'))}</b>\n"
        f"Volume ratio: <b>{_metric(primary.get('volume_ratio'))}</b> · "
        f"ATR: <b>{_metric(snapshot.atr)}</b>\n\n"
        f"<b>Компоненты</b>\n{component_text or 'нет данных'}"
    )


def format_fundamental_analysis(history: IdeaHistory) -> str:
    snapshot = history.snapshot
    if snapshot is None:
        return "🏢 <b>Фундаментал</b>\n\nDecision-time snapshot недоступен."
    components = json.loads(snapshot.fundamental_components or "{}")
    publications = json.loads(snapshot.fundamental_publications or "[]")
    component_text = "\n".join(
        f"• {escape(str(name))}: {float(value):+.1f}/100" for name, value in components.items()
    )
    sources = "\n".join(
        f"• {escape(str(item.get('report_period')))} · "
        f"{escape(str(item.get('source')))} · available {escape(str(item.get('available_from')))}"
        for item in publications
    )
    return (
        "🏢 <b>Фундаментал</b>\n\n"
        f"Score: <b>{snapshot.fundamental_score:+.1f}/100</b>\n"
        f"Статус: <b>{escape(history.idea.fundamental_label)}</b>\n\n"
        f"<b>Компоненты</b>\n{component_text or 'unavailable'}\n\n"
        f"<b>Point-in-time источники</b>\n{sources or 'unavailable'}"
    )


def format_idea_market_analysis(history: IdeaHistory) -> str:
    snapshot = history.snapshot
    if snapshot is None:
        return "🌍 <b>Рынок</b>\n\nDecision-time snapshot недоступен."
    return (
        "🌍 <b>Рынок в момент решения</b>\n\n"
        f"IMOEX regime: <b>{escape(snapshot.regime or 'unavailable')}</b> "
        f"({snapshot.market_regime_score:+.1f}/100)\n"
        f"Volatility: <b>{escape(snapshot.market_volatility or 'unavailable')}</b>\n"
        f"Relative strength: <b>{snapshot.relative_strength_score:+.1f}/100</b>\n"
        f"Volume score: <b>{snapshot.volume_score:+.1f}/100</b>\n\n"
        "Значения взяты из immutable decision-time snapshot."
    )


def format_lifecycle_history(history: IdeaHistory, *, timezone: str) -> str:
    lines = ["📜 <b>История идеи</b>"]
    for event in history.events:
        price = f" @ {event.price:.2f} ₽" if event.price is not None else ""
        lines.append(
            f"• {_format_time(event.occurred_at, timezone)} · "
            f"{escape(event.event_type)} → <b>{escape(event.to_status)}</b>{price}"
        )
    lines.append(_closed_result(history.idea, history.paper_trade))
    return "\n".join(item for item in lines if item)


class ForwardReportingService:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        operations: OperationalService,
        liquidity: LiquidityService | None = None,
        v24_daily: DailyJournalServiceV24 | None = None,
        v24_outbox: V24OutboxService | None = None,
        market_overview: MarketOverviewService | None = None,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.operations = operations
        self.liquidity = liquidity
        self.v24_daily = v24_daily
        self.v24_outbox = v24_outbox
        self.market_overview = market_overview
        self.started_at = datetime.now(UTC)

    async def _recipients(self) -> dict[int, TelegramUser | None]:
        async with self.session_factory() as session:
            users = list(
                await session.scalars(select(TelegramUser).where(TelegramUser.is_active.is_(True)))
            )
        recipients: dict[int, TelegramUser | None] = {user.telegram_id: user for user in users}
        for chat_id in self.settings.admin_chat_ids:
            recipients.setdefault(chat_id, None)
        return recipients

    async def _mark_sent(
        self,
        telegram_id: int,
        key: str,
        kind: str,
        *,
        idea_id: int | None = None,
        event_id: int | None = None,
        sent_at: datetime,
    ) -> None:
        async with self.session_factory() as session, session.begin():
            session.add(
                ForwardNotification(
                    telegram_id=telegram_id,
                    notification_key=key,
                    notification_type=kind,
                    idea_id=idea_id,
                    event_id=event_id,
                    sent_at=sent_at,
                )
            )

    async def _deactivate_if_registered(self, telegram_id: int) -> None:
        async with self.session_factory() as session, session.begin():
            await session.execute(
                update(TelegramUser)
                .where(TelegramUser.telegram_id == telegram_id)
                .values(is_active=False)
            )

    async def dispatch_notifications(
        self,
        bot: Bot,
        *,
        now: datetime | None = None,
    ) -> dict[str, int]:
        sent_at = now or datetime.now(UTC)
        recipients = await self._recipients()
        async with self.session_factory() as session:
            events = list(
                await session.scalars(
                    select(TradingIdeaEvent)
                    .where(TradingIdeaEvent.event_type.in_(NOTIFIABLE_EVENT_TYPES))
                    .order_by(TradingIdeaEvent.occurred_at, TradingIdeaEvent.id)
                )
            )
            ideas = {
                idea.id: idea
                for idea in await session.scalars(
                    select(TradingIdea).where(
                        TradingIdea.id.in_({event.idea_id for event in events})
                    )
                )
            }
            papers = {
                trade.idea_id: trade
                for trade in await session.scalars(
                    select(PaperTrade).where(PaperTrade.idea_id.in_(ideas))
                )
            }
        counters = {"recipients": len(recipients), "sent": 0, "errors": 0}
        liquidity_cache: dict[int, LiquidityAssessment | None] = {}
        for telegram_id, user in recipients.items():
            if user is not None and not AnalysisMode(user.analysis_mode).includes_legacy:
                continue
            async with self.session_factory() as session:
                sent_keys = set(
                    await session.scalars(
                        select(ForwardNotification.notification_key).where(
                            ForwardNotification.telegram_id == telegram_id
                        )
                    )
                )
                watched_since = {
                    item.secid: item.created_at
                    for item in await session.scalars(
                        select(WatchlistItem).where(WatchlistItem.telegram_id == telegram_id)
                    )
                }
                followed_since = {
                    item.idea_id: item.created_at
                    for item in await session.scalars(
                        select(IdeaFollow).where(IdeaFollow.telegram_id == telegram_id)
                    )
                }
            for event in events:
                key = f"event:{event.id}"
                if key in sent_keys:
                    continue
                recipient_cutoff = user.created_at if user is not None else self.started_at
                if aware_utc(event.occurred_at) < aware_utc(recipient_cutoff):
                    continue
                idea = ideas.get(event.idea_id)
                if idea is None:
                    continue
                event_at = aware_utc(event.occurred_at)
                watch_started_at = watched_since.get(idea.ticker)
                follow_started_at = followed_since.get(idea.id)
                contextual_subscription = bool(
                    user is not None
                    and (
                        (
                            user.notify_watchlist
                            and watch_started_at is not None
                            and event_at >= aware_utc(watch_started_at)
                        )
                        or (
                            event.event_type != "CREATED"
                            and follow_started_at is not None
                            and event_at >= aware_utc(follow_started_at)
                        )
                    )
                )
                if user is not None:
                    preference = {
                        "CREATED": user.notify_new_idea,
                        "ACTIVATED": user.notify_activation,
                        "TP_HIT": user.notify_tp,
                        "SL_HIT": user.notify_sl,
                        "EXPIRED": user.notify_expiry,
                        "ENTRY_MISSED": user.notify_expiry,
                        "INVALIDATED": user.notify_expiry,
                    }.get(event.event_type, True)
                    if not preference and not contextual_subscription:
                        continue
                    if (
                        user.ai_filter_enabled
                        and idea.strategy_version == self.settings.strategy_version
                        and idea.ai_verdict not in {"STRONG_APPROVE", "APPROVE"}
                    ):
                        continue
                    if (
                        not contextual_subscription
                        and user.idea_horizon != "all"
                        and user.idea_horizon != idea.horizon
                    ):
                        continue
                    strength = (
                        idea.final_quality_score
                        if idea.strategy_version == self.settings.strategy_version
                        else idea.confidence
                    )
                    if not contextual_subscription and strength < user.minimum_confidence:
                        continue
                assessment = None
                if event.event_type == "CREATED" and self.liquidity is not None:
                    if idea.id not in liquidity_cache:
                        try:
                            liquidity_cache[idea.id] = await self.liquidity.assess_idea(
                                idea, now=sent_at
                            )
                        except Exception:
                            logger.exception("Liquidity assessment failed for idea %s", idea.id)
                            liquidity_cache[idea.id] = None
                    assessment = liquidity_cache[idea.id]
                message = (
                    format_new_idea(
                        idea,
                        timezone=self.settings.scheduler_timezone,
                        liquidity=assessment,
                    )
                    if event.event_type == "CREATED"
                    else format_lifecycle_event(
                        idea,
                        event,
                        papers.get(idea.id),
                        timezone=self.settings.scheduler_timezone,
                    )
                )
                closed = event.to_status in {
                    IdeaStatus.TP_HIT.value,
                    IdeaStatus.SL_HIT.value,
                    IdeaStatus.EXPIRED.value,
                    IdeaStatus.INVALIDATED.value,
                    IdeaStatus.CANCELLED.value,
                }
                keyboard = (
                    idea_context_keyboard(
                        idea,
                        watched=idea.ticker in watched_since,
                        followed=idea.id in followed_since,
                    )
                    if event.event_type == "CREATED"
                    else lifecycle_context_keyboard(idea, closed=closed)
                )
                try:
                    await _send_bot_message(
                        bot,
                        telegram_id,
                        message,
                        reply_markup=keyboard,
                    )
                    await self._mark_sent(
                        telegram_id,
                        key,
                        event.event_type,
                        idea_id=idea.id,
                        event_id=event.id,
                        sent_at=sent_at,
                    )
                    sent_keys.add(key)
                    counters["sent"] += 1
                except TelegramForbiddenError:
                    await self._deactivate_if_registered(telegram_id)
                    counters["errors"] += 1
                    break
                except Exception:
                    counters["errors"] += 1
                    logger.exception(
                        "Forward notification failed for chat=%s event=%s",
                        telegram_id,
                        event.id,
                    )
        if self.v24_outbox is not None:
            v24 = await self.v24_outbox.dispatch(bot, now=sent_at)
            counters["sent"] += v24["sent"]
            counters["errors"] += v24["errors"]
        return counters

    async def dispatch_daily_summary(
        self,
        bot: Bot,
        *,
        now: datetime | None = None,
    ) -> dict[str, int]:
        sent_at = aware_utc(now or datetime.now(UTC))
        local = sent_at.astimezone(ZoneInfo(self.settings.scheduler_timezone))
        start = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
        async with self.session_factory() as session:
            ideas = list(
                await session.scalars(select(TradingIdea).where(TradingIdea.created_at >= start))
            )
            events = list(
                await session.scalars(
                    select(TradingIdeaEvent).where(TradingIdeaEvent.occurred_at >= start)
                )
            )
            active_now = len(
                list(
                    await session.scalars(
                        select(TradingIdea.id).where(TradingIdea.status.in_(OPEN_IDEA_STATUSES))
                    )
                )
            )
        by_id = {idea.id: idea for idea in ideas}
        all_today_ideas = {**by_id}
        if events:
            async with self.session_factory() as session:
                for idea in await session.scalars(
                    select(TradingIdea).where(
                        TradingIdea.id.in_({event.idea_id for event in events})
                    )
                ):
                    all_today_ideas[idea.id] = idea

        def transition_count(status: str, horizon: str | None = None) -> int:
            return sum(
                event.to_status == status
                and (horizon is None or all_today_ideas[event.idea_id].horizon == horizon)
                for event in events
                if event.idea_id in all_today_ideas
            )

        lines = [
            "📅 <b>FORWARD PAPER — DAILY REPORT</b>",
            "",
            f"New ideas: <b>{len(ideas)}</b>",
            f"Activated: <b>{transition_count(IdeaStatus.ACTIVE.value)}</b>",
            f"TP: <b>{transition_count(IdeaStatus.TP_HIT.value)}</b>",
            f"SL: <b>{transition_count(IdeaStatus.SL_HIT.value)}</b>",
            f"Expired: <b>{transition_count(IdeaStatus.EXPIRED.value)}</b>",
            "",
            f"Active now: <b>{active_now}</b>",
        ]
        for horizon in IdeaHorizon:
            horizon_ideas = [idea for idea in ideas if idea.horizon == horizon.value]
            lines.extend(
                [
                    "",
                    (
                        f"<b>{HORIZON_LABELS[horizon.value]} · "
                        f"{self.settings.observation_mode(horizon)}</b>"
                    ),
                    f"New {len(horizon_ideas)} · "
                    f"Activated {transition_count(IdeaStatus.ACTIVE.value, horizon.value)} · "
                    f"TP {transition_count(IdeaStatus.TP_HIT.value, horizon.value)} · "
                    f"SL {transition_count(IdeaStatus.SL_HIT.value, horizon.value)} · "
                    f"Expired {transition_count(IdeaStatus.EXPIRED.value, horizon.value)}",
                ]
            )
        message = "\n".join(lines)
        recipients = await self._recipients()
        key = f"daily:{local.date().isoformat()}"
        counters = {"recipients": len(recipients), "sent": 0, "errors": 0}
        for telegram_id in recipients:
            user = recipients[telegram_id]
            if user is not None and not AnalysisMode(user.analysis_mode).includes_legacy:
                continue
            if user is not None and not user.notify_daily_summary:
                continue
            async with self.session_factory() as session:
                exists = await session.scalar(
                    select(ForwardNotification.id).where(
                        ForwardNotification.telegram_id == telegram_id,
                        ForwardNotification.notification_key == key,
                    )
                )
            if exists is not None:
                continue
            try:
                await bot.send_message(telegram_id, message)
                await self._mark_sent(
                    telegram_id,
                    key,
                    "DAILY_SUMMARY",
                    sent_at=sent_at,
                )
                counters["sent"] += 1
            except TelegramForbiddenError:
                await self._deactivate_if_registered(telegram_id)
                counters["errors"] += 1
            except Exception:
                counters["errors"] += 1
                logger.exception("Daily summary failed for chat=%s", telegram_id)
        if (
            self.settings.intraday_v24_enabled
            and self.v24_daily is not None
            and self.v24_outbox is not None
        ):
            record = await self.v24_daily.persist(
                local.date(),
                strategy_version=self.settings.intraday_v24_strategy_version,
            )
            payload = format_daily_journal_summary(record)
            for telegram_id, user in recipients.items():
                if user is not None and (
                    not AnalysisMode(user.analysis_mode).includes_v24
                    or not user.notify_daily_summary
                ):
                    continue
                await self.v24_outbox.enqueue(
                    telegram_id=telegram_id,
                    notification_key=f"v24-daily:{local.date().isoformat()}",
                    notification_type="V24_DAILY_JOURNAL",
                    payload=payload,
                    available_at=sent_at,
                )
            v24 = await self.v24_outbox.dispatch(bot, now=sent_at)
            counters["sent"] += v24["sent"]
            counters["errors"] += v24["errors"]
        return counters

    async def dispatch_v24_market_summary(
        self,
        bot: Bot,
        *,
        now: datetime | None = None,
    ) -> dict[str, int]:
        checked_at = aware_utc(now or datetime.now(UTC))
        if (
            not self.settings.intraday_v24_enabled
            or self.v24_outbox is None
            or self.market_overview is None
        ):
            return {"queued": 0, "sent": 0, "errors": 0}
        local = checked_at.astimezone(ZoneInfo(self.settings.scheduler_timezone))
        if local.hour < self.settings.intraday_v24_market_summary_hour:
            return {"queued": 0, "sent": 0, "errors": 0}
        overview = await self.market_overview.current()
        payload = format_market_overview(overview)
        recipients = await self._recipients()
        queued = 0
        for telegram_id in recipients:
            user = recipients[telegram_id]
            if user is not None and not AnalysisMode(user.analysis_mode).includes_v24:
                continue
            queued += await self.v24_outbox.enqueue_market_summary_once(
                telegram_id=telegram_id,
                payload=payload,
                now=checked_at,
                timezone=self.settings.scheduler_timezone,
                after_hour=self.settings.intraday_v24_market_summary_hour,
            )
        dispatched = await self.v24_outbox.dispatch(bot, now=checked_at)
        return {
            "queued": queued,
            "sent": dispatched["sent"],
            "errors": dispatched["errors"],
        }
