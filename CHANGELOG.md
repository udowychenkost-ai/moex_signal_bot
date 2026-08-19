# Changelog

## Unreleased — unified TradingIdea platform

### Added

- Separate `TradingIdea` domain model, ORM schema and event history; an internal
  `Signal` is no longer exposed as a complete user trade setup.
- Config-driven `INTRADAY_1D`, `SWING_5D` and `POSITION_1M` horizon profiles
  using one analysis/scoring/idea/risk pipeline.
- ATR/support-resistance entry zones with WAITING/ENTRY_AVAILABLE/MISSED logic.
- Persistent lifecycle tracking for activation, TP, SL, expiry, cancellation and
  invalidation, including conservative same-candle TP/SL resolution.
- Compact Telegram idea cards, detail callbacks, grouped best-idea reports and a
  three-item main menu.
- User preferences for report frequency, idea horizon, risk and minimum
  confidence.
- Independent `market_scan` and `idea_reporting` jobs in one scheduler.
- Per-user `(idea, version)` notification deduplication and material-change
  versioning.
- Historical backtesting through the production `build_signal`,
  `build_trading_idea`, risk and lifecycle functions, including entry range,
  expiry, commission, lot sizing, drawdown, Sharpe, expectancy and breakdowns.
- Forward paper trading tied one-to-one to activated persisted ideas, with P&L,
  commission, R-multiple and `/portfolio` summary.
- Alembic revisions `20260819_0001`–`20260819_0006` and automatic adoption of
  recognized legacy `create_all` databases.
- Extensible idea factor columns (`technical`, `fundamental`, `news`, `total`)
  with weight renormalization when optional providers are absent.
- Architecture documentation and expanded unit/integration/migration tests.

### Changed

- Market history now covers every timeframe required by the horizon profiles and
  remains incremental through repository overlap/upsert.
- Bot polling starts without waiting for a blocking initial universe download;
  the first market scan is scheduled immediately in the background.
- Application entry points run Alembic upgrades instead of relying on runtime
  `metadata.create_all`.
- Risk and P&L calculations are shared by live ideas, backtest and paper trading.
- `_claude_candidate` and all duplicate client/bot/scheduler/model/config code
  were removed after the audit.

## Claude candidate merge checkpoint

### Adapted from the candidate

- The useful indicator set: SMA20/50/200, ADX, Stochastic, CCI, Bollinger Bands
  and OBV, integrated into the existing async analysis service.
- The component idea behind weighted trend/momentum/MACD/Bollinger/volume
  scoring, reimplemented behind typed normalized configuration.
- The useful concept of clustered local support/resistance, with the extrema bug
  corrected instead of copying the candidate implementation.
- Level-aware TP/SL and lot-aware sizing concepts, hardened with direction/R:R
  validation and ATR fallback inside the existing risk manager.
- Synthetic end-to-end test scenarios, `.dockerignore` and container log limits.
- Runtime dependency `ta` (MIT).

### Intentionally not ported

- Sync `apimoex` client: the existing async `httpx` client already had stronger
  pagination, retry, concurrency, resampling and ISS+ behavior.
- Candidate global Bot/Scheduler/Session objects, sync network calls inside async
  flows, alternative SQLAlchemy models and `python-dotenv` constants.
- `backtrader_moexalgo`: the candidate only mentioned it in README and contained
  no implementation. Backtesting now uses the production pipeline directly.
- Claimed fundamentals, sentiment, bonds and paper/backtest features that were
  not actually implemented in the candidate.

No candidate source files or third-party project source were copied verbatim.
See `docs/CLAUDE_INTEGRATION_AUDIT.md` and `THIRD_PARTY_NOTICES.md`.
