# Changelog

## Unreleased — Claude candidate integration

### Added

- SMA20/50/200, ADX, Stochastic, CCI, Bollinger Bands и OBV в единый technical
  analysis pipeline.
- Настраиваемый weighted scoring по trend/momentum/MACD/Bollinger/volume с
  автоматической нормализацией весов.
- Исправленное определение и кластеризация локальных support/resistance levels.
- Безопасный level-based TP/SL с проверкой направления, минимального R:R и ATR
  fallback.
- Lot-aware position sizing с risk budget и ограничением доступным капиталом.
- Синтетические интеграционные тесты полного пути candles → signal → DB.
- `.dockerignore` и ограничение размера Docker json logs.

### Changed

- Прежний scoring остаётся безопасным default; weighted-модель включается явно
  через `TECHNICAL_SCORING_MODEL=weighted`.
- Telegram `/settings` показывает активные scoring и TP/SL methods.
- Автоматические alert'ы используют индивидуальный риск каждого пользователя.
- Состав universe деактивирует только устаревшие инструменты и защищён от пустого
  ответа MOEX.

### Not ported

- Sync `apimoex` client, глобальные bot/scheduler objects, альтернативные
  SQLAlchemy models и `python-dotenv` config.
- Заявленные, но не реализованные fundamentals, news/sentiment, bonds,
  backtesting и paper trading.
- `backtrader_moexalgo`: в Claude-кандидате была только рекомендация в README.
