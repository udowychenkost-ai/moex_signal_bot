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
                         Idea Repository
                              │
                              ▼
                         IdeaTracker
                         │         │
                         ▼         ▼
                    PaperTrade  ReportingService → Telegram
```

Backtest получает historical candles из того же repository layer и вызывает те
же чистые функции `build_signal`, `build_trading_idea`,
`evaluate_idea_candle`, `calculate_position_size` и `calculate_trade_pnl`.

## Границы модулей

| Модуль | Ответственность | Не отвечает за |
|---|---|---|
| `app/moex.py` | HTTP-контракт MOEX ISS, pagination, retry, преобразование ответа | БД, scoring |
| `app/ingestion.py` | Universe, incremental range, OHLCV/order-book persistence | Генерацию идей |
| `app/analysis.py` | Индикаторы и уровни из переданных candles | BUY/SELL и persistence |
| `app/scoring.py` | Детерминированный technical score | TP/SL, Telegram |
| `app/signals.py` | Один внутренний `GeneratedSignal`, журнал сигналов | Пользовательскую торговую идею |
| `app/horizons.py` | Профили сроков и веса | Отдельную стратегию на горизонт |
| `app/ideas.py` | Агрегация timeframes/factors, entry zone, вызов общего risk manager | Lifecycle после публикации |
| `app/idea_repository.py` | Единственность открытой идеи, material updates, version/dedup events | Анализ рынка |
| `app/idea_tracker.py` | Активация, TP/SL, expiry, missed entry | Генерацию новой идеи |
| `app/reporting.py` | Фильтры пользователя, формат, расписание доставки, notification dedup | Market scan |
| `app/paper.py` | Forward P&L по активированным persisted ideas | Альтернативную торговую стратегию |
| `app/backtest.py` | Историческая оркестрация общего production pipeline и метрики | Отдельные правила сигналов |
| `app/scanner.py` | Один рыночный цикл: ingestion → tracking → ideas → paper | Telegram frequency |
| `app/scheduler.py` | Две задачи в одном scheduler | Бизнес-логику задач |
| `app/migrations.py` | Alembic upgrade и безопасное принятие распознанной legacy-схемы | Runtime `create_all` |

## Signal и TradingIdea

`GeneratedSignal` — внутренний результат одного timeframe: технический score,
BUY/SELL/HOLD, ATR, уровни и объяснения. Он может храниться даже тогда, когда не
проходит требования пользовательской идеи.

`TradingIdea` — версия пользовательского торгового сценария: направление,
горизонт, entry zone, TP/SL, confidence, expected return/risk/R:R, rationale,
invalidation и lifecycle. В БД допускается только одна открытая идея на
`ticker + horizon`. Смена направления отменяет предыдущую идею.

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
- tracker обрабатывает только свечи новее `last_evaluated_at`;
- каждый переход сохраняется в `trading_idea_events`;
- уведомление уникально для `(telegram_id, idea_id, idea_version)`.

## Горизонты

Все горизонты используют один pipeline. `HorizonProfile` задаёт timeframes,
factor weights, confidence floor, ATR multipliers, entry-zone width и expiry.
Месячный профиль не содержит минутных timeframes. Чтобы добавить новый срок,
нужно добавить enum/profile и пользовательское отображение, не новый engine.

## Scanning и reporting

Один `AsyncIOScheduler` содержит:

1. `market_scan`: сразу после старта, затем каждые 5–15 минут по конфигурации;
2. `idea_reporting`: каждые 5 минут проверяет, кому наступило время отправки.

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
