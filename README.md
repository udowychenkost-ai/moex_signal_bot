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
- Telegram-меню из шести разделов и V2.1 contextual inline UX: stateful
  watch/follow actions, idea/instrument/lifecycle drill-down, quick actions and
  pagination без удаления Reply Keyboard или slash-команд;
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

## INTRADAY V2.4 safety layer

Version `0.7.0` completes the isolated `intraday_v2_4` application layer:
one live/shadow orchestrator now composes the existing MTF, Data Integrity,
Data SLA, microstructure, liquidity, cost, risk, calibration, adversarial and
Final Audit services. Its initial `DecisionSnapshotV24` and policy-version
references are immutable; MODEL and explicitly confirmed ACTUAL trades remain
separate and later changes use append-only events. Candidate claims and the
Telegram outbox make scheduler retries idempotent. Alembic head is
`20260902_0021`.

The safe deployment default is:

```env
ENABLE_LEGACY_STRATEGY=true
INTRADAY_V24_ENABLED=false
INTRADAY_V24_SHADOW_ENABLED=false
INTRADAY_V24_STRATEGY_VERSION=intraday_v2_4
INTRADAY_V24_MAX_HOLDING_TRADING_DAYS=2
INTRADAY_V24_LEVERAGE_ENABLED=false
```

`ENABLE_LEGACY_STRATEGY` controls the existing V1/V2 scanner.
`INTRADAY_V24_ENABLED` controls V2.4 production publication, while
`INTRADAY_V24_SHADOW_ENABLED` may run the same fail-closed engine and collect
journals/model observations without publishing recommendations. Both engines
use one APScheduler and failure-isolated jobs.

Every Telegram user has a persistent `analysis_mode`: `LEGACY_ONLY` (migration
and new-user default), `INTRADAY_V24_ONLY`, or `BOTH`. It controls delivery and
presentation, not engine execution or ownership of historical records. In
`BOTH`, statistics stay in separate Classic/Intraday sections; opposite
directions are displayed independently with a conflict warning. Open ACTUAL
V2.4 positions remain visible even after a user switches away from Intraday.

An authorized ID from `TELEGRAM_ADMIN_CHAT_IDS` can open `/riskpolicy` or
Settings → Risk Budget, review the effective policy, enter all existing risk
domain fields, preview, and explicitly confirm a new effective-dated version.
Previous versions and the version referenced by historical snapshots are never
overwritten.

Production remains deliberately disabled. The bundled public source does not
provide the verified event/news context, full reliable L2, tick/session and
borrow facts required by the deterministic gates; Data SLA, liquidity, cost,
risk and opportunity policies also require operator-approved values, and the
strategy is uncalibrated. Missing data remains `DATA_NOT_AVAILABLE` and cannot
be converted to a pass by Gemini.

V2.4 documentation:

- [`docs/MASTER_PROMPT_V2_4_GAP_ANALYSIS.md`](docs/MASTER_PROMPT_V2_4_GAP_ANALYSIS.md)
- [`docs/INTRADAY_V2_4_ARCHITECTURE.md`](docs/INTRADAY_V2_4_ARCHITECTURE.md)
- [`docs/JOURNAL_SCHEMA.md`](docs/JOURNAL_SCHEMA.md)
- [`docs/DATA_SLA.md`](docs/DATA_SLA.md)
- [`docs/RISK_BUDGET.md`](docs/RISK_BUDGET.md)
- [`docs/CALIBRATION.md`](docs/CALIBRATION.md)
- [`docs/FINAL_AUDIT.md`](docs/FINAL_AUDIT.md)
- [`docs/INTRADAY_V2_4_PRODUCTION_CHECKLIST.md`](docs/INTRADAY_V2_4_PRODUCTION_CHECKLIST.md)

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
[`docs/V2_QUALITY_AI.md`](docs/V2_QUALITY_AI.md). Карта Telegram callback UX и
текстовые примеры находятся в [`docs/V2_1_INLINE_UX.md`](docs/V2_1_INLINE_UX.md).

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
AI_MODEL=gemini-3.6-flash
AI_FALLBACK_MODEL=gemini-flash-lite-latest
AI_MAX_OUTPUT_TOKENS=4096
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
notification preferences меняются кнопками. Под карточками идей, lifecycle
alerts, акциями, top-3, статистикой, рынком и status размещены контекстные
inline-кнопки. Watch/follow хранится в PostgreSQL, повторное нажатие идемпотентно,
а состояние кнопки меняется edit-in-place. Watchlist, signal history, active
ideas и результаты выводятся страницами по пять записей. Slash-команды и нижняя
Reply Keyboard остаются fallback:

Главная Reply Keyboard содержит прямые действия «Отслеживаемые», «Результаты
сигналов», «Рынок сейчас» и «Проверить акцию». Последняя запрашивает тикер через
ForceReply, поэтому `/signal` вводить необязательно. У исторической идеи без
creation-time AI review показывается компактная причина и кнопка
«Проанализировать сейчас». Она строит новый current candidate, выполняет Gemini
review и маркирует результат как текущий; сохранённые verdict/snapshot старой
идеи не изменяются.

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

### Ликвидность исполнения (V2.1.5)

Карточка идеи отдельно показывает текущий/последний оборот, средний рублёвый
оборот завершённых дневных свечей за 20 торговых дней, оценку ликвидности и
ориентировочный комфортный размер по ликвидности. Это read-only UX-слой: он не
меняет стратегический position sizing, QualityGate, AI verdict, TP/SL или
lifecycle.

Размер ограничивается долей ADV20 и, только при свежем стакане, 10% более тонкой
из direction-aware сторон входа/выхода в диапазоне ±0.5%. `QUANTITY` стакана
считается в лотах и переводится в RUB notional через `price × quantity ×
lot_size`. Затем применяются существующий класс волатильности и spread modifier;
результат округляется вниз. Устаревший стакан явно помечается и исключается из
расчёта. Кнопка `💧 Ликвидность` открывает подробные диапазоны ±0.25%, ±0.50% и
±1.00% без отдельного MOEX-запроса на каждое открытие карточки.

Default provider — Gemini `gemini-3.6-flash`. При timeout, rate limit,
`MODEL_NOT_FOUND`, unsupported model или временной недоступности primary
выполняется один запрос к
`gemini-flash-lite-latest`. Для `AIAnalysis` используется бюджет 4096 output
tokens и официально поддерживаемый Gemini 3.6 `thinkingLevel=minimal`.
Truncated/malformed/schema-invalid JSON получает один compact retry на primary;
если он также невалиден — ровно один запрос к fallback. Если все попытки
неуспешны, отсутствует
`GEMINI_API_KEY` или обе модели недоступны, результат —
`AI_NOT_REVIEWED / WAIT`: candidate остаётся в research cohort, но
`TradingIdea` не публикуется. Fallback без успешного AI review по умолчанию
выключен. `AI score` — рейтинг анализа 0–100, не статистическая вероятность
успеха. Для альтернативного OpenAI provider задайте `AI_PROVIDER=openai`,
совместимый `AI_MODEL` и `OPENAI_API_KEY`.

Structured validation errors имеют отдельный код
`INVALID_STRUCTURED_RESPONSE`. Каждая попытка сохраняется отдельно с этапом
`PRIMARY`, `PRIMARY_STRUCTURED_RETRY` или `FALLBACK`; raw model text в БД и логи
не записывается.

Все пользовательские описательные поля Gemini возвращаются на русском языке;
тикеры, числа и технические обозначения вроде RSI, EMA20, BUY, SELL, IMOEX и R:R
остаются без перевода. Преимущественно англоязычный ответ получает код
`LANGUAGE_MISMATCH` и один повторный structured-запрос на primary с явной
инструкцией Russian only (`PRIMARY_LANGUAGE_RETRY`). Если исправить язык не
удалось, действует прежняя fallback/fail-closed политика, поэтому английский
текст пользователю не публикуется.

При старте Gemini сначала сверяется с `v1beta/models`, затем для каждой модели
выполняется минимальный structured `generateContent` probe. Наличие в ListModels
означает только `LISTED`; доступной модель считается только при `CALLABLE=YES`.
Primary unavailable при доступном fallback даёт `DEGRADED`, обе недоступные
модели — `ERROR`; market ingestion и scheduler при этом продолжают работать.
Кнопка `🧠 Gemini` показывает доступность моделей и статистику запросов без
секретов, а `📈 Последний scan` — полный QualityGate/AI/publication funnel.
Немедленная ручная проверка без ожидания сигнала:

```bash
python -m app gemini-health
```

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

Публичный `iss.moex.com` предоставляет свечи и задержанные best bid/offer без
токена. Полный L2 `/orderbook` является подписочным продуктом; в публичном
`marketdata` поля `BIDDEPTH/OFFERDEPTH` обычно отсутствуют. Бот не симулирует
глубину: сохраняет реальный level-1 spread, а неизвестное количество — как
`NULL`. Если подписочный endpoint возвращает глубину, `QUANTITY` хранится в
лотах и переводится в рубли только как `price × quantity × lot_size`.

Сбор использует один отфильтрованный batch только по active monitored universe,
работает отдельным scheduler job каждые две минуты и транзакционно заменяет
предыдущий snapshot только после получения и проверки нового. Ошибка одного
инструмента не останавливает свечи, scanning, lifecycle или Telegram.

```env
MOEX_BASE_URL=https://iss.moex.com/iss
ENABLE_ORDERBOOK=true
ORDERBOOK_INTERVAL_MINUTES=2
ORDERBOOK_REQUEST_CONCURRENCY=5
```

Ручная диагностика без запуска scheduler:

```bash
python -m app ingest-orderbook
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
