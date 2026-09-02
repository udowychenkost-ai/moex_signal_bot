# INTRADAY V2.4 architecture

## Scope and isolation

`intraday_v2_4` is an additive, disabled-by-default strategy layer. It does not
change `INTRADAY_1D`, `SWING_5D`, `POSITION_1M`, `v1`,
`v2_ai_quality_filter`, their thresholds, historical rows or OOS artifacts.
There is no broker adapter, order API or automatic actual-trade execution.

The code is organized as a fail-closed pipeline:

```text
MOEX ISS ingestion (1d, 1h, 15m, 5m; optional verified 1m)
  -> DataIntegrity + DataSLA + Microstructure
  -> deterministic MTF technical / market regime / setup
  -> entry / path-to-TP / execution / cost / liquidity / risk
  -> calibration + statistical admission
  -> adversarial check -> all-mandatory FinalAudit
  -> one V2.4 classification
  -> immutable IdeaJournal + DecisionSnapshotV24
  -> separate MODEL and user-confirmed ACTUAL journals
  -> append-only TradeEventJournal
```

The scheduled V2.4 coordinator currently performs recovery and data ingestion
in strict priority order: confirmed ACTUAL positions, active/pending MODEL
trades, then the V2.4 ingestion pass. The pure analysis, gate, classification
and persistence services are implemented and tested, but a live end-to-end
candidate orchestrator is intentionally not registered yet. Therefore
`INTRADAY_V24_ENABLED` must remain `false` in production. Enabling it today
warms the isolated MTF data and reports readiness; it does not publish V2.4
ideas. This is an explicit remaining integration gap, not a silent success.

## Important modules

| Boundary | Modules |
|---|---|
| Data provenance and freshness | `data_integrity.py`, `data_sla.py`, `microstructure.py`, `context_v24.py` |
| MTF and setups | `intraday_technical.py`, `intraday_setup.py`, `market_regime_v24.py`, `intraday_v24.py` |
| Execution and model fills | `execution_v24.py`, `model_execution_v24.py` |
| Cost, liquidity and risk | `risk_v24.py`, `liquidity_v24.py`, `opportunity.py` |
| Audit and classification | `adversarial.py`, `final_audit.py`, `classification_v24.py` |
| Journals | `journal.py`, `actual_trades.py`, `models.py` |
| Statistics | `statistics_v24.py`, `calibration.py` |
| Runtime safety | `journal_health.py`, `kill_switch.py`, `scheduler_v24.py`, `observability_v24.py` |
| Telegram/reporting | `reporting_v24.py`, `bot.py`, `telegram_ui.py` |

## Fail-closed invariants

- Missing configuration is a typed `NOT_CONFIGURED`, never a plausible zero.
- Missing market facts are `DATA_NOT_AVAILABLE`/`NULL`; Gemini cannot supply
  them.
- Data SLA not configured or not passing blocks immediate action.
- Public best bid/offer without quantity is not full L2 depth.
- Missing risk caps prevent `FULL_RISK_PASS` and a reliable recommended size.
- An uncalibrated cohort never exposes a numeric probability.
- One failed mandatory audit gate prevents `PRODUCTION_QUALIFIED`.
- Uninitialized or unhealthy journal/kill-switch state blocks new V2.4
  positions, while recovery/monitoring continues.
- Actual trades and their changes require an explicit Telegram user action.

## Scheduler and restart recovery

`V24SchedulerCoordinator` restores ACTUAL/MODEL state from PostgreSQL on every
cycle, checks the journal with a rollback-only write probe and evaluates the
persistent kill switch before the new-data stage. The legacy scheduler and
lifecycle remain unchanged. One-process APScheduler uses `max_instances=1`;
multiple app replicas still require an external leader lock and are not a
supported deployment topology.

## Production-enablement prerequisites

Do not register the final live candidate orchestrator until all of the
following exist and pass an integration test on the deployment:

1. explicitly approved Data SLA values and source classes;
2. a versioned risk policy and cost inputs supplied by the operator;
3. reliable full-depth/order-impact inputs where a depth cap is mandatory;
4. callable providers for the required event/corporate-action/borrow facts;
5. the end-to-end scan adapter that writes a complete immutable snapshot,
   model lifecycle and idempotent notification in one tested workflow;
6. shadow/forward observations sufficient for the chosen admission policy.

