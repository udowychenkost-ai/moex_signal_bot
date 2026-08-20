# Changelog

## Unreleased — unified TradingIdea platform

### Added

- LIVE OBSERVATION / FORWARD PAPER runtime with the fixed policy
  `INTRADAY_1D=RESEARCH`, `SWING_5D=RESEARCH`, `POSITION_1M=PAPER`.
- Independent ingestion, idea-scanning, lifecycle/paper, Telegram-dispatch and
  daily-summary jobs inside the existing single APScheduler.
- Data-freshness guard per ticker/timeframe and completed-candle filtering for
  decision and lifecycle evaluation; stale MOEX data cannot create ideas.
- Immutable one-to-one `TradingIdeaSnapshot` with decision price, factors,
  technical/fundamental/news/total scores, strength, ATR, indicators and levels.
- Event-level Telegram notifications for CREATED/ACTIVATED/TP/SL/EXPIRED/
  INVALIDATED/CANCELLED with a persistent idempotent delivery outbox.
- `/status`, `/stats`, `/ideas` and `/idea ID`, including small-sample warnings,
  per-horizon modes and forward metrics for 7/30 days/all time.
- Persisted scheduler diagnostics, startup recovery, daily Telegram summary,
  Docker/PostgreSQL healthchecks and Ubuntu VPS runbook in `DEPLOY.md`.
- Alembic revision `20260820_0008` for observation modes, snapshots, job state
  and notification outbox.
- Alembic revision `20260820_0009` for sector classification, IMOEX/secondary
  market candles, contextual decision fields and point-in-time fundamental
  reports.
- `MarketRegimeService`: IMOEX BULL/BEAR/SIDEWAYS, causal volatility state,
  drawdown/ATR, horizon-relative strength and mandatory live scoring context.
- Eight transparent contextual technical components with confirmed momentum
  extremes, directional relative volume/OBV and volume-confirmed level breaks.
- Provider-based reviewed official fundamental imports, sector-relative scoring
  and immutable publication metadata; missing coverage is never synthesized.
- Fixed A–F ablation command with explicit `not_evaluable` fundamental variants
  when point-in-time coverage is absent, plus broad-crash oversold regression.
- OOS/WF promotion gate retained `legacy` for all horizons: POSITION full
  improved expectancy but increased drawdown and lacked fundamentals; SWING
  full remained negative. Market context is collected for forward analysis
  without a hidden production scoring switch.

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
- Independent market scan and idea reporting boundaries in one scheduler.
- Per-user `(idea, version)` notification deduplication and material-change
  versioning.
- Historical backtesting through the production `build_signal`,
  `build_trading_idea`, risk and lifecycle functions, including entry range,
  expiry, commission, lot sizing, drawdown, Sharpe, expectancy and breakdowns.
- Forward paper trading tied one-to-one to activated persisted ideas, with P&L,
  reference/fill prices, commission, slippage, R-multiple and `/portfolio`
  summary.
- Alembic revisions `20260819_0001`–`20260819_0007` and automatic adoption of
  recognized legacy `create_all` databases.
- Extensible idea factor columns (`technical`, `fundamental`, `news`, `total`)
  with weight renormalization when optional providers are absent.
- Architecture documentation and expanded unit/integration/migration tests.
- A separate fixed 20-stock research universe with incremental MOEX history,
  coverage manifest and explicit survivorship/corporate-action warnings.
- Leakage-safe TRAIN/VALIDATION/OOS evaluation, limited calibration sweep,
  three-fold walk-forward, simple buy-and-hold/EMA/RSI benchmarks and strict
  JSON/Markdown research output.
- Configurable order-side BUY/SELL slippage with gross, commission, slippage and
  net attribution in backtest and forward paper trading.
- PostgreSQL staging compose example and an explicit forward-paper acceptance
  checklist; no real-order execution path was added.
- Reproducible 20-stock/1,722,488-candle research artifacts through 2026-08-18.
  OOS rejected intraday (PF 0.51, expectancy -0.46 R), found swing economically
  flat after costs (PF 1.004), and retained only a modest position-horizon edge
  for forward paper (PF 1.145, expectancy +0.067 R). Production defaults were
  not changed automatically.

### Changed

- Market history now covers every timeframe required by the horizon profiles and
  remains incremental through repository overlap/upsert.
- Bot polling starts without waiting for a blocking initial universe download;
  the first market scan is scheduled immediately in the background.
- Application entry points run Alembic upgrades instead of relying on runtime
  `metadata.create_all`.
- Risk and P&L calculations are shared by live ideas, backtest and paper trading.
- Backtest decisions execute no earlier than the next primary candle; adjacent
  evaluation windows are non-overlapping and expiry-crossing candles cannot
  claim post-deadline TP/SL.
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
