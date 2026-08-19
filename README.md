# MOEX Signal Bot — MVP

Изолированный Python-сервис Telegram-сигналов для акций Московской биржи. На первом этапе реализованы стабильный сбор данных из MOEX ISS и MVP-контур бота; существующий проект в корне репозитория не изменяется.

## Что уже работает

- загрузка и обновление списка акций режима TQBR;
- настраиваемая вселенная топ-20 и классификация 1/2 эшелона;
- свечи M5, M15, H1, D1 и W1 (M5/M15 агрегируются из M1);
- инкрементальная запись свечей с upsert последней незакрытой свечи;
- снимки 20 уровней стакана с хранением за последние 24 часа при наличии ISS+ доступа;
- SQLite по умолчанию, PostgreSQL через `DATABASE_URL`;
- RSI, MACD, EMA20/50, ATR, объёмный всплеск, поддержка/сопротивление;
- объяснимый скор от −100 до +100 и BUY/SELL/HOLD;
- ATR stop-loss/take-profit с R:R 1:2 и пользовательским риском на сделку;
- команды `/start`, `/signal`, `/watchlist`, `/settings`, `/portfolio`;
- фоновые обновления и алерты при новом BUY/SELL по watchlist;
- журнал всех сигналов в БД и тесты ключевой логики.

Фундаментал, облигации, графики, новости, бэктест и paper trading оставлены следующими вехами — текущая версия не имитирует их фиктивными данными.

## Быстрый старт

Требуется Python 3.11+.

```powershell
cd "D:\Социальная касса\moex_signal_bot"
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

Создайте бота через BotFather и запишите токен в `.env`:

```env
TELEGRAM_BOT_TOKEN=123456:replace_me
```

Первичная загрузка без запуска Telegram polling:

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

Для отдельной первичной загрузки:

```powershell
docker compose run --rm moex-bot python -m app ingest
```

## PostgreSQL

Поддержка уже включена. Укажите асинхронный DSN:

```env
DATABASE_URL=postgresql+asyncpg://moex:secret@postgres:5432/moex
```

Для production следует добавить Alembic-миграции и отдельный PostgreSQL-сервис/managed database. Автосоздание таблиц предназначено для MVP.

## Команды

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

Планировщик запускается по будням с 10:00 до 18:59 по Москве. Частота задаётся `INGESTION_INTERVAL_MINUTES`. Один и тот же сигнал на одной и той же свече повторно не отправляется.

### Стакан и ISS+

На публичном `iss.moex.com` свечи доступны без ключа, но endpoint L2 сейчас может отвечать `X-MicexPassport-Marker: denied`. Поэтому стакан по умолчанию выключен и сбой L2 не мешает сохранять OHLCV. При наличии доступа задайте:

```env
MOEX_BASE_URL=https://apim.moex.com/iss
MOEX_API_TOKEN=your_iss_plus_token
ENABLE_ORDERBOOK=true
```

## Конфигурация эшелонов

ISS не всегда возвращает free-float в основном срезе инструментов. Поэтому MVP использует два прозрачных правила:

1. тикеры из `BLUE_CHIP_TICKERS` относятся к первому эшелону;
2. для остальных применяются пороги капитализации, оборота и free-float, когда все метрики доступны; ликвидные бумаги ниже первого порога попадают во второй эшелон.

Текущий `VALTODAY` используется как оперативный показатель оборота. Расчёт настоящего среднего дневного оборота по истории — ближайшее расширение Модуля 1.

## Тесты и линтер

```powershell
pytest
ruff check app tests
```

## Ограничения MVP

- Публичный ISS может отдавать задержанные или неполные данные вне торговой сессии.
- Сигналы — детерминированный технический скоринг, не прогноз и не рекомендация.
- `/portfolio` пока сообщает статус будущего paper-trading модуля.
- Публичный REST-стакан подходит для мониторинга, но не заменяет брокерский L2 для исполнения.

Официальная справка по полям свечей и стакана: [MOEX ALGOPACK / real-time market data](https://moexalgo.github.io/docs/description/realtime/).
