# Ubuntu VPS deployment — LIVE OBSERVATION / FORWARD PAPER

This deployment only observes MOEX and simulates the configured paper horizon.
There is no broker adapter, order placement, auto execution or trading credential.

Configured policy:

- `INTRADAY_1D=RESEARCH`
- `SWING_5D=RESEARCH`
- `POSITION_1M=PAPER`

## 1. Prepare Ubuntu

Install Docker Engine and the Compose plugin from Docker's official Ubuntu
instructions. Then clone the repository and select the integration branch:

```bash
git clone https://github.com/udowychenkost-ai/moex_signal_bot.git
cd moex_signal_bot
git checkout integrate-claude-version
cp .env.example .env
chmod 600 .env
```

Edit `.env` and set at minimum:

- `TELEGRAM_BOT_TOKEN` from BotFather;
- `TELEGRAM_ADMIN_CHAT_IDS` to the numeric Telegram chat ID that must always
  receive observation notifications;
- one long random `POSTGRES_PASSWORD` and exactly the same URL-encoded password
inside `DATABASE_URL`.
- `GEMINI_API_KEY` for the default `AI_PROVIDER=gemini` and
  `AI_MODEL=gemini-3.6-flash`. One transient primary failure uses exactly one
  `AI_FALLBACK_MODEL=gemini-flash-lite-latest` request. The V2 policy is
  fail-closed: if review is unavailable, PASS candidates are recorded as
  `AI_NOT_REVIEWED / WAIT` and no new V2 idea is published. OpenAI remains an
  optional provider but is not the deployment default.

Market context defaults are deployment-safe: `IMOEX` is mandatory and
`RTSI,RGBITR,RVI` are secondary daily diagnostics. Keep
`TECHNICAL_SCORING_MODEL=legacy` unless the committed OOS report explicitly
promotes the contextual candidate.

Fundamental input is optional and read-only mounted from
`./fundamentals/official.json`. Do not populate it with estimates or scraped
aggregator values. Each normalized report must retain its official URL,
`publication_date` and `available_from`; see `fundamentals/README.md`. With no
verified file the bot remains operational and shows `Фундаментал: нет данных`.

Do not add broker tokens. Start a Telegram conversation with the bot and issue
`/start`; this also registers the chat for commands.

## 2. Start

```bash
export GIT_COMMIT="$(git rev-parse --short HEAD)"
docker compose config --quiet
docker compose build --pull
docker compose up -d
docker compose ps
```

`python -m app run` applies all Alembic migrations before polling Telegram, so
the app container cannot begin normal work against an old schema. PostgreSQL and
the app both have healthchecks and `restart: unless-stopped`. Database data lives
in the named `moex_postgres` volume.

Revisions through `20260827_0013` and the V2.4 chain
`20260901_0014`–`20260902_0021` only add columns/tables and
preserve every existing V1/V2 `TradingIdea` and experiment row. Existing V1
rows remain labeled `strategy_version=v1`; V2 forward statistics use
`v2_ai_quality_filter` and do not mix the baseline. Revision `0011` adds only
provider fallback/raw usage telemetry. Revision `0012` adds the per-user
`idea_follows` relation and `notify_watchlist=true` preference; it neither
clears nor rewrites existing users, ideas, experiments or notification outbox.
Revision `0013` only makes `order_book_levels.quantity` nullable: public ISS
publishes delayed best bid/offer but can omit depth, which must not be replaced
with a fabricated zero quantity.

V2.4 revisions add immutable decision/context/daily-summary records, separate
MODEL/ACTUAL journals, append-only events and policies, persistent kill-switch
history, journal health, transactional candidate claims and an idempotent
notification outbox. Revision `0021` adds the persistent user analysis mode,
strategy-family markers, source fetch timestamps and snapshot policy-version
references. Existing users are explicitly migrated to `LEGACY_ONLY`; legacy
ideas remain `LEGACY`. No historical decision is rebuilt or deleted.

The V2.4 orchestrator and admin risk-policy wizard are registered in 0.7.1, but
production must still keep `INTRADAY_V24_ENABLED=false`. Public data and current
configuration cannot yet satisfy every mandatory gate. Shadow processing is a
separate opt-in and never permits Telegram publication.

On first deployment wait for ingestion of stock and IMOEX histories before
expecting ideas. `/status` lists stale `IMOEX/timeframe` records until the
benchmark warm-up is complete; ideas are blocked during that state.

## 3. Logs and health

```bash
docker compose ps
docker compose logs --tail=200 app
docker compose logs -f app
docker compose exec app python -m app healthcheck
docker compose exec app python -m app migrate
docker compose exec app python -m app gemini-health
```

In Telegram run:

```text
/status
/stats
/ideas
/idea 123
```

`/status` must show `Database: OK`, `Scheduler: RUNNING`, a recent MOEX update,
and successful ingestion/scanning/lifecycle/reporting jobs. A stale timeframe is
shown explicitly and prevents new ideas for the affected ticker/horizon.
The `idea_scanning` job details include checked instruments, quant candidates,
QualityGate PASS/WEAK/REJECT, AI outcomes/errors, publications, top rejection
reasons, requests/tokens/cost/fallbacks, cooldown and top-N suppressions. The
Telegram `🧠 Gemini` status calls ListModels and shows primary/fallback health and
today's request telemetry without exposing the API key.

## 4. Update and redeploy

Before the 0.7.1 update, back up PostgreSQL as described below
and preserve the current environment file:

```bash
cp .env ".env.pre-v2.4-$(date -u +%Y%m%dT%H%M%SZ)"
nano .env
```

Ensure the following values are present; keep all existing Telegram/PostgreSQL
secrets and V2 thresholds unchanged:

```env
GEMINI_API_KEY=replace_with_real_key
AI_PROVIDER=gemini
AI_MODEL=gemini-3.6-flash
AI_FALLBACK_MODEL=gemini-flash-lite-latest
AI_MAX_OUTPUT_TOKENS=4096
AI_FILTER_ENABLED=true
AI_ALLOW_UNREVIEWED_FALLBACK=false
ENABLE_LEGACY_STRATEGY=true
INTRADAY_V24_ENABLED=false
INTRADAY_V24_SHADOW_ENABLED=false
INTRADAY_V24_STRATEGY_VERSION=intraday_v2_4
INTRADAY_V24_MAX_HOLDING_TRADING_DAYS=2
INTRADAY_V24_LEVERAGE_ENABLED=false
```

Do not set `INTRADAY_V24_ENABLED=true` on the live VPS yet. Version 0.7.1 has
the fail-closed integration code, but external data, approved Data SLA,
liquidity/cost/opportunity settings, Risk Budget and calibration must pass the
separate production checklist first.

Then update without deleting the database volume:

```bash
git fetch origin
git checkout integrate-claude-version
git pull --ff-only origin integrate-claude-version
sed -i 's/^APP_VERSION=.*/APP_VERSION=0.7.1/' .env
sed -i 's/^AI_MODEL=.*/AI_MODEL=gemini-3.6-flash/' .env
sed -i 's/^AI_FALLBACK_MODEL=.*/AI_FALLBACK_MODEL=gemini-flash-lite-latest/' .env
if grep -q '^INTRADAY_V24_ENABLED=' .env; then
  sed -i 's/^INTRADAY_V24_ENABLED=.*/INTRADAY_V24_ENABLED=false/' .env
else
  printf '%s\n' 'INTRADAY_V24_ENABLED=false' >> .env
fi
if grep -q '^INTRADAY_V24_SHADOW_ENABLED=' .env; then
  sed -i 's/^INTRADAY_V24_SHADOW_ENABLED=.*/INTRADAY_V24_SHADOW_ENABLED=false/' .env
else
  printf '%s\n' 'INTRADAY_V24_SHADOW_ENABLED=false' >> .env
fi
if grep -q '^ENABLE_LEGACY_STRATEGY=' .env; then
  sed -i 's/^ENABLE_LEGACY_STRATEGY=.*/ENABLE_LEGACY_STRATEGY=true/' .env
else
  printf '%s\n' 'ENABLE_LEGACY_STRATEGY=true' >> .env
fi
if grep -q '^AI_MAX_OUTPUT_TOKENS=' .env; then
  sed -i 's/^AI_MAX_OUTPUT_TOKENS=.*/AI_MAX_OUTPUT_TOKENS=4096/' .env
else
  printf '%s\n' 'AI_MAX_OUTPUT_TOKENS=4096' >> .env
fi
if grep -q '^ENABLE_ORDERBOOK=' .env; then
  sed -i 's/^ENABLE_ORDERBOOK=.*/ENABLE_ORDERBOOK=true/' .env
else
  printf '%s\n' 'ENABLE_ORDERBOOK=true' >> .env
fi
if grep -q '^ORDERBOOK_INTERVAL_MINUTES=' .env; then
  sed -i 's/^ORDERBOOK_INTERVAL_MINUTES=.*/ORDERBOOK_INTERVAL_MINUTES=2/' .env
else
  printf '%s\n' 'ORDERBOOK_INTERVAL_MINUTES=2' >> .env
fi
if grep -q '^ORDERBOOK_REQUEST_CONCURRENCY=' .env; then
  sed -i 's/^ORDERBOOK_REQUEST_CONCURRENCY=.*/ORDERBOOK_REQUEST_CONCURRENCY=5/' .env
else
  printf '%s\n' 'ORDERBOOK_REQUEST_CONCURRENCY=5' >> .env
fi
export GIT_COMMIT="$(git rev-parse --short HEAD)"
docker compose config --quiet
docker compose build --pull
docker compose up -d --remove-orphans
docker compose ps
docker compose logs --tail=200 app
docker compose exec app python -m app healthcheck
docker compose exec app python -m app gemini-health
docker compose exec -T postgres sh -c \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT version_num FROM alembic_version"'
docker compose exec -T postgres sh -c \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -P pager=off' <<'SQL'
SELECT version_num FROM alembic_version;
SELECT 'idea_journals', COUNT(*) FROM idea_journals
UNION ALL SELECT 'model_trade_journals', COUNT(*) FROM model_trade_journals
UNION ALL SELECT 'actual_trade_journals', COUNT(*) FROM actual_trade_journals
UNION ALL SELECT 'trade_event_journal', COUNT(*) FROM trade_event_journal;
SQL
```

The expected Alembic revision is `20260902_0021`. Do not run `docker compose
down -v`: the `-v` flag would remove the persistent PostgreSQL volume.

After the app starts, `/status` must show Alembic `20260902_0021`, Legacy
`ENABLED`, Intraday engine/shadow/publication `DISABLED`, journal `AVAILABLE`,
and the actual Data SLA/risk/calibration/kill states. `NOT_CONFIGURED` is
expected for policies not yet approved and must not be “fixed” with invented
limits. In Telegram, Settings → Режим анализа persists the per-user selection;
existing users start in `LEGACY_ONLY`. Admins listed in
`TELEGRAM_ADMIN_CHAT_IDS` can use `/riskpolicy`, but activation always requires
an explicit confirmation and creates a new policy version.

V2.1.5.1 adds the independent `order_book_ingestion` job. Public ISS provides a
delayed official best bid/offer feed but not guaranteed depth; the bot stores
missing depth as `NULL`, uses the real spread, and does not claim a depth-based
size. Set `ENABLE_ORDERBOOK=true`, `ORDERBOOK_INTERVAL_MINUTES=2` and
`ORDERBOOK_REQUEST_CONCURRENCY=5`. A paid ISS token is optional and is only
needed for subscribed full depth. After redeploy, run the manual verification
below; a snapshot older than 300 seconds is intentionally excluded.

```bash
docker compose exec app python -m app ingest-orderbook
docker compose exec -T postgres sh -c \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -P pager=off' <<'SQL'
SELECT COUNT(*), MAX(snapshot_at) FROM order_book_levels;
SELECT secid, COUNT(*) AS levels, MAX(snapshot_at)
  FROM order_book_levels
 GROUP BY secid
 ORDER BY secid;
SELECT secid, side, level, price, quantity, snapshot_at
  FROM order_book_levels
 WHERE secid='SBER'
 ORDER BY snapshot_at DESC, side, level
 LIMIT 20;
SQL
```

Both `B` and `S` rows must have positive prices. `quantity > 0` means real MOEX
lots; `NULL` means public ISS did not publish depth and is expected—not a failed
or fabricated snapshot. `/status` reports enabled state, last update, fresh
instruments and the last job as `OK`, `PARTIAL` or `ERROR`.

V2.1.4 adds no migration and no environment variable. Gemini user-facing prose
is now validated as Russian; a predominantly English structured response is
recorded as `LANGUAGE_MISMATCH` and retried once on the primary model with stage
`PRIMARY_LANGUAGE_RETRY`. Technical tokens and tickers remain unchanged.

V2.1.3 does not add a migration. It gives the complete `AIAnalysis` object a
4096-token ceiling, uses Gemini 3.6 `thinkingLevel=minimal`, and performs at
most three structured attempts: primary, one primary validation retry, then one
fallback. Invalid model text is not persisted.

To validate the real on-demand path immediately after deployment, open an
existing idea in Telegram and run `/idea ID`, then press
`🧠 Проанализировать сейчас`. Inspect the resulting separate attempts:

```bash
docker compose exec -T postgres sh -c \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -P pager=off' <<'SQL'
SELECT created_at, model, status, fallback_used, latency_ms,
       COALESCE((usage_json::jsonb)->>'error_code', '') AS error_code,
       (usage_json::jsonb)->>'retry_stage' AS retry_stage
  FROM ai_request_logs
 WHERE request_kind = 'ON_DEMAND_IDEA'
 ORDER BY id DESC
 LIMIT 3;
SQL
```

A normal first-pass result has one `PRIMARY / OK` row. Recovery from a truncated
response has `PRIMARY / ERROR / INVALID_STRUCTURED_RESPONSE`, followed by
`PRIMARY_STRUCTURED_RETRY / OK`; fallback is present only if that retry also
fails.

The app performs startup recovery from PostgreSQL: open and pending ideas remain
in place, later candles continue their lifecycle, and the notification outbox
prevents a previously delivered event from being sent twice.
Open `candidate_experiments`, including AI-rejected rows, also resume lifecycle
tracking so cohort outcomes are not lost after restart.
Per-user watchlist and followed-idea button states are also restored from
PostgreSQL after an app/container restart.

## 5. PostgreSQL backup and restore

Create a backup outside the container and verify that it is non-empty:

```bash
mkdir -p backups
docker compose exec -T postgres sh -c \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
  > "backups/moex-$(date -u +%Y%m%dT%H%M%SZ).dump"
ls -lh backups/
```

Copy backups to a second machine or protected object storage. Test restoration on
a separate PostgreSQL instance before relying on it. A destructive restore into
the live database requires an intentional maintenance window:

```bash
docker compose stop app
docker compose exec -T postgres sh -c \
  'dropdb -U "$POSTGRES_USER" --if-exists "$POSTGRES_DB" && createdb -U "$POSTGRES_USER" "$POSTGRES_DB"'
docker compose exec -T postgres sh -c \
  'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists' \
  < backups/SELECTED_BACKUP.dump
docker compose up -d app
docker compose logs --tail=200 app
```

## 6. Operating rules

- Do not change the three observation modes during a measurement window.
- Do not tune thresholds from forward outcomes without starting a newly declared
  research cycle.
- Treat RESEARCH R as lifecycle analytics without paper P&L or trading costs.
- Treat POSITION paper P&L as simulation with configured commission/slippage,
  never as evidence of executable broker fills.
- Back up PostgreSQL daily and monitor `/status` after MOEX outages and VPS
  restarts.
