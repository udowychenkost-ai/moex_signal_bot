# Intraday V2.4 production checklist

This checklist intentionally separates code readiness from data, policy,
calibration and publication readiness. A green implementation does not make a
strategy safe to publish.

## Current release state (0.7.2)

| Readiness area | State | Evidence / blocker |
|---|---|---|
| IMPLEMENTATION READY | YES | One fail-closed orchestrator, one scheduler, transactional candidate claim + outbox, immutable decision snapshot, append-only lifecycle events, persistent modes and versioned risk-policy wizard are implemented and regression-tested. |
| SHADOW READY | YES, opt-in | `INTRADAY_V24_SHADOW_ENABLED=true` runs collection without production delivery. Start with a test database/VPS backup and monitor the V2.4 scan/journal metrics. The committed default remains `false`. |
| DATA READY | NO | The current public source does not establish full reliable L2 depth, tick/price-band/session facts, broker borrow/carry, verified realtime event/news context or cross-asset feeds. These remain `DATA_NOT_AVAILABLE`. |
| RISK POLICY READY | NO | No risk values are invented. An authorized administrator must create and explicitly activate an approved effective-dated policy. Data SLA, liquidity, cost and opportunity policies also remain unconfigured by default. |
| CALIBRATION READY | NO | No statistically admitted V2.4 forward cohort exists yet. Ambiguous or unreliable model fills are excluded from calibration. |
| PRODUCTION NOTIFICATION READY | NO | `INTRADAY_V24_ENABLED=false`; mandatory deterministic gates and Final Audit cannot currently pass reliably. |
| STATISTICALLY QUALIFIED | NO | No approved OOS + forward sample satisfies the configured admission and reliability requirements. |

Version 0.7.2 retains the 0.7.1 evaluation ordering and adds compact per-ticker
diagnostics to the existing scan job payload and `/status`. Deterministic
setup detection now runs before the unchanged market-regime permission gate;
blocked directions remain `NO_TRADE`. No numeric setup, scoring, risk or
production-qualification threshold was relaxed.

## Implementation acceptance

- [x] `LEGACY` and `INTRADAY_V24` have independent `strategy_family` and
  `strategy_version` values.
- [x] Existing/new Telegram users default to persistent `LEGACY_ONLY`.
- [x] `LEGACY_ONLY`, `INTRADAY_V24_ONLY` and `BOTH` affect delivery/display,
  not stored history.
- [x] Legacy and V2.4 run as failure-isolated jobs under one APScheduler.
- [x] Shadow processing is independent from user subscription and never grants
  publication eligibility.
- [x] The orchestrator reuses existing MTF, data, execution, risk, calibration,
  adversarial and audit services.
- [x] Missing/unknown mandatory inputs fail closed; Gemini cannot override a
  deterministic failure.
- [x] Candidate claim, journal, snapshot, optional model record and outbox rows
  commit in one transaction.
- [x] Initial decisions are immutable; lifecycle changes are append-only events.
- [x] ACTUAL creation still requires explicit user confirmation and remains
  visible while open regardless of later analysis-mode changes.
- [x] Risk policy activation is admin-only, previewed, explicitly confirmed and
  append-only by configuration version/effective time.
- [x] `/ideas`, `/stats`, `/status` and Telegram Settings expose strategy mode
  without mixing performance cohorts.

## Data readiness

Before considering production, document and test providers for every mandatory
fact below. A provider being reachable is not proof that a specific value is
timely and reliable.

- [ ] Completed 1d/1h/15m/5m stock and IMOEX candles carry source and fetch
  timestamps and pass the approved Data SLA.
- [ ] A real 1m execution-quality source is available if 1m execution is used.
- [ ] Best bid/ask and the depth used for sizing are distinguishable; public
  level-1 quantity must never be represented as full L2.
- [ ] Tick size, price bands and trading-session state are current.
- [ ] Corporate actions and realtime event/news context are verified and
  point-in-time.
- [ ] SHORT eligibility, borrow availability and carry are supplied by an
  authorized provider; otherwise SHORT remains blocked.
- [ ] A reliable portfolio-state source supplies current exposure/heat and
  current-day realized loss. Until then, any existing ACTUAL or filled MODEL
  history makes the portfolio gate unavailable rather than assuming zero.
- [ ] Staleness, missing fields and provider errors are exercised in staging and
  remain fail-closed.

## Policy readiness

- [ ] Approve all `DATA_SLA_*`, required source classes, microstructure spread
  and order-book-age limits; increment `DATA_SLA_VERSION`.
- [ ] Approve every `LIQUIDITY_V2_*` participation/slippage/impact value.
- [ ] Approve all cost fields and a meaningful
  `INTRADAY_V24_COST_MODEL_VERSION`; SHORT also needs carry.
- [ ] Approve all eight `INTRADAY_V24_OPPORTUNITY_WEIGHT_*` values.
- [ ] In Telegram, an ID from `TELEGRAM_ADMIN_CHAT_IDS` runs `/riskpolicy`,
  enters every existing risk-domain field, reviews the preview and confirms a
  new unique version.
- [ ] `/status` reports Data SLA, liquidity, costs and Risk Budget configured,
  journal available and kill switch inactive.

## Calibration and statistical admission

- [ ] Enable shadow only after database backup and staging verification.
- [ ] Collect model outcomes without changing historical V1/V2 cohorts.
- [ ] Resolve fill ambiguity from reliable lower-timeframe data; do not mark
  ambiguous/public-quote-only fills calibration eligible.
- [ ] Keep MODEL and ACTUAL performance separate.
- [ ] Validate calibration per exact V2.4 cohort and strategy version using the
  declared OOS + forward policy, Wilson bound, Brier/ECE and degradation checks.
- [ ] Record the approved statistical-admission policy version.

## Production publication gate

Only after all sections above are complete:

1. Back up PostgreSQL and verify restore on a separate database.
2. Confirm Alembic head `20260902_0021` and a clean application healthcheck.
3. Review several shadow scan records: all mandatory gates must have real
   evidence, not operator placeholders.
4. Confirm Final Audit readiness, journal availability and kill switch state in
   `/status`.
5. Confirm an independent review of Data SLA, liquidity, cost and risk-policy
   values and strategy statistics.
6. Enable `INTRADAY_V24_ENABLED=true` in a controlled window. Keep shadow and
   production states visible and retain an immediate rollback to `false`.

Never use `docker compose down -v`, delete historical rows, rebuild old
snapshots, merge V2.4 MODEL results with legacy paper P&L, or turn an unavailable
fact into a numeric default merely to make the audit pass.
