from __future__ import annotations

from html import escape

from app.liquidity import LiquidityAssessment, LiquidityRating

RATING_COMPACT = {
    LiquidityRating.HIGH: "🟢 высокая",
    LiquidityRating.MEDIUM: "🟡 средняя",
    LiquidityRating.LOW: "🔴 низкая",
    LiquidityRating.UNKNOWN: "⚪ не определена",
}
RATING_DETAILED = {
    LiquidityRating.HIGH: "🟢 Высокая",
    LiquidityRating.MEDIUM: "🟡 Средняя",
    LiquidityRating.LOW: "🔴 Низкая",
    LiquidityRating.UNKNOWN: "⚪ Не определена",
}


def format_rubles(value: float | None) -> str:
    if value is None:
        return "нет данных"
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f} млрд ₽"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f} млн ₽"
    if value >= 1_000:
        return f"{value / 1_000:.0f} тыс. ₽"
    return f"{value:.0f} ₽"


def format_comfortable_size(value: float | None) -> str:
    if value is None:
        return "нет данных"
    if value >= 1_000_000:
        amount = value / 1_000_000
        rendered = f"{amount:.1f}".rstrip("0").rstrip(".")
        return f"до ~{rendered} млн ₽"
    return f"до ~{value:,.0f} ₽".replace(",", " ")


def format_liquidity_compact(assessment: LiquidityAssessment | None) -> str:
    if assessment is None:
        return (
            "💧 <b>Ликвидность</b>\n"
            "Оборот сегодня: нет данных\n"
            "Средний оборот: нет данных\n"
            "Ликвидность: ⚪ не определена\n"
            "Комфортный размер по ликвидности: нет данных"
        )
    turnover = (
        assessment.turnover_today if assessment.market_open else assessment.last_daily_turnover
    )
    turnover_label = "Оборот сегодня" if assessment.market_open else "Последний оборот"
    return (
        "💧 <b>Ликвидность</b>\n"
        f"{turnover_label}: {format_rubles(turnover)}\n"
        f"Средний оборот: {format_rubles(assessment.adv20)}\n"
        f"Ликвидность: {RATING_COMPACT[assessment.liquidity_rating]}\n"
        "Комфортный размер по ликвидности: "
        f"{format_comfortable_size(assessment.comfortable_size_rounded)}"
    )


def _format_price(value: float | None) -> str:
    if value is None:
        return "нет данных"
    return f"{value:,.2f} ₽".replace(",", " ")


def _format_relative(value: float | None) -> str:
    return "нет данных" if value is None else f"{value:.2f}×"


def _format_spread(value: float | None) -> str:
    return "нет данных" if value is None else f"{value * 100:.2f}%"


def format_liquidity_details(assessment: LiquidityAssessment) -> str:
    title_name = escape(assessment.instrument_name or assessment.ticker)
    ticker = escape(assessment.ticker)
    title = (
        f"💧 <b>Ликвидность — {title_name} ({ticker})</b>"
        if title_name.upper() != ticker
        else f"💧 <b>Ликвидность — {ticker}</b>"
    )
    lines = [title, "", f"Цена: <b>{_format_price(assessment.price)}</b>", ""]
    if assessment.market_open:
        lines.extend(
            [
                "Оборот сегодня:",
                f"<b>{format_rubles(assessment.turnover_today)}</b>",
            ]
        )
    else:
        lines.extend(
            [
                "Биржа сейчас закрыта.",
                "",
                "Последний дневной оборот:",
                f"<b>{format_rubles(assessment.last_daily_turnover)}</b>",
            ]
        )
    adv_period = (
        f"{assessment.adv_days_target}D"
        if assessment.adv_days_used >= assessment.adv_days_target
        else f"{assessment.adv_days_used}/{assessment.adv_days_target} дней"
    )
    lines.extend(
        [
            "",
            f"Средний дневной оборот {adv_period}:",
            f"<b>{format_rubles(assessment.adv20)}</b>",
            "",
            "Текущий объём:" if assessment.market_open else "Последний оборот к среднему:",
            f"<b>{_format_relative(assessment.relative_turnover)}</b>",
            "",
        ]
    )
    spread_label = "Спред" if assessment.spread_is_current else "Последний доступный спред"
    lines.extend([f"{spread_label}:", f"<b>{_format_spread(assessment.spread_pct)}</b>", ""])
    direction_context = (
        "BUY: вход оценивается по ask, потенциальный выход — по bid."
        if assessment.direction == "BUY"
        else "SELL: вход оценивается по bid, обратный выкуп/выход — по ask."
    )
    lines.extend([direction_context, "Показана более консервативная из двух сторон.", ""])
    lines.append("Стакан:")
    if assessment.book_fresh:
        for depth in assessment.depths:
            lines.append(f"±{depth.band * 100:.2f}% — <b>{format_rubles(depth.relevant_depth)}</b>")
    else:
        lines.append("<b>недоступен / устарел</b>")
        if assessment.market_open and assessment.book_snapshot_at is not None:
            lines.extend(
                [
                    "",
                    "⚠️ Стакан устарел — оценка размера основана только на обороте.",
                ]
            )
        elif assessment.market_open:
            lines.extend(
                [
                    "",
                    "⚠️ Текущий стакан недоступен — оценка размера основана только на обороте.",
                ]
            )
    lines.extend(
        [
            "",
            "Ликвидность:",
            f"<b>{RATING_DETAILED[assessment.liquidity_rating]}</b>",
            "",
            "Комфортный размер по ликвидности:",
            f"<b>{format_comfortable_size(assessment.comfortable_size_rounded)}</b>",
        ]
    )
    if assessment.calculation_basis == "TURNOVER_ONLY":
        lines.append("<i>Без учёта текущего стакана.</i>")
    elif assessment.calculation_basis == "INSUFFICIENT":
        lines.append("<i>Недостаточно данных для расчёта размера.</i>")
    lines.extend(
        [
            "",
            "Оценка учитывает оборот, свежий стакан, спред и существующий контекст "
            "волатильности, когда эти данные доступны.",
            "Это не рекомендация по размеру риска.",
            "Фактический размер позиции должен отдельно учитывать размер капитала, "
            "стоп и допустимый риск.",
        ]
    )
    return "\n".join(lines)
