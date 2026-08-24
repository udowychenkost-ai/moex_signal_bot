# Telegram V2.1 contextual inline UX

The lower Reply Keyboard remains the global navigation surface. Inline
keyboards act only on the card currently shown in chat. Slash commands remain
available and no callback can place an exchange or broker order.

V2.1.1 expands the Reply Keyboard to a compact two-column grid: best ideas,
watchlist, active ideas, results, current market, ticker check, statistics,
settings and system health. `Проверить акцию` asks for a MOEX ticker through
Telegram ForceReply and runs the same current signal path as `/signal`.

## Text examples

New idea:

```text
🔥 BUY — SBER (Сбербанк)
Entry: 307.20–309.10 · TP: 326.50 · SL: 299.40
Horizon: 1M · Strength: 84 · R:R: 2.4

[👁 Отслеживать акцию] [⭐ Следить за идеей]
[❓ Почему идея?]      [🧠 AI-анализ]
[📊 Теханализ]         [🌍 Рынок]
[🔄 Что изменилось?]   [📊 Открыть на MOEX]
[⬅️ К идеям]           [🏠 Главное меню]
```

After watch/follow, the same message markup is edited in place:

```text
[✅ Акция отслеживается] [✅ Слежу за идеей]
```

Closed lifecycle event:

```text
🎯 TP HIT — SBER · 1M
Status: TP_HIT · Result: +2.40R

[📊 Полный результат] [❓ Почему была открыта?]
[🔄 Как менялась?]    [📜 История SBER]
[📒 Все результаты]   [🏠 Главное меню]
```

Paginated watchlist:

```text
👁 Watchlist · 6 инструментов
[👁 GAZP]
[👁 LKOH]
[👁 MOEX]
[👁 PLZL]
[👁 ROSN]
[◀️] [1 / 2] [▶️]
[🏠 Главное меню]
```

## Callback and persistence contract

- Callback payloads contain only a short action plus a numeric idea ID or a
  validated MOEX ticker; automated tests enforce Telegram's 64-byte limit.
- Each callback resolves the current active Telegram user again. Mutations are
  scoped to that user's `telegram_id`; idea/instrument existence and action
  relevance are rechecked server-side.
- `watchlist_items` is unique on `(telegram_id, secid)` and `idea_follows` is
  unique on `(telegram_id, idea_id)`. Explicit watch/unwatch and follow/unfollow
  operations are safe to retry.
- A deleted idea or unknown/inactive instrument returns an alert and performs no
  edit. A closed idea rejects a new follow as expired; an existing follow can
  still be removed.
- Watch/follow settings live in PostgreSQL and therefore survive application and
  container restarts. No state is trusted from the old message markup.
- With watch notifications enabled, future events for watched tickers take
  priority over generic event/horizon/strength preferences. Following an idea
  does the same for its future lifecycle events. Subscription timestamps prevent
  old skipped events from being replayed; the user's AI safety filter remains
  authoritative.
- Text/markup is edited in place where Telegram permits it. New ideas and
  lifecycle events stay separate notifications. Home sends one small message so
  Telegram can restore the persistent Reply Keyboard, which cannot be attached
  to an inline edit.

## Historical ideas without AI review

`NOT_REQUESTED`, `AI_NOT_REVIEWED` and provider-internal status values are never
rendered to the user. An idea without a successful creation-time review shows a
compact explanation and omits empty bull/bear/timing/risk sections. Its
`Проанализировать сейчас` action refreshes the horizon timeframes, builds a new
current candidate, runs the unchanged QualityGate and Gemini structured
contract, and stores only request telemetry. It does not call
`apply_ai_review`, update `TradingIdea`, or replace `TradingIdeaSnapshot`.

Current results are explicitly labeled `AI-анализ выполнен сейчас, а не в момент
создания идеи.` Russian UI labels map the four stored verdicts without changing
their database values: strong confirmation, confirmed, wait and rejected.

## Navigation map

Idea cards link to rationale, AI, technical, market, change history, MOEX,
active ideas and Home. Instrument cards link to a fresh scan, current idea,
AI/technical/fundamental/market details, signal history, notification settings,
watch toggle and MOEX. Results, statistics, market dashboard and system status
all retain Home plus their own refresh/filter navigation. Watchlist, open ideas,
signal history and every result cohort use five-row pages.
