from __future__ import annotations

from urllib.parse import quote

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.ai_ux import has_historical_ai_review
from app.idea_repository import OPEN_IDEA_STATUSES
from app.models import TradingIdea


def moex_url(ticker: str) -> str:
    return f"https://www.moex.com/ru/issue.aspx?board=TQBR&code={quote(ticker.upper())}"


def _page_row(prefix: str, page: int, total_pages: int) -> list[InlineKeyboardButton]:
    previous = max(0, page - 1)
    following = min(total_pages - 1, page + 1)
    return [
        InlineKeyboardButton(text="◀️", callback_data=f"{prefix}:{previous}"),
        InlineKeyboardButton(text=f"{page + 1} / {total_pages}", callback_data="noop"),
        InlineKeyboardButton(text="▶️", callback_data=f"{prefix}:{following}"),
    ]


def idea_context_keyboard(
    idea: TradingIdea,
    *,
    watched: bool,
    followed: bool,
) -> InlineKeyboardMarkup:
    watch_text = "✅ Акция отслеживается" if watched else "👁 Отслеживать акцию"
    watch_action = "unwatch" if watched else "watch"
    if followed:
        follow_text = "✅ Слежу за идеей"
        follow_callback = f"idea_unfollow:{idea.id}"
    elif idea.status in OPEN_IDEA_STATUSES:
        follow_text = "⭐ Следить за идеей"
        follow_callback = f"idea_follow:{idea.id}"
    else:
        follow_text = "🔒 Идея закрыта"
        follow_callback = "noop"
    rows = [
        [
            InlineKeyboardButton(
                text=watch_text,
                callback_data=f"instrument_{watch_action}:{idea.ticker}:{idea.id}",
            ),
            InlineKeyboardButton(
                text=follow_text,
                callback_data=follow_callback,
            ),
        ],
        [
            InlineKeyboardButton(text="❓ Почему идея?", callback_data=f"idea_why:{idea.id}"),
            InlineKeyboardButton(text="🧠 AI-анализ", callback_data=f"idea_ai:{idea.id}"),
        ],
        [
            InlineKeyboardButton(text="📊 Теханализ", callback_data=f"idea_tech:{idea.id}"),
            InlineKeyboardButton(text="🌍 Рынок", callback_data=f"idea_market:{idea.id}"),
        ],
        [
            InlineKeyboardButton(
                text="💧 Ликвидность", callback_data=f"idea_liquidity:{idea.id}"
            )
        ],
        [
            InlineKeyboardButton(
                text="🔄 Что изменилось?",
                callback_data=f"idea_changes:{idea.id}",
            ),
            InlineKeyboardButton(text="📊 Открыть на MOEX", url=moex_url(idea.ticker)),
        ],
        [
            InlineKeyboardButton(text="⬅️ К идеям", callback_data="ideas:0"),
            InlineKeyboardButton(text="🏠 Главное меню", callback_data="home"),
        ],
    ]
    if not has_historical_ai_review(idea):
        rows.insert(
            3,
            [
                InlineKeyboardButton(
                    text="🧠 Проанализировать сейчас",
                    callback_data=f"idea_ai_now:{idea.id}",
                )
            ],
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def liquidity_context_keyboard(idea_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⬅️ Назад к идее", callback_data=f"idea:{idea_id}"
                ),
                InlineKeyboardButton(text="🏠 Главное меню", callback_data="home"),
            ]
        ]
    )


def lifecycle_context_keyboard(idea: TradingIdea, *, closed: bool) -> InlineKeyboardMarkup:
    if closed:
        rows = [
            [
                InlineKeyboardButton(
                    text="📊 Полный результат", callback_data=f"idea_result:{idea.id}"
                ),
                InlineKeyboardButton(
                    text="❓ Почему была открыта?", callback_data=f"idea_why:{idea.id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔄 Как менялась?", callback_data=f"idea_changes:{idea.id}"
                ),
                InlineKeyboardButton(
                    text=f"📜 История {idea.ticker}",
                    callback_data=f"instrument_history:{idea.ticker}:0",
                ),
            ],
            [
                InlineKeyboardButton(text="📒 Все результаты", callback_data="results:menu"),
                InlineKeyboardButton(text="🏠 Главное меню", callback_data="home"),
            ],
        ]
    else:
        rows = [
            [
                InlineKeyboardButton(text="📄 Открыть идею", callback_data=f"idea:{idea.id}"),
                InlineKeyboardButton(
                    text="🔄 Что изменилось?", callback_data=f"idea_changes:{idea.id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="👁 Акция", callback_data=f"instrument_view:{idea.ticker}"
                ),
                InlineKeyboardButton(text="📜 История", callback_data=f"idea_history:{idea.id}"),
            ],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="home")],
        ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def instrument_context_keyboard(
    ticker: str,
    *,
    watched: bool,
    idea_id: int | None,
    back_page: int = 0,
) -> InlineKeyboardMarkup:
    watch_text = "✅ Отслеживается" if watched else "👁 Отслеживать"
    watch_action = "unwatch" if watched else "watch"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="👁 Что происходит?", callback_data=f"instrument_refresh:{ticker}"
                ),
                InlineKeyboardButton(
                    text="🔥 Текущая идея",
                    callback_data=f"idea:{idea_id}" if idea_id else f"instrument_noidea:{ticker}",
                ),
            ],
            [
                InlineKeyboardButton(text="🧠 AI-анализ", callback_data=f"instrument_ai:{ticker}"),
                InlineKeyboardButton(
                    text="📊 Теханализ", callback_data=f"instrument_tech:{ticker}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🏢 Фундаментал", callback_data=f"instrument_fund:{ticker}"
                ),
                InlineKeyboardButton(
                    text="🌍 Рыночный контекст", callback_data=f"instrument_market:{ticker}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📜 История сигналов", callback_data=f"instrument_history:{ticker}:0"
                ),
                InlineKeyboardButton(
                    text="🔔 Настроить уведомления", callback_data="settings:notifications"
                ),
            ],
            [
                InlineKeyboardButton(
                    text=watch_text,
                    callback_data=f"instrument_{watch_action}:{ticker}",
                ),
                InlineKeyboardButton(text="📊 MOEX", url=moex_url(ticker)),
            ],
            [
                InlineKeyboardButton(text="⬅️ Назад", callback_data=f"watchlist:{back_page}"),
                InlineKeyboardButton(text="🏠 Главное меню", callback_data="home"),
            ],
        ]
    )


def instrument_analysis_keyboard(ticker: str, *, watched: bool) -> InlineKeyboardMarkup:
    watch_text = "✅ Отслеживается" if watched else "👁 Отслеживать"
    watch_action = "unwatch" if watched else "watch"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 Обновить", callback_data=f"instrument_refresh:{ticker}"
                ),
                InlineKeyboardButton(
                    text="🔥 Есть ли идея?", callback_data=f"instrument_idea:{ticker}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🧠 AI подробнее", callback_data=f"instrument_ai:{ticker}"
                ),
                InlineKeyboardButton(
                    text="📊 Технические данные", callback_data=f"instrument_tech:{ticker}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🌍 Сравнить с IMOEX", callback_data=f"instrument_market:{ticker}"
                ),
                InlineKeyboardButton(
                    text="📜 История", callback_data=f"instrument_history:{ticker}:0"
                ),
            ],
            [
                InlineKeyboardButton(
                    text=watch_text,
                    callback_data=f"instrument_{watch_action}:{ticker}",
                ),
                InlineKeyboardButton(text="📊 MOEX", url=moex_url(ticker)),
            ],
            [
                InlineKeyboardButton(text="⬅️ К акции", callback_data=f"instrument_view:{ticker}"),
                InlineKeyboardButton(text="🏠 Главное меню", callback_data="home"),
            ],
        ]
    )


def top_ideas_keyboard(
    ideas: list[TradingIdea],
    *,
    filter_key: str = "today",
) -> InlineKeyboardMarkup:
    number_icons = ("1️⃣", "2️⃣", "3️⃣")
    rows = [
        [
            InlineKeyboardButton(
                text=f"{number_icons[index]} {idea.ticker}", callback_data=f"idea:{idea.id}"
            )
        ]
        for index, idea in enumerate(ideas[:3])
    ]
    rows.extend(
        [
            [
                InlineKeyboardButton(text="🔄 Обновить", callback_data=f"best:{filter_key}"),
                InlineKeyboardButton(text="⚙️ Фильтры", callback_data="best:menu"),
            ],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="home")],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def watchlist_keyboard(
    tickers: tuple[str, ...],
    *,
    page: int,
    total_pages: int,
) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"👁 {ticker}", callback_data=f"instrument_view:{ticker}")]
        for ticker in tickers
    ]
    rows.append(_page_row("watchlist", page, total_pages))
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def signal_history_keyboard(
    ticker: str,
    *,
    watched: bool,
    page: int,
    total_pages: int,
) -> InlineKeyboardMarkup:
    watch_text = "✅ Отслеживается" if watched else "👁 Отслеживать"
    watch_action = "unwatch" if watched else "watch"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            _page_row(f"instrument_history:{ticker}", page, total_pages),
            [
                InlineKeyboardButton(
                    text=watch_text,
                    callback_data=f"instrument_{watch_action}:{ticker}",
                ),
                InlineKeyboardButton(text="📊 MOEX", url=moex_url(ticker)),
            ],
            [
                InlineKeyboardButton(text="⬅️ К акции", callback_data=f"instrument_view:{ticker}"),
                InlineKeyboardButton(text="🏠 Главное меню", callback_data="home"),
            ],
        ]
    )


def ideas_page_keyboard(
    ideas: tuple[TradingIdea, ...],
    *,
    page: int,
    total_pages: int,
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"{'📈' if idea.direction == 'BUY' else '📉'} {idea.ticker} · {idea.horizon}",
                callback_data=f"idea:{idea.id}",
            )
        ]
        for idea in ideas
    ]
    rows.append(_page_row("ideas", page, total_pages))
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def results_keyboard(
    selected: str = "menu",
    *,
    page: int = 0,
    total_pages: int = 1,
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="✅ Плюсовые", callback_data="results:win:0"),
            InlineKeyboardButton(text="❌ Минусовые", callback_data="results:loss:0"),
        ],
        [
            InlineKeyboardButton(text="⏳ Активные", callback_data="results:active:0"),
            InlineKeyboardButton(text="⌛ Истекшие", callback_data="results:expired:0"),
        ],
        [
            InlineKeyboardButton(text="🚫 Неактивированные", callback_data="results:missed:0"),
            InlineKeyboardButton(text="🧠 AI отклонённые", callback_data="results:airej:0"),
        ],
        [InlineKeyboardButton(text="📊 Общая статистика", callback_data="stats_view:all:all")],
    ]
    if selected != "menu":
        rows.append(_page_row(f"results:{selected}", page, total_pages))
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def statistics_context_keyboard(period: str = "7", horizon: str = "all") -> InlineKeyboardMarkup:
    def selected(label: str, value: str, current: str) -> str:
        return f"✅ {label}" if value == current else label

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=selected("7 дней", "7", period), callback_data=f"stats_view:7:{horizon}"
                ),
                InlineKeyboardButton(
                    text=selected("30 дней", "30", period),
                    callback_data=f"stats_view:30:{horizon}",
                ),
                InlineKeyboardButton(
                    text=selected("Всё время", "all", period),
                    callback_data=f"stats_view:all:{horizon}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=selected("1D", "1d", horizon), callback_data=f"stats_view:{period}:1d"
                ),
                InlineKeyboardButton(
                    text=selected("5D", "5d", horizon), callback_data=f"stats_view:{period}:5d"
                ),
                InlineKeyboardButton(
                    text=selected("1M", "1m", horizon), callback_data=f"stats_view:{period}:1m"
                ),
                InlineKeyboardButton(
                    text=selected("Все", "all", horizon),
                    callback_data=f"stats_view:{period}:all",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📈 BUY vs SELL",
                    callback_data=f"stats_break:dir:{period}:{horizon}",
                ),
                InlineKeyboardButton(
                    text="🏆 Лучшие бумаги",
                    callback_data=f"stats_break:best:{period}:{horizon}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="💩 Худшие бумаги",
                    callback_data=f"stats_break:worst:{period}:{horizon}",
                ),
                InlineKeyboardButton(
                    text="🧠 Gemini vs Quant",
                    callback_data=f"stats_break:ai:{period}:{horizon}",
                ),
            ],
            [
                InlineKeyboardButton(text="📒 Сигналы", callback_data="results:menu"),
                InlineKeyboardButton(text="🏠 Главное меню", callback_data="home"),
            ],
        ]
    )


def market_context_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔥 Лучшие идеи", callback_data="best:today"),
                InlineKeyboardButton(text="📈 Лидеры рынка", callback_data="market:leaders"),
            ],
            [
                InlineKeyboardButton(text="📉 Слабейшие", callback_data="market:laggards"),
                InlineKeyboardButton(text="🟢 Перепроданные", callback_data="market:oversold"),
            ],
            [
                InlineKeyboardButton(text="🔴 Перекупленные", callback_data="market:overbought"),
                InlineKeyboardButton(text="📦 Аномальный объём", callback_data="market:volume"),
            ],
            [
                InlineKeyboardButton(text="🔄 Обновить", callback_data="market:refresh"),
                InlineKeyboardButton(text="🏠 Главное меню", callback_data="home"),
            ],
        ]
    )


def status_context_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔄 Обновить", callback_data="status:refresh"),
                InlineKeyboardButton(text="📡 MOEX", callback_data="status:moex"),
            ],
            [
                InlineKeyboardButton(text="🧠 Gemini", callback_data="status:gemini"),
                InlineKeyboardButton(text="⏱ Scheduler", callback_data="status:scheduler"),
            ],
            [
                InlineKeyboardButton(text="📊 База", callback_data="status:database"),
                InlineKeyboardButton(text="📈 Последний scan", callback_data="status:scan"),
            ],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="home")],
        ]
    )
