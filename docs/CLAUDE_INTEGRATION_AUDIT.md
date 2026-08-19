# Технический аудит текущей и Claude-реализаций

Дата аудита: 2026-08-19. Исходная текущая версия зафиксирована коммитом
`f280412`; альтернативная версия получена из `moex_signal_bot_1.zip`. До
интеграции текущая версия проходила 14 тестов и Ruff, Claude-версия — 12 тестов,
но имела 12 замечаний Ruff. Живой запрос Claude-клиента вернул 334 дневные свечи
SBER и 506 инструментов; ранее был отдельно проверен и текущий MOEX-клиент.

## Сопоставление

| MODULE | CURRENT CODEX | CLAUDE VERSION | WHICH IS BETTER | WHAT TO KEEP | MERGE PLAN |
|---|---|---|---|---|---|
| Архитектура | Асинхронные сервисы, dependency injection, доменные DTO, repositories | Sync-модули с глобальными bot/scheduler/session объектами | Current | Текущие границы сервисов и один composition root | Claude-модули целиком не переносить |
| MOEX ISS / AlgoPack | `httpx`, retry/backoff, пагинация, concurrency, M1→M5/M15, ISS+ L2, инкрементальная загрузка | `apimoex`, sync-вызовы, базовые candles/securities, без L2 и устойчивого pagination-контроля | Current | Текущий `MoexClient` и ingestion | `apimoex` не добавлять; исправить stale universe |
| Технический анализ | RSI/MACD/EMA/ATR/volume/S-R | Дополнительно SMA20/50/200, ADX, Stochastic, CCI, Bollinger, OBV | Claude по широте, Current по интеграции | Один текущий pipeline и полезные индикаторы Claude | Добавить `ta`, расширить единый `analysis.py` |
| Уровни S/R | Простой rolling min/max | Локальные уровни и clustering, но extrema-алгоритм сравнивал не все соседние точки | Смешанный | Идею кластеризации, не дефектный код | Реализовать centered extrema + cluster заново |
| Scoring engine | Стабильный legacy score с объяснениями | Взвешенные trend/momentum/MACD/Bollinger/volume компоненты | Claude по расширяемости | Компонентную модель и старый режим совместимости | Настраиваемые нормализуемые веса + `legacy` |
| BUY / SELL / HOLD | Пороговый результат, журналируется | Аналогично, но отдельная модель результата | Current | Текущий `GeneratedSignal` и `SignalRecord` | Подключить новый score без второй модели |
| TP / SL | Надёжный ATR 1:2 | ATR и S/R; S/R мог пересечь entry или нарушить R:R | Смешанный | ATR fallback и идею level method | Валидировать направление/R:R, иначе ATR |
| Risk management | Пользовательский %, базовый position size | Lot-aware sizing и cash cap | Claude функциональнее | Одну risk-модель и совместимый старый API | Добавить детальный `PositionSize` |
| Telegram bot | Async aiogram 3, обработка ошибок, user settings, timeframe | Global Bot на import, менее полные команды и error handling | Current | Текущий router/DI | Показать выбранные scoring/risk в `/settings` |
| Scheduler | Один AsyncIOScheduler, dedup на candle, контролируемый lifecycle | Второй global scheduler, sync network внутри event loop | Current | Текущий scheduler | Не переносить; персонализировать риск alert'ов |
| PostgreSQL | Async SQLAlchemy, `asyncpg`, dialect-specific upsert | Sync SQLAlchemy; заявлен PostgreSQL, но драйвер не указан и SQLite-типы/flows доминируют | Current | Текущую модель данных | Не создавать параллельные таблицы |
| Candles / order flow | Persistent candles + order-book levels и retention | Candles загружаются на запрос, order flow отсутствует | Current | Текущие сущности и foreign keys | Без второй candle-модели |
| Fundamental | Явно не реализован, поле score зарезервировано | Не реализован | Паритет | Честный нулевой placeholder в записи | Не имитировать данные |
| News / sentiment | Не реализовано | Не реализовано | Паритет | Ничего | Отдельная будущая интеграция |
| Bonds | Не реализовано | Не реализовано | Паритет | Ничего | Нужна отдельная доменная модель |
| Backtesting | История сигналов пригодна как вход, движка нет | Нет; README лишь рекомендует сторонний проект | Current немного готовее | Журнал сигналов | Не добавлять зависимость без отдельного дизайна |
| Paper trading | Команда честно сообщает, что модуля нет | Поля/ответы создавали впечатление функциональности без P&L lifecycle | Current | Явное ограничение | Не переносить фиктивный portfolio |
| Config / `.env` | Typed Pydantic Settings, validators, secrets не коммитятся | `python-dotenv`, module constants | Current | Typed config | Добавить только scoring/risk параметры |
| Docker | Slim image, non-root user, named volume | Root container и лишний gcc; полезны `.dockerignore` и log rotation | Current | Non-root image | Перенести ignore и log limits |
| Tests | 14 baseline: MOEX pagination/resampling, DB upsert, analysis/risk | 12 synthetic indicator/scoring/E2E tests | Смешанный | Оба набора сценариев в текущей структуре | Добавить expanded TA, S/R, risk и E2E tests |
| Security | BigInteger Telegram ID, token проверяется при run, запреты полного deactivation | Integer Telegram ID, import-time token use, sync globals, нет уникальности watchlist | Current | Текущие ограничения/уникальные ключи | Claude DB/bot не переносить |

## Только в исходной текущей версии

- retry/backoff, пагинация ISS, concurrency limit и API-token routing;
- агрегация минутных свечей, инкрементальный overlap и upsert открытой свечи;
- persistent instruments/candles/order book, retention L2;
- полноценный async lifecycle, user settings, timeframe, alert dedup;
- реальная async PostgreSQL-конфигурация и BigInteger Telegram IDs.

## Только в Claude-версии до интеграции

- SMA200, ADX, Stochastic, CCI, Bollinger Bands и OBV;
- компонентный weighted scoring;
- level-based TP/SL и подробный lot-aware position sizing;
- синтетический end-to-end тест signal engine;
- `.dockerignore` и лимиты container logs.

## Дублирование и конфликты

Кандидат дублировал MOEX client, модели User/Watchlist/Signal, bot, scheduler,
signal engine, config и risk engine. Их одновременное сохранение означало бы две
модели одной сущности, два lifecycle scheduler'а и разные sync/async DB/API
контракты. Поэтому полезные алгоритмические идеи перенесены внутрь существующих
границ, а альтернативные сервисы не включены.

Различия зависимостей: кандидат добавлял `apimoex`, `ta` и `python-dotenv`, но не
имел `httpx`, `aiosqlite`, `asyncpg`, `pydantic-settings`, `pytest-asyncio` и Ruff.
В итог оставлен только `ta`; остальные функции уже надёжнее покрываются текущим
стеком.

## Найденные риски и исправления

- исправлен алгоритм локальных extrema Claude-кандидата;
- level TP/SL теперь отклоняет уровни с неправильной стороны entry и R:R ниже
  заданного минимума, затем безопасно использует ATR;
- пустой/отфильтрованный ответ universe не деактивирует все инструменты;
- инструменты, выпавшие из актуальной вселенной, теперь помечаются inactive;
- scheduler форматирует риск отдельно для каждого Telegram-пользователя;
- `session_scope` стал настоящим async context manager;
- не перенесены import-time Bot, sync I/O в event loop и альтернативные DB tables.

Alembic и автоматическое принятие распознанных legacy-схем были добавлены на
следующем этапе roadmap. Индикаторные веса и порог остаются инженерными
начальными значениями и должны быть откалиброваны на out-of-sample backtest до
использования с реальным капиталом.

## Проверка происхождения и лицензий

В коде кандидата найдены импорты `apimoex` и `ta`, а в README — рекомендация
`backtrader_moexalgo`. Поиск уникальных имён функций и фраз не выявил совпадений
исходного кода с индексируемыми GitHub-проектами; файлов лицензий или vendored
third-party source в архиве не было. Следовательно, обнаружено использование API
библиотек и общих подходов, но не копирование сторонних исходников.

- `apimoex` — Unlicense; проверялся, но в итоговый runtime не включён;
- `ta` — MIT; это единственная добавленная runtime-зависимость;
- `backtrader_moexalgo` — MIT; только ссылка в README кандидата, код и зависимость
  не переносились.

Сведения о реально поставляемой зависимости находятся в
`THIRD_PARTY_NOTICES.md`.

## Выполненный план интеграции

1. Расширить единый TA pipeline и исправить clustered S/R.
2. Добавить weighted scoring с typed weights и режимом `legacy`.
3. Добавить безопасный level TP/SL, ATR fallback и lot-aware sizing.
4. Подключить всё к одному `SignalService`, исправить universe и user-risk alerts.
5. Усилить Docker-конфигурацию, тесты и документацию.
6. Удалить `_claude_candidate`, прогнать полный verification gate.

## Верификация merge checkpoint до TradingIdea roadmap

- `pytest`: 30 passed;
- Ruff lint и format check: passed;
- `compileall`, импорт всех application modules и `pip check`: passed;
- wheel build: passed;
- DDL всех шести таблиц скомпилирован для SQLite и PostgreSQL;
- CLI startup (`python -m app --help`): passed;
- live MOEX smoke: 1 инструмент, 456 D1 свечей, 0 ошибок;
- Compose YAML разобран отдельным YAML parser без ошибок;
- реальный `docker build` не запускался: Docker CLI отсутствует на машине аудита.

Type checker в проекте не настроен. Telegram polling не запускался без реального
`TELEGRAM_BOT_TOKEN`, чтобы не выполнять внешние действия от имени пользователя.

## Состояние после TradingIdea roadmap

После завершения merge поверх сохранённой архитектуры добавлены одна модель
`TradingIdea`, три конфигурационных horizon profile, entry zone, lifecycle
tracker, Telegram reports/settings, deduplication, production-pipeline backtest,
paper trading и Alembic revisions `20260819_0001`–`20260819_0006`.

История больше не скачивается целиком при каждом анализе: существующий async
MOEX client и repository layer используют последний timestamp, небольшой overlap
и idempotent upsert. Redis и второй cache layer не добавлялись. Backtrader также
не добавлялся: отдельный framework потребовал бы дублировать или адаптировать
production strategy, тогда как новый backtest напрямую вызывает те же чистые
signal/idea/risk/lifecycle-функции.

Sentiment и внешние fundamentals сознательно отложены, как требовал roadmap.
При этом в единой таблице и scoring contract уже присутствуют factor columns и
веса, поэтому подключение providers не потребует второй модели или engine.
