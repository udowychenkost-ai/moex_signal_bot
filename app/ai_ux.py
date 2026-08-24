from __future__ import annotations

import json
from html import escape

from app.ai_analyst import AIReviewResult
from app.domain import TradingIdeaData
from app.models import TradingIdea

AI_VERDICT_LABELS = {
    "STRONG_APPROVE": "Сильное подтверждение",
    "APPROVE": "Подтверждено",
    "WAIT": "Подождать",
    "REJECT": "Отклонено",
}


def has_historical_ai_review(idea: TradingIdea) -> bool:
    return idea.ai_verdict in AI_VERDICT_LABELS and bool(
        idea.ai_provider
        or idea.ai_model
        or idea.ai_reviewed_at
        or idea.ai_score is not None
        or idea.ai_short_summary
        or idea.ai_bull_case
        or idea.ai_bear_case
        or idea.ai_why_now
    )


def historical_ai_unavailable_reason(idea: TradingIdea) -> str:
    if idea.strategy_version == "v1":
        return "Идея создана до включения обязательного AI-фильтра."
    if idea.ai_provider is None and idea.ai_reviewed_at is None:
        return "Для идеи не сохранён успешный AI review на момент её создания."
    return "AI review не был успешно завершён в момент создания идеи."


def _json_items(value: str) -> list[str]:
    try:
        parsed = json.loads(value or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item).strip() for item in parsed if str(item).strip()]


def _provider_title(provider: str | None) -> str:
    if (provider or "").lower() == "gemini":
        return "🧠 <b>Gemini</b>"
    if (provider or "").lower() == "openai":
        return "🧠 <b>OpenAI</b>"
    return "🧠 <b>AI-анализ</b>"


def _analysis_sections(
    *,
    bull_case: str,
    bear_case: str,
    why_now: str,
    risks: list[str],
    invalidations: list[str],
) -> list[str]:
    sections: list[str] = []
    if bull_case.strip():
        sections.append(f"<b>Почему интересно:</b>\n{escape(bull_case.strip())}")
    if bear_case.strip():
        sections.append(f"<b>Что против:</b>\n{escape(bear_case.strip())}")
    if why_now.strip():
        sections.append(f"<b>Почему сейчас:</b>\n{escape(why_now.strip())}")
    if risks:
        sections.append(
            "<b>Главные риски:</b>\n" + "\n".join(f"• {escape(item)}" for item in risks)
        )
    if invalidations:
        sections.append(
            "<b>Условия отмены:</b>\n" + "\n".join(f"• {escape(item)}" for item in invalidations)
        )
    return sections


def format_historical_ai_analysis(idea: TradingIdea) -> str:
    if not has_historical_ai_review(idea):
        return (
            "🧠 <b>AI-анализ</b>\n\n"
            "AI-анализ для этой идеи не выполнялся.\n\n"
            "<b>Причина:</b>\n"
            f"{escape(historical_ai_unavailable_reason(idea))}\n\n"
            "<b>Возможные причины:</b>\n"
            "• идея создана до включения AI-фильтра;\n"
            "• research candidate;\n"
            "• AI review не был запрошен;\n"
            "• provider был недоступен;\n"
            "• AI review не был завершён."
        )

    score = f"{idea.ai_score:.0f}/100" if idea.ai_score is not None else "нет оценки"
    header = (
        f"{_provider_title(idea.ai_provider)}\n\n"
        f"Вердикт: <b>{AI_VERDICT_LABELS[idea.ai_verdict]}</b>\n"
        f"AI-оценка: <b>{score}</b>"
    )
    sections = _analysis_sections(
        bull_case=idea.ai_bull_case or "",
        bear_case=idea.ai_bear_case or "",
        why_now=idea.ai_why_now or "",
        risks=_json_items(idea.ai_key_risks),
        invalidations=_json_items(idea.ai_invalidation_conditions),
    )
    return "\n\n".join((header, *sections))


def format_ai_idea_summary(idea: TradingIdea) -> str:
    if not has_historical_ai_review(idea):
        return "🧠 <b>AI-анализ:</b> не выполнялся."
    score = f"{idea.ai_score:.0f}/100" if idea.ai_score is not None else "без оценки"
    lines = [
        _provider_title(idea.ai_provider),
        f"Вердикт: <b>{AI_VERDICT_LABELS[idea.ai_verdict]}</b> · {score}",
    ]
    if idea.ai_short_summary.strip():
        lines.append(escape(idea.ai_short_summary.strip()))
    if idea.ai_why_now.strip():
        lines.append(escape(idea.ai_why_now.strip()))
    risks = _json_items(idea.ai_key_risks)[:2]
    if risks:
        lines.append("⚠️ <b>Основные риски:</b>\n" + "\n".join(f"• {escape(x)}" for x in risks))
    return "\n".join(lines)


def format_current_ai_analysis(
    review: AIReviewResult,
    candidate: TradingIdeaData,
) -> str:
    if review.status != "OK":
        return (
            "🧠 <b>Gemini</b>\n\n"
            "AI-анализ текущего состояния не выполнен.\n\n"
            "<b>Причина:</b>\n"
            "Gemini временно недоступен или не вернул корректный структурированный ответ.\n\n"
            "<i>Исторический AI verdict идеи не изменён.</i>"
        )
    analysis = review.analysis
    header = (
        "🧠 <b>Gemini</b>\n\n"
        f"Вердикт: <b>{AI_VERDICT_LABELS[analysis.verdict]}</b>\n"
        f"AI-оценка: <b>{analysis.score:.0f}/100</b>"
    )
    sections = _analysis_sections(
        bull_case=analysis.bull_case,
        bear_case=analysis.bear_case,
        why_now=analysis.why_now,
        risks=list(analysis.key_risks),
        invalidations=list(analysis.invalidation_conditions),
    )
    context = (
        f"Текущий сценарий: <b>{candidate.direction.value} · {escape(candidate.ticker)} · "
        f"{candidate.horizon.value}</b>"
    )
    marker = "<i>AI-анализ выполнен сейчас, а не в момент создания идеи.</i>"
    return "\n\n".join((header, context, *sections, marker))


def format_current_ai_unavailable(reason: str) -> str:
    return (
        "🧠 <b>Gemini</b>\n\n"
        "AI-анализ текущего состояния не выполнен.\n\n"
        f"<b>Причина:</b>\n{escape(reason)}\n\n"
        "<i>Исторический AI verdict идеи и snapshot не изменены.</i>"
    )
