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
- детерминированный BUY/SELL/HOLD и совместимые `legacy`/`weighted`/`contextual`
  scoring modes; contextual-модель хранит trend/momentum/extreme/volume/levels/
  volatility/relative-strength/regime components;
- обязательный IMOEX market context с BULL/BEAR/SIDEWAYS, causal volatility
  state, drawdown, ATR и relative strength бумаги к индексу;
- сменный `FundamentalDataProvider`, point-in-time отчёты и sector-relative
  valuation/profitability/debt/growth/cashflow/dividend scoring без look-ahead;
- отдельная доменная модель `TradingIdea` с горизонтами 1 день, 5 дней и 1 месяц;
- диапазон входа, ATR/level TP/SL, минимальный R:R и lot-aware sizing;
- lifecycle `PENDING_ENTRY → ACTIVE → TP_HIT/SL_HIT/EXPIRED`, включая
  `CANCELLED` и `INVALIDATED`, с полной историей событий;
- защита от ложного результата: идея не считается активированной или выигрышной,
  если цена не вошла в entry zone;
- V2 `QualityGate`: hard thresholds, liquidity/volume, multi-timeframe and
  independent confirmations, explicit conflicts, regime overrides and R:R;
- batch ranking по `final_quality_score`, configurable top-N/day limits и
  cooldown для `ticker + horizon + direction`;
- fail-closed Gemini second opinion только для `PASS` candidates, строгий JSON
  schema, provider/model/usage/cost/latency/error/fallback telemetry и запрет
  LLM «спасать» REJECT; OpenAI сохранён как альтернативный provider;
- frozen `candidate_experiments` для quant/AI approved/rejected cohorts и
  отдельный lifecycle фактического результата даже для неопубликованных идей;
- Telegram-меню из шести разделов, button-only настройки, V2-карточка и экраны
  AI/technical/fundamental/market/lifecycle;
- пользовательские фильтры: частота, горизонт, риск, сила, AI и типы уведомлений;
- один APScheduler с независимыми ingestion, scanning, lifecycle, reporting и
  daily-summary jobs;
- event-outbox deduplication: новая идея и каждый lifecycle-переход доставляются
  не более одного раза на Telegram chat;
- data-freshness guard и работа только по завершённым decision/lifecycle свечам;
- неизменяемый decision-time snapshot факторов, индикаторов, ATR и уровней;
- historical backtest на том же signal/idea/risk/lifecycle pipeline;
- leakage-safe research pipeline с TRAIN/VALIDATION/OOS, walk-forward,
  legacy/weighted comparison и простыми benchmark-стратегиями;
- forward paper trading только по реально активированным `TradingIdea`;
- Alembic-миграции с автоматическим обновлением распознанной старой схемы.
- эксплуатационные `/status`, `/stats`, `/ideas`, `/idea ID` и startup recovery.

Официальный публичный normalized fundamental API в проект не выдумывается.
Проверенные факты импортируются из `fundamentals/official.json` вместе с
`publication_date`, `available_from`, источником и URL. Пока файл пуст или
sector peer coverage недостаточен, fundamental factor честно помечается «нет
данных», исключается из суммы, а доступные веса перенормируются.

## Архитектура

```text
MOEX ISS → Ingestion → Stock + IMOEX candles (DB)
                          ↓                 ↓
Analysis → Scoring ← MarketRegime/RelativeStrength → Quant candidate → Risk
                 ↖ Point-in-time Fundamentals
                                                    ↓ QualityGate
                                          Candidate experiment (frozen)
                                                    ↓ PASS only
                                           AI structured second opinion
                                                    ↓ APPROVE only
                                             Idea Repository
                                                    ↓
                                             Idea Tracker
                                               ↙          ↘
                                      Paper trading    Reporting → Telegram

Historical candles → тот же Signal/TradingIdea/Risk/Tracker pipeline → Backtest
```

Подробные границы модулей и инварианты описаны в
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). Отдельный V2 decision record с
QualityGate, AI-контрактом, OOS-цифрами и ограничениями находится в
[`docs/V2_QUALITY_AI.md`](docs/V2_QUALITY_AI.md).

## Горизонты

| Горизонт | Timeframes и веса | Primary | Срок |
|---|---|---|---|
| `INTRADAY_1D` | 5m 15%, 15m 35%, 1h 30%, 4h 15%, 1d 5% | 15m | 1 день |
| `SWING_5D` | 1h 25%, 4h 35%, 1d 30%, 1w 10% | 4h | 5 дней |
| `POSITION_1M` | 4h 10%, 1d 55%, 1w 35% | 1d | 30 дней |

LIVE OBSERVATION policy зафиксирована отдельно от стратегии:

| Горизонт | Режим | Учёт результата |
|---|---|---|
| `INTRADAY_1D` | `RESEARCH` | lifecycle и R, без paper P&L |
| `SWING_5D` | `RESEARCH` | lifecycle и R, без paper P&L |
| `POSITION_1M` | `PAPER` | lot-aware simulated P&L с costs |

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
GEMINI_API_KEY=replace_me
AI_PROVIDER=gemini
AI_MODEL=gemini-2.5-flash
AI_FALLBACK_MODEL=gemini-2.5-flash-lite
```

Команды приложения:

```powershell
python -m app migrate
python -m app ingest
python -m app run
python -m app backtest SBER SWING_5D
python -m app.research ingest --date-to 2026-08-18
python -m app.research run
python -m app.research ablation --horizons POSITION_1M SWING_5D
python -m app.research quality-v2
```

`run`, `ingest` и `backtest` сами выполняют Alembic upgrade. Отдельный `migrate`
удобен для deployment-проверки. Старые базы, созданные прежним `create_all`,
распознаются и принимаются под управление Alembic; неизвестная неполная схема
останавливает запуск с явной ошибкой вместо скрытого повреждения данных.

## Telegram

После `/start` доступны «Лучшие идеи», «Активные идеи», «Статистика»,
«Настройки», «Анализ рынка» и «Статус системы». Все основные фильтры и
notification preferences меняются кнопками. Slash-команды остаются fallback:

```text
/best
/ideas
/idea 123
/status
/stats
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

Ingestion, scanning, lifecycle/paper и Telegram dispatch запускаются независимо.
При недоступном MOEX старые свечи могут оставаться в БД, но freshness guard не
даёт создать из них новую `TradingIdea`; `/status` показывает stale timeframes.
После рестарта tracker продолжает все сохранённые pending/active идеи, а
notification outbox не отправляет уже доставленные события повторно.

## Scoring и риск

V2 не меняет математический score задним числом. После существующего quant
candidate применяется отдельный `QualityGate`; только `PASS` попадает в AI.
Число независимых подтверждений задаётся по горизонту: `1D=4` остаётся
research-only, а leakage-safe TRAIN/VALIDATION выбрали `5D=5` и `1M=5` до
проверки на отдельном unseen TEST. AI
получает только structured snapshot, не свечи и не внешний news context.

Default provider — Gemini `gemini-2.5-flash`. Только при timeout, rate limit или
временной недоступности primary выполняется один запрос к
`gemini-2.5-flash-lite`. Если обе модели недоступны, отсутствует
`GEMINI_API_KEY`, ответ повреждён или не соответствует schema, результат —
`AI_NOT_REVIEWED / WAIT`: candidate остаётся в research cohort, но
`TradingIdea` не публикуется. Fallback без успешного AI review по умолчанию
выключен. `AI score` — рейтинг анализа 0–100, не статистическая вероятность
успеха. Для альтернативного OpenAI provider задайте `AI_PROVIDER=openai`,
совместимый `AI_MODEL` и `OPENAI_API_KEY`.

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

`TECHNICAL_SCORING_MODEL=contextual` использует восемь весов из выбранного
`HorizonProfile`; они не зашиты в engine. Перепроданность даёт положительный
`momentum_extreme_score` только при подтверждённой стабилизации/восстановлении,
а BEAR regime дополнительно подавляет ложный mean-reversion BUY.

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
моделирует entry zone, активацию не раньше следующей свечи, TP/SL, expiry,
commission, раздельный BUY/SELL slippage, lot size и position sizing. Окна
`[start, end)` не пересекаются, а signal/ATR/S/R получают только уже закрытые к
моменту решения свечи. Результат содержит gross/commission/slippage/net,
expectancy, R, drawdown, Sharpe и разбивки по ticker/horizon/timeframe/direction/
confidence/year.

Отдельный research dataset задаётся в `research.toml` и хранится в gitignored
`data/research.db`. Команда `app.research run` использует заранее объявленную
ограниченную сетку: TRAIN формирует shortlist, VALIDATION выбирает конфигурацию,
OOS TEST не участвует в выборе. Результаты сохраняются в
[`reports/backtests`](reports/backtests), включая `BACKTEST_REPORT.md`, fixed
legacy/weighted baselines, calibration summary, OOS и walk-forward.

`app.research ablation` отдельно фиксирует A/B/C/F market-context варианты.
D/E, требующие fundamentals, получают статус `not_evaluable`, если в dataset
нет реального point-in-time coverage; нулевые/синтетические ratios не
подставляются.

`app.research quality-v2` калибрует число confirmations только на
TRAIN/VALIDATION и формирует `V2_QUALITY_COMPARISON.md/json`. Historical AI
replay намеренно не выполняется: вклад AI измеряется forward таблицей
`candidate_experiments` (`ALL QUANT` / `AI APPROVED` / `AI REJECTED`).

На полном фиксированном universe из 20 бумаг V2 QualityGate сократил unseen
TEST volume для SWING с 2 016 до 406 идей (−79,86%), а для POSITION — с 723 до
207 (−71,37%). POSITION улучшил PF `1,139→1,297` и expectancy
`0,064R→0,166R`; SWING PF вырос только `0,886→0,901`, expectancy осталась
отрицательной `−0,077R→−0,071R`, поэтому 5D сохраняет статус RESEARCH. Текущий
legacy INTRADAY selector не создал OOS-кандидатов, и из него нельзя делать вывод
о качестве фильтра. Полный результат и ограничения:
[`V2_QUALITY_COMPARISON.md`](reports/backtests/V2_QUALITY_COMPARISON.md).

Финальный ablation не изменил production selectors. Для POSITION full-context
улучшил OOS/WF expectancy, но увеличил OOS max drawdown с 2.44% до 3.47%; regime
alone был нестабилен, а fundamental coverage равен 0/20. Для SWING full-context
остался отрицательным (OOS PF 0.926, −0.044R; WF PF 0.982, −0.009R). Поэтому
INTRADAY/SWING/POSITION сохраняют `legacy`, а новые факторы продолжают
сохраняться для forward-аудита без скрытого изменения сигналов.

Зафиксированный прогон до `2026-08-18` использовал 20 акций и 1 722 488 свечей.
Результат после commission/slippage:

| Горизонт | Selected config | OOS PF | Expectancy | Net P&L | Вывод |
|---|---|---:|---:|---:|---|
| 1 день | `weighted_trend_context` | 0.51 | -0.46 R | -55 329.76 ₽ | reject |
| 5 дней | `weighted_trend_context` | 1.004 | ≈0 R | +586.24 ₽ | экономически нулевой |
| 1 месяц | `legacy_default` | 1.145 | +0.067 R | +20 969.06 ₽ | только forward paper |

Полные splits, fixed-model comparisons, ticker/direction/confidence breakdowns и
walk-forward находятся в
[`BACKTEST_REPORT.md`](reports/backtests/BACKTEST_REPORT.md). Production default
автоматически не переключается по результату исследования. `Maximum drawdown`
в текущем отчёте рассчитан по realised equity после закрытий, не по intratrade
mark-to-market.

`/portfolio` показывает общий forward paper account. Позиция создаётся только
для `POSITION_1M=PAPER` после активации опубликованной идеи и закрывается по тому же lifecycle,
сохраняя reference/fill prices, gross P&L, commission, slippage, net P&L и
R-multiple. В проекте нет broker execution adapter: paper-контур не может
разместить реальную заявку.

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
# заполните Telegram и PostgreSQL secrets
$env:GIT_COMMIT = git rev-parse --short HEAD
docker compose up --build -d
docker compose logs -f app
```

Контейнер работает не от root; PostgreSQL хранится в persistent named volume,
имеет healthcheck, а Alembic обновляет схему до запуска polling. Полная инструкция
для Ubuntu VPS, update/redeploy и backup/restore: [`DEPLOY.md`](DEPLOY.md).

```env
DATABASE_URL=postgresql+asyncpg://moex:secret@postgres:5432/moex
```

Для дополнительного staging-контура используйте `.env.staging.example` и
`docker-compose.staging.yml`. Пошаговая приёмка описана в
[`docs/STAGING_CHECKLIST.md`](docs/STAGING_CHECKLIST.md). Если Docker CLI или
PostgreSQL на рабочей машине отсутствуют, runtime-проверка остаётся внешним
deployment check и не считается выполненной локально.

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
 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md). Финальная аналитическая
граница market context/fundamentals описана в
[`docs/MARKET_CONTEXT_FUNDAMENTALS.md`](docs/MARKET_CONTEXT_FUNDAMENTALS.md).

Документация MOEX: [AlgoPack / real-time market data](https://moexalgo.github.io/docs/description/realtime/).
