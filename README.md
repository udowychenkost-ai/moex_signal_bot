# MOEX Signal Bot

Асинхронный Telegram-бот торговых сигналов для акций Московской биржи. Сервис
получает данные через MOEX ISS, хранит рыночную историю и сигналы, рассчитывает
объяснимый технический score, BUY/SELL/HOLD и безопасные TP/SL.

> Сигналы носят информационный характер и не являются индивидуальной
> инвестиционной рекомендацией.

## Реализовано

- асинхронный MOEX ISS-клиент на `httpx`: retry/backoff, пагинация и ограничение
  конкурентных запросов;
- вселенная ликвидных акций TQBR с 1/2 эшелонами и деактивацией устаревшего
  состава;
- свечи M5, M15, H1, D1 и W1; M5/M15 агрегируются из M1;
- идемпотентная инкрементальная запись свечей и снимки 20 уровней стакана;
- единый набор индикаторов: SMA20/50/200, EMA20/50, MACD, ADX, RSI,
  Stochastic, CCI, Bollinger Bands, ATR, OBV и относительный объём;
- кластеризованные локальные уровни поддержки/сопротивления;
- настраиваемый компонентный score от −100 до +100 и режим совместимости
  `legacy`;
- BUY/SELL/HOLD, confidence и текстовое объяснение результата;
- TP/SL по ATR либо по уровням с проверкой направления и минимального R:R;
- расчёт размера позиции с учётом размера лота, риска и доступного капитала;
- Telegram-команды `/start`, `/signal`, `/watchlist`, `/settings`, `/portfolio`;
- один APScheduler для загрузки и персонализированных alert'ов по watchlist;
- SQLite по умолчанию и PostgreSQL через асинхронный DSN;
- Docker-запуск от непривилегированного пользователя и ротация container logs.

Фундаментальный анализ, новости/sentiment, облигации, полноценный backtesting и
paper trading пока не реализованы. `/portfolio` честно сообщает об этом; сервис
не выдаёт фиктивные позиции или P&L.

## Быстрый старт

Требуется Python 3.11+.

```powershell
cd D:\moex_signal_bot
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

Укажите токен, полученный у BotFather:

```env
TELEGRAM_BOT_TOKEN=123456:replace_me
```

Первичная загрузка без Telegram polling:

```powershell
python -m app ingest
```

Запуск бота:

```powershell
python -m app run
```

## Docker

```powershell
Copy-Item .env.example .env
# заполните TELEGRAM_BOT_TOKEN
docker compose up --build -d
docker compose logs -f moex-bot
```

Первичная загрузка в отдельном контейнере:

```powershell
docker compose run --rm moex-bot python -m app ingest
```

SQLite хранится в именованном volume `moex_bot_data`. Для PostgreSQL задайте
асинхронный DSN:

```env
DATABASE_URL=postgresql+asyncpg://moex:secret@postgres:5432/moex
```

Таблицы пока создаются через SQLAlchemy metadata. Перед изменением production-
схемы необходимо добавить Alembic: автоматическое `create_all` не заменяет
версионированные миграции.

## Scoring и риск

По умолчанию применяется `TECHNICAL_SCORING_MODEL=legacy`, чтобы обновление не
меняло ранее настроенные сигналы. Для новой компонентной модели явно задайте
`TECHNICAL_SCORING_MODEL=weighted`. Пять компонентов нормализуются до 100,
поэтому веса можно менять без ручного сохранения суммы:

```env
SCORE_WEIGHT_TREND=25
SCORE_WEIGHT_MOMENTUM=25
SCORE_WEIGHT_MACD=20
SCORE_WEIGHT_BOLLINGER=15
SCORE_WEIGHT_VOLUME=15
SIGNAL_THRESHOLD=25
```

`TECHNICAL_SCORING_MODEL=legacy` сохраняет прежнюю формулу и выбор уровней.
TP/SL настраивается через `RISK_METHOD=atr|levels`. При `levels` небезопасный или
недостаточный набор уровней автоматически откатывается к ATR; скрытой смены
направления сделки не происходит.

```env
RISK_METHOD=levels
LEVEL_BUFFER_PCT=0.3
MINIMUM_REWARD_RISK_RATIO=2.0
ATR_STOP_MULTIPLIER=1.5
ATR_TAKE_MULTIPLIER=3.0
```

## Планировщик и Telegram

Планировщик работает по будням с 10:00 до 18:59 в
`SCHEDULER_TIMEZONE` (по умолчанию `Europe/Moscow`). Частота задаётся
`INGESTION_INTERVAL_MINUTES`. Повторный BUY/SELL на той же свече не отправляется,
а процент риска берётся из настроек конкретного получателя.

```text
/signal SBER
/signal LKOH 1d
/watchlist add SBER
/watchlist remove SBER
/watchlist
/settings
/settings timeframe 1h
/settings risk 0.5
```

## MOEX ISS и стакан

Публичный `iss.moex.com` предоставляет свечи без токена. L2 endpoint может
требовать ISS+ и отвечать маркером `denied`, поэтому стакан по умолчанию выключен,
а его недоступность не мешает OHLCV ingestion.

```env
MOEX_BASE_URL=https://apim.moex.com/iss
MOEX_API_TOKEN=your_iss_plus_token
ENABLE_ORDERBOOK=true
```

## Проверки

```powershell
pytest -q
ruff check app tests
python -m compileall -q app
```

Технический аудит альтернативной реализации и принятые решения находятся в
[`docs/CLAUDE_INTEGRATION_AUDIT.md`](docs/CLAUDE_INTEGRATION_AUDIT.md). История
интеграции — в [`CHANGELOG.md`](CHANGELOG.md), сведения о сторонних компонентах —
в [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

Официальное описание market data: [MOEX AlgoPack / real-time market data](https://moexalgo.github.io/docs/description/realtime/).
