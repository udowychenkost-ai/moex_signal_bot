from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai_providers import AsyncHTTPClient, GeminiHealthStatus, GeminiProvider
from app.config import Settings
from app.models import AIRequestLog
from app.observation import aware_utc

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class GeminiRuntimeHealth:
    provider: str
    primary: GeminiHealthStatus
    fallback: GeminiHealthStatus
    api_status: str
    checked_at: datetime


@dataclass(frozen=True, slots=True)
class GeminiRequestDiagnostics:
    requests_today: int
    successes: int
    errors: int
    fallback_used: int
    last_success_at: datetime | None
    last_error_at: datetime | None
    last_error_status_code: int | None
    last_error_code: str
    last_error_message: str


def _provider(settings: Settings, model: str, *, fallback: bool, client=None) -> GeminiProvider:
    return GeminiProvider(
        api_key=settings.gemini_api_key,
        base_url=settings.gemini_base_url,
        model=model,
        timeout_seconds=settings.ai_request_timeout_seconds,
        input_cost_per_million=(
            settings.ai_fallback_input_cost_per_million
            if fallback
            else settings.ai_input_cost_per_million
        ),
        output_cost_per_million=(
            settings.ai_fallback_output_cost_per_million
            if fallback
            else settings.ai_output_cost_per_million
        ),
        client=client,
        is_fallback=fallback,
    )


def _error_metadata(row: AIRequestLog) -> tuple[int | None, str, str]:
    usage: dict[str, object] = {}
    try:
        parsed = json.loads(row.usage_json or "{}")
        if isinstance(parsed, dict):
            usage = parsed
    except (json.JSONDecodeError, TypeError):
        pass
    raw_status = usage.get("status_code")
    status_code = int(raw_status) if isinstance(raw_status, int | float) else None
    error_code = str(usage.get("error_code") or "").strip().upper()
    message = str(usage.get("error_message") or row.error or "Ошибка Gemini API").strip()
    if status_code is None:
        match = re.search(r"\b([45][0-9]{2})\b", row.error or "")
        status_code = int(match.group(1)) if match else None
    lowered = message.lower()
    if not error_code and status_code == 404 and "model" in lowered:
        error_code = "MODEL_NOT_FOUND"
    if not error_code:
        error_code = f"HTTP_{status_code}" if status_code is not None else "ERROR"
    if status_code is not None and "for url" in lowered:
        message = f"Gemini API returned HTTP {status_code} for the configured model."
    message = re.sub(r"(?i)([?&]key=)[^&\s'\"]+", r"\1***", message)
    return status_code, error_code, " ".join(message.split())[:300]


def format_gemini_diagnostics(
    health: GeminiRuntimeHealth,
    stats: GeminiRequestDiagnostics,
) -> str:
    primary_listed = "YES" if health.primary.model_listed else "NO"
    primary_callable = "YES" if health.primary.model_callable else "NO"
    fallback_listed = "YES" if health.fallback.model_listed else "NO"
    fallback_callable = "YES" if health.fallback.model_callable else "NO"
    last_success = stats.last_success_at.isoformat() if stats.last_success_at else "нет"
    last_error_at = stats.last_error_at.isoformat() if stats.last_error_at else "нет"
    status_prefix = (
        str(stats.last_error_status_code) if stats.last_error_status_code is not None else ""
    )
    error_code = " ".join(item for item in (status_prefix, stats.last_error_code) if item)
    last_error = escape(stats.last_error_message or "нет")
    return (
        "🧠 <b>Gemini</b>\n\n"
        "Provider: <b>Gemini</b>\n"
        f"Primary model: <b>{escape(health.primary.configured_model)}</b>\n"
        f"Fallback model: <b>{escape(health.fallback.configured_model)}</b>\n\n"
        f"API: <b>{health.api_status}</b>\n\n"
        "<b>Primary:</b>\n"
        f"LISTED: <b>{primary_listed}</b>\n"
        f"CALLABLE: <b>{primary_callable}</b>\n\n"
        "<b>Fallback:</b>\n"
        f"LISTED: <b>{fallback_listed}</b>\n"
        f"CALLABLE: <b>{fallback_callable}</b>\n\n"
        f"Requests today: <b>{stats.requests_today}</b>\n"
        f"Success: <b>{stats.successes}</b>\n"
        f"Errors: <b>{stats.errors}</b>\n"
        f"Fallback used: <b>{stats.fallback_used}</b>\n\n"
        f"Last successful request: <b>{escape(last_success)}</b>\n"
        f"Last error: <b>{escape(last_error_at)}</b>\n"
        f"Last error code: <b>{escape(error_code or 'нет')}</b>\n"
        f"{last_error}"
    )


class GeminiHealthMonitor:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        *,
        primary: GeminiProvider | None = None,
        fallback: GeminiProvider | None = None,
        client: AsyncHTTPClient | None = None,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.primary = primary or _provider(
            settings,
            settings.ai_model,
            fallback=False,
            client=client,
        )
        self.fallback = fallback or _provider(
            settings,
            settings.ai_fallback_model,
            fallback=True,
            client=client,
        )
        self.last_report: GeminiRuntimeHealth | None = None

    async def refresh(self) -> GeminiRuntimeHealth:
        if self.fallback is self.primary:
            primary = await self.primary.check_health()
            fallback = primary
        else:
            primary, fallback = await asyncio.gather(
                self.primary.check_health(),
                self.fallback.check_health(),
            )
        if primary.model_available:
            api_status = "OK"
        elif fallback.model_available:
            api_status = "DEGRADED"
        else:
            api_status = "ERROR"
        report = GeminiRuntimeHealth(
            provider="gemini",
            primary=primary,
            fallback=fallback,
            api_status=api_status,
            checked_at=max(primary.checked_at, fallback.checked_at),
        )
        self.last_report = report
        return report

    async def validate_startup(self) -> GeminiRuntimeHealth:
        report = await self.refresh()
        if report.api_status == "OK":
            logger.info(
                "Gemini provider health OK primary=%s callable=true fallback=%s callable=%s",
                report.primary.configured_model,
                report.fallback.configured_model,
                str(report.fallback.model_callable).lower(),
            )
        elif report.api_status == "DEGRADED":
            logger.warning(
                "Gemini provider health DEGRADED primary=%s callable=false "
                "fallback=%s callable=true",
                report.primary.configured_model,
                report.fallback.configured_model,
            )
        else:
            logger.error(
                "Gemini provider health ERROR primary=%s code=%s fallback=%s code=%s",
                report.primary.configured_model,
                report.primary.error_code,
                report.fallback.configured_model,
                report.fallback.error_code,
            )
        return report

    async def request_diagnostics(
        self,
        *,
        now: datetime | None = None,
    ) -> GeminiRequestDiagnostics:
        if self.session_factory is None:
            return GeminiRequestDiagnostics(0, 0, 0, 0, None, None, None, "", "")
        checked_at = aware_utc(now or datetime.now(UTC))
        local_now = checked_at.astimezone(ZoneInfo(self.settings.scheduler_timezone))
        day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
        async with self.session_factory() as session:
            rows = list(
                await session.scalars(
                    select(AIRequestLog)
                    .where(
                        AIRequestLog.provider == "gemini",
                        AIRequestLog.created_at >= day_start,
                    )
                    .order_by(AIRequestLog.created_at, AIRequestLog.id)
                )
            )
        successes = [row for row in rows if row.status == "OK"]
        errors = [row for row in rows if row.status != "OK"]
        last_error = errors[-1] if errors else None
        status_code, error_code, message = (
            _error_metadata(last_error) if last_error is not None else (None, "", "")
        )
        return GeminiRequestDiagnostics(
            requests_today=len(rows),
            successes=len(successes),
            errors=len(errors),
            fallback_used=sum(row.fallback_used for row in rows),
            last_success_at=successes[-1].created_at if successes else None,
            last_error_at=last_error.created_at if last_error is not None else None,
            last_error_status_code=status_code,
            last_error_code=error_code,
            last_error_message=message,
        )
