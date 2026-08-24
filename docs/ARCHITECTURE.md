# Архитектура MOEX Signal Bot

## Поток данных

```text
MOEX ISS
  │
  ▼
MoexClient ── pagination / retry / resampling / ISS+ auth
  │
  ▼
IngestionService ── incremental overlap + upsert
  │
  ├── instruments
  ├── candles
  └── order_book_levels
          │
          ▼
Analysis Engine → Scoring Engine → SignalService
                                      │
                                      ▼
                            TradingIdeaGenerator
                              │             │
                              │       shared Risk Manager
                              ▼
                     Quant candidate (detached)
                              │
                  QualityGate + conflicts + ranking
                              │
                 CandidateExperiment (immutable input)
                              │ PASS
                    AIAnalystService (structured)
                              │ APPROVE
                         Idea Repository
                              │
                              ▼
                         IdeaTracker
                         │         │
                         ▼         ▼
            POSITION-only Paper  Forward outbox → Telegram
                                      │
                         status / stats / daily summary
```

Backtest получает historical candles из того же repository layer и вызывает те
же чистые функции `build_signal`, `build_trading_idea`,
`evaluate_idea_candle`, `calculate_position_size` и `calculate_trade_pnl`.

## Границы модулей

| Модуль | Ответственность | Не отвечает за |
|---|---|---|
| `app/moex.py` | HTTP-контракт MOEX ISS, pagination, retry, преобразование ответа | БД, scoring |
| `app/ingestion.py` | Universe, incremental range, OHLCV/order-book persistence | Генерацию идей |
| `app/analysis.py` | Индикаторы, уровни и детерминированный technical score | TP/SL, Telegram |
| `app/signals.py` | Один внутренний `GeneratedSignal`, журнал сигналов | Пользовательскую торговую идею |
| `app/horizons.py` | Профили сроков и веса | Отдельную стратегию на горизонт |
| `app/ideas.py` | Агрегация timeframes/factors, entry zone, вызов общего risk manager | Lifecycle после публикации |
| `app/quality.py` | Детерминированный PASS/WEAK/REJECT, confirmations/conflicts/regime compatibility | LLM reasoning |
| `app/ai_analyst.py` | Provider-neutral structured second opinion, Gemini fallback orchestration, no-invention prompt, fail-closed result | Quant score и право спасать REJECT |
| `app/ai_providers.py` | Gemini/OpenAI HTTP contracts, exact model/usage/latency/error telemetry | QualityGate, scoring и решение о публикации |
| `app/on_demand_ai.py` | Явный Gemini review текущего candidate и отдельная telemetry | Перезапись historical verdict/snapshot или публикация идеи |
| `app/experiments.py` | Frozen candidate cohorts, cooldown, AI telemetry link и rejected lifecycle | Пользовательскую публикацию |
| `app/idea_repository.py` | Единственность открытой идеи, material updates, version/dedup events | Анализ рынка |
| `app/idea_tracker.py` | Активация, TP/SL, expiry, missed entry | Генерацию новой идеи |
| `app/reporting.py` | Фильтры пользователя, формат, расписание доставки, notification dedup | Market scan |
| `app/forward.py` | Event notifications, `/status` formatting, stats and daily summary | Рыночный анализ |
| `app/telegram_context.py` | Авторизованные DB-backed watch/follow, object lookup и pagination | Форматирование Telegram-кнопок |
| `app/telegram_ui.py` | Короткие callback IDs и контекстные inline keyboards | Доступ к БД и бизнес-решения |
| `app/observation.py` | Closed-candle and freshness guards | Scoring |
| `app/operations.py` | Job state, health, forward metrics and lifecycle details | Scheduler triggers |
| `app/paper.py` | Forward P&L только по активированным `POSITION_1M=PAPER` | Broker execution |
| `app/backtest.py` | Историческая оркестрация общего production pipeline и метрики | Отдельные правила сигналов |
| `app/research_data.py` | Отдельный universe, incremental dataset и coverage metadata | Production universe |
| `app/research_runner.py` | TRAIN/VALIDATION/OOS, calibration и walk-forward orchestration | Изменение production defaults |
| `app/research_baselines.py` | Research-only buy-and-hold/EMA/RSI benchmarks | Production signals |
| `app/scanner.py` | Раздельные ingestion, tracking, idea and paper operations | Telegram frequency |
| `app/scheduler.py` | Пять независимых jobs в одном scheduler | Бизнес-логику задач |
| `app/migrations.py` | Alembic upgrade и безопасное принятие распознанной legacy-схемы | Runtime `create_all` |

## Signal и TradingIdea

`GeneratedSignal` — внутренний результат одного timeframe: технический score,
BUY/SELL/HOLD, ATR, уровни и объяснения. Он может храниться даже тогда, когда не
проходит требования пользовательской идеи.

`TradingIdea` — версия пользовательского торгового сценария: направление,
горизонт, entry zone, TP/SL, confidence, expected return/risk/R:R, rationale,
invalidation и lifecycle. В БД допускается только одна открытая идея на
`ticker + horizon`. Смена направления отменяет предыдущую идею.

`CandidateExperiment` создаётся для любого quant BUY/SELL candidate до решения о
публикации. Его decision snapshot и quant/quality/AI результаты не
перезаписываются. Собственный tracker продолжает pending/active lifecycle даже
для AI REJECT, поэтому `/stats` сравнивает actual outcomes, а не только решения
модели.

```text
Quant candidate
  ├─ Quality REJECT/WEAK ───────────────► research cohort only
  └─ Quality PASS
       ├─ cooldown/rank limit ──────────► research cohort only
       └─ structured AI review
            ├─ WAIT/REJECT ─────────────► research cohort only
            └─ APPROVE/STRONG_APPROVE ─► TradingIdea + Telegram
```

Факторная оценка хранит `technical_score`, `fundamental_score`, `news_score` и
`total_score`. Сейчас внешний источник есть только у technical. Вес отсутствующих
факторов исключается из знаменателя, поэтому нулевой placeholder не занижает
текущий результат. Это позволяет позже подключить fundamentals/news явно.

## Инварианты lifecycle

```text
                  цена вошла в entry zone
PENDING_ENTRY ─────────────────────────────► ACTIVE
     │                                         │
     │ TP достигнут без входа                  ├──► TP_HIT
     ├──────────────────────► INVALIDATED      ├──► SL_HIT
     │                                         └──► EXPIRED
     └──────────────────────► EXPIRED/CANCELLED
```

- `entry_price_from <= entry_price_to`;
- BUY: `SL < reference entry < TP`, SELL: `TP < reference entry < SL`;
- идея не создаётся при R:R ниже системного минимума;
- TP до входа означает `INVALIDATED`, а не прибыль;
- если одна OHLC-свеча одновременно касается TP и SL, применяется консервативный
  SL-first порядок;
- tracker обрабатывает только завершённые свечи новее `last_evaluated_at`;
- каждый переход сохраняется в `trading_idea_events`;
- новая идея всегда начинается `PENDING_ENTRY`, поэтому formation candle не
  может одновременно доказать вход;
- decision snapshot создаётся один раз и после reassessment не изменяется;
- lifecycle notification уникально для `(telegram_id, event_id)`.
- watchlist уникален для `(telegram_id, secid)`, follow — для
  `(telegram_id, idea_id)`; explicit watch/unwatch и follow/unfollow остаются
  идемпотентными при повторной доставке callback;
- callback повторно проверяет активного Telegram user и существование объекта;
  закрытую идею нельзя начать follow, но существующий follow можно снять;
- callback_data содержит только action + stable ID/ticker и не превышает лимит
  Telegram; пользовательские состояния и рыночные данные в payload не кладутся.

## Горизонты

Все горизонты используют один pipeline. `HorizonProfile` задаёт timeframes,
factor weights, confidence floor, ATR multipliers, entry-zone width и expiry.
Месячный профиль не содержит минутных timeframes. Чтобы добавить новый срок,
нужно добавить enum/profile и пользовательское отображение, не новый engine.

## Scanning и reporting

Один `AsyncIOScheduler` содержит:

1. `market_ingestion`: MOEX universe и свечи;
2. `idea_scanning`: freshness check и новые/изменённые идеи;
3. `lifecycle_tracking`: активация/закрытие и POSITION paper P&L;
4. `telegram_reporting`: независимая доставка новых событий;
5. `daily_summary`: один вечерний forward report.

Market scan не зависит от Telegram-настроек. Reporting не пересчитывает рынок и
не создаёт слабую идею ради расписания. `max_instances=1` предотвращает
параллельный запуск одного и того же job в рамках процесса.

## Persistence и миграции

SQLAlchemy-модель едина для SQLite/PostgreSQL. Alembic — единственный production
механизм изменения схемы. На старте приложение:

1. инспектирует схему;
2. для versioned DB выполняет обычный upgrade;
3. для распознанной старой `create_all`-схемы ставит корректный stamp;
4. обновляет до head;
5. отказывается продолжать для неизвестной неполной схемы.

Ингestion запрашивает от последней сохранённой свечи с небольшим overlap. Upsert
обновляет незакрытую свечу и не создаёт дубликаты, поэтому PostgreSQL/SQLite уже
выполняют роль historical cache; Redis в текущем потоке не нужен.

## Известные ограничения и точки расширения

- fundamental/news/sector providers ещё не подключены;
- backtest CLI выводит агрегированные метрики, экспорт списка сделок пока не
  оформлен отдельной командой;
- paper account глобальный системный, не персональный брокерский портфель;
- order book зависит от доступности ISS+ и не участвует в основном score по
  умолчанию;
- несколько реплик scheduler требуют внешней leader-election/lock стратегии;
- параметры стратегии нужно калибровать out-of-sample до любого реального риска;
- бот не размещает реальные биржевые заявки.
- research universe фиксирован по текущей ликвидности и не устраняет
  survivorship bias; свечи не скорректированы на corporate actions.
