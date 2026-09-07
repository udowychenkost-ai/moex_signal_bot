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
  -> selected AI provider review only after deterministic pre-gates are clear
  -> adversarial check -> all-mandatory FinalAudit
  -> one V2.4 classification
  -> immutable IdeaJournal + DecisionSnapshotV24
  -> separate MODEL and user-confirmed ACTUAL journals
  -> append-only TradeEventJournal
  -> mode-filtered idempotent Telegram outbox
```

`IntradayV24Orchestrator` is the single application service for this chain. The
scheduled coordinator runs confirmed ACTUAL recovery/management first,
active/pending MODEL lifecycle second, then canonical ingestion and the isolated
V2.4 scan. A transactional SHA-256 candidate claim prevents duplicate journal,
snapshot, model and outbox rows across retries. Legacy scan/reporting and V2.4
remain separate jobs in the same APScheduler, so an application error in one
does not stop the other.

`INTRADAY_V24_ENABLED` remains `false` in production because external facts and
approved policies cannot yet satisfy all gates. `INTRADAY_V24_SHADOW_ENABLED`
is a separate collection switch: it may journal/model candidates without
granting publication eligibility.

## Important modules

| Boundary | Modules |
|---|---|
| Data provenance and freshness | `data_integrity.py`, `data_sla.py`, `microstructure.py`, `context_v24.py` |
| MTF and setups | `intraday_technical.py`, `intraday_setup.py`, `market_regime_v24.py`, `intraday_v24.py` |
| Execution and model fills | `execution_v24.py`, `model_execution_v24.py` |
| Cost, liquidity and risk | `risk_v24.py`, `liquidity_v24.py`, `opportunity.py` |
| Audit and classification | `adversarial.py`, `final_audit.py`, `classification_v24.py` |
| Application orchestration | `orchestrator_v24.py` |
| Journals | `journal.py`, `actual_trades.py`, `models.py` |
| Statistics | `statistics_v24.py`, `calibration.py` |
| Runtime safety | `journal_health.py`, `kill_switch.py`, `scheduler_v24.py`, `observability_v24.py` |
| Versioned admin policy | `risk_policy_admin.py` |
| Telegram/reporting | `reporting_v24.py`, `bot.py`, `telegram_ui.py` |

## Fail-closed invariants

- Missing configuration is a typed `NOT_CONFIGURED`, never a plausible zero.
- Missing market facts are `DATA_NOT_AVAILABLE`/`NULL`; AI cannot supply
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

Do not enable production publication until all of the following exist and pass
an integration test on the deployment:

1. explicitly approved Data SLA values and source classes;
2. a versioned risk policy and cost inputs supplied by the operator;
3. reliable full-depth/order-impact inputs where a depth cap is mandatory;
4. callable providers for the required event/corporate-action/borrow facts;
5. a normal initialized kill switch and available journal at the exact release
   Alembic head;
6. shadow/forward observations sufficient for the chosen admission policy.

The full release gate is maintained in
[`INTRADAY_V2_4_PRODUCTION_CHECKLIST.md`](INTRADAY_V2_4_PRODUCTION_CHECKLIST.md).
