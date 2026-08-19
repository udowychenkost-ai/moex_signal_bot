# MOEX Signal Bot

Асинхронный Telegram-бот для поиска и сопровождения торговых идей по акциям
Московской биржи. Сервис постоянно обновляет MOEX ISS-данные, строит внутренние
технические сигналы, превращает только качественные сигналы в `TradingIdea`,
отслеживает вход/TP/SL/срок и отдельно отправляет персональные отчёты.

> Все результаты носят информационный характер и не являются индивидуальной
> инвестиционной рекомендацией.

## Что работает

- async MOEX ISS-клиент на `httpx`: pagination, retry/backoff, concurrency limit,
  M1→M5/M15 и опциональный ISS+ order book;
- инкрементальная история в SQLite или PostgreSQL с overlap/upsert открытых свечей;
- единый technical-analysis pipeline: SMA/EMA, MACD, ADX, RSI, Stochastic, CCI,
  Bollinger Bands, ATR, OBV, relative volume и clustered support/resistance;
- детерминированный BUY/SELL/HOLD и совместимые `legacy`/`weighted` scoring modes;
- отдельная доменная модель `TradingIdea` с горизонтами 1 день, 5 дней и 1 месяц;
- диапазон входа, ATR/level TP/SL, минимальный R:R и lot-aware sizing;
- lifecycle `PENDING_ENTRY → ACTIVE → TP_HIT/SL_HIT/EXPIRED`, включая
  `CANCELLED` и `INVALIDATED`, с полной историей событий;
- защита от ложного результата: идея не считается активированной или выигрышной,
  если цена не вошла в entry zone;
- Telegram-меню «Лучшие идеи / Мои идеи / Настройки», компактная карточка и
  отдельная кнопка «Подробнее»;
- пользовательские фильтры: частота, горизонт, риск и минимальный confidence;
- один APScheduler с независимыми задачами market scanning и reporting;
- version-based deduplication уведомлений без повтора неизменившейся идеи;
- historical backtest на том же signal/idea/risk/lifecycle pipeline;
- forward paper trading только по реально активированным `TradingIdea`;
- Alembic-миграции с автоматическим обновлением распознанной старой схемы.

Fundamental/news/sector scores пока не загружаются из внешних источников. Поля и
веса для них предусмотрены, а при отсутствии данных technical score автоматически
перенормируется без изменения текущего поведения. News/sentiment — следующий
крупный модуль, а не фиктивный источник BUY/SELL.

## Архитектура

```text
MOEX ISS → Ingestion → Candles/Order book (DB)
                          ↓
Analysis → Scoring → Signal → TradingIdea Generator → Risk Manager
                                                    ↓
                                             Idea Repository
                                                    ↓
                                             Idea Tracker
                                               ↙          ↘
                                      Paper trading    Reporting → Telegram

Historical candles → тот же Signal/TradingIdea/Risk/Tracker pipeline → Backtest
```

Подробные границы модулей и инварианты описаны в
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Горизонты

| Горизонт | Timeframes и веса | Primary | Срок |
|---|---|---|---|
| `INTRADAY_1D` | 5m 15%, 15m 35%, 1h 30%, 4h 15%, 1d 5% | 15m | 1 день |
| `SWING_5D` | 1h 25%, 4h 35%, 1d 30%, 1w 10% | 4h | 5 дней |
| `POSITION_1M` | 4h 10%, 1d 55%, 1w 35% | 1d | 30 дней |

Это профили одного движка, а не три стратегии. Новый горизонт добавляется через
`HorizonProfile` без дублирования analysis/scoring/risk-кода.

## Быстрый старт

Требуется Python 3.11+.

```powershell
cd D:\moex_signal_bot
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

Укажите токен BotFather в `.env`:

```env
TELEGRAM_BOT_TOKEN=123456:replace_me
```

Команды приложения:

```powershell
python -m app migrate
python -m app ingest
python -m app run
python -m app backtest SBER SWING_5D
```

`run`, `ingest` и `backtest` сами выполняют Alembic upgrade. Отдельный `migrate`
удобен для deployment-проверки. Старые базы, созданные прежним `create_all`,
распознаются и принимаются под управление Alembic; неизвестная неполная схема
останавливает запуск с явной ошибкой вместо скрытого повреждения данных.

## Telegram

После `/start` доступны три основные кнопки. Дополнительные команды:

```text
/best
/ideas
/portfolio
/signal SBER 15m
/watchlist add SBER
/watchlist remove SBER
/watchlist
/settings
/settings frequency hourly|3h|daily|strong|off
/settings horizon 1d|5d|1m|all
/settings risk 0.5
/settings confidence 70
```

Пользовательская частота влияет только на доставку. Рыночный скан стартует
асинхронно сразу после запуска и затем выполняется по будням каждые
`INGESTION_INTERVAL_MINUTES` в торговое время. Отчётная задача проверяет
персональные фильтры отдельно каждые 5 минут.

## Scoring и риск

`TECHNICAL_SCORING_MODEL=legacy` оставлен default, чтобы обновление не меняло
существующие сигналы скрыто. Компонентный вариант включается явно:

```env
TECHNICAL_SCORING_MODEL=weighted
SCORE_WEIGHT_TREND=25
SCORE_WEIGHT_MOMENTUM=25
SCORE_WEIGHT_MACD=20
SCORE_WEIGHT_BOLLINGER=15
SCORE_WEIGHT_VOLUME=15
SIGNAL_THRESHOLD=25
```

TP/SL может быть ATR-based или level-based. При небезопасных уровнях движок
использует ATR fallback; идея ниже `MINIMUM_REWARD_RISK_RATIO` не публикуется.

```env
RISK_METHOD=levels
LEVEL_BUFFER_PCT=0.3
MINIMUM_REWARD_RISK_RATIO=2.0
DEFAULT_RISK_PER_TRADE_PCT=1.0
```

## Backtest и paper trading

Backtest читает уже сохранённые свечи и не скачивает всю историю повторно. Он
моделирует entry zone, активацию, TP/SL, expiry, commission, lot size и position
sizing. CLI сейчас печатает основные метрики JSON; объект результата также
содержит сделки и разбивки по ticker/horizon/timeframe/direction/sector/confidence.

`/portfolio` показывает общий forward paper account. Позиция создаётся только
после реальной активации опубликованной идеи и закрывается по тому же lifecycle,
сохраняя gross/net P&L, commission и R-multiple.

## MOEX ISS и order book

Публичный `iss.moex.com` предоставляет свечи без токена. L2 может требовать ISS+
и возвращать `denied`, поэтому стакан выключен по умолчанию и его сбой не мешает
OHLCV ingestion.

```env
MOEX_BASE_URL=https://apim.moex.com/iss
MOEX_API_TOKEN=your_iss_plus_token
ENABLE_ORDERBOOK=true
```

## Docker

```powershell
Copy-Item .env.example .env
# заполните TELEGRAM_BOT_TOKEN
docker compose up --build -d
docker compose logs -f moex-bot
```

Контейнер работает не от root, схема обновляется при запуске, SQLite хранится в
именованном volume. Для PostgreSQL задайте async DSN:

```env
DATABASE_URL=postgresql+asyncpg://moex:secret@postgres:5432/moex
```

## Проверки

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\ruff.exe format --check app tests migrations
.venv\Scripts\ruff.exe check app tests migrations
.venv\Scripts\python.exe -m compileall -q app migrations
.venv\Scripts\python.exe -m pip check
```

Аудит Claude-кандидата и решения по переносу находятся в
[`docs/CLAUDE_INTEGRATION_AUDIT.md`](docs/CLAUDE_INTEGRATION_AUDIT.md), история
изменений — в [`CHANGELOG.md`](CHANGELOG.md), лицензии — в
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

Документация MOEX: [AlgoPack / real-time market data](https://moexalgo.github.io/docs/description/realtime/).
