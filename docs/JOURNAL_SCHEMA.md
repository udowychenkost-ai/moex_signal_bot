# V2.4 journal schema

Alembic revisions `20260901_0014` through `20260901_0020` add the V2.4 schema.
All revisions are additive and preserve legacy ideas, paper trades, AI logs and
notification history.

| Table | Purpose | Mutation policy |
|---|---|---|
| `trade_id_sequences` | Atomic daily/ticker/direction sequence | Internal upsert only |
| `idea_journals` | Original V2.4 idea card and decision | Immutable by DB trigger |
| `decision_snapshots_v24` | Point-in-time inputs, evidence and gates | Immutable by DB trigger |
| `model_trade_journals` | Hypothetical/model execution and result | Lifecycle fields may advance |
| `actual_trade_journals` | User-confirmed actual execution facts | Created only with explicit confirmation |
| `trade_event_journal` | Entry, stop, target, partial/full exit and notes | Append-only by DB trigger |
| `context_records_v24` | Point-in-time fundamental/news/event/cross-asset envelopes | Append-only by DB trigger |
| `risk_budget_settings` | Effective-dated risk policy | Append-only by DB trigger |
| `statistical_admission_settings` | Effective-dated calibration/admission policy | Append-only by DB trigger |
| `kill_switch_events` | Persistent state transitions and reasons | Append-only by DB trigger |
| `journal_health_probes` | Rollback-only write readiness target | Probe rows are never committed |
| `daily_journal_summaries_v24` | Per-day MODEL/ACTUAL report | Immutable by DB trigger |
| `v24_notification_outbox` | Idempotent Telegram delivery | Mutable delivery state only |

## Identifiers and separation

An idea ID has the form `YYYYMMDD-TICKER-LONG|SHORT-NN`. Re-entry always
allocates a new ID. MODEL and ACTUAL IDs are distinct and both reference the
same original idea. An ACTUAL row additionally requires `telegram_id` and a
unique confirmation key, so a repeated Telegram update cannot create another
trade.

## Snapshot rules

The decision snapshot records only facts available at decision time, including
source/timestamp/SLA, setup/regime, entry/stop/targets, liquidity/risk inputs and
the audit matrix. It is never backfilled from later data. If old evidence does
not exist, the correct value is `MISSING_HISTORICAL_EVIDENCE`.

All subsequent observations belong in `trade_event_journal`. Application code
does not update the original idea or snapshot to make historical decisions look
better. PostgreSQL and SQLite triggers independently reject update/delete.

## Backup and restore

The journal lives in the same PostgreSQL database and named Docker volume as
the legacy application. Use a full `pg_dump -Fc`; do not copy tables
selectively because foreign keys and immutable evidence must remain consistent.
Restore into a separate database first and verify `alembic_version`, row counts
and `/status` journal health before using the backup operationally.

