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
- `OPENAI_API_KEY` for the configured `AI_MODEL=gpt-5-mini`. The V2 policy is
  fail-closed: without a working key PASS candidates are recorded as `WAIT` and
  no new V2 idea is published.

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

Revision `20260824_0010` only adds columns/tables and preserves every existing
V1 `TradingIdea`. Existing rows are labeled `strategy_version=v1`; V2 forward
statistics use `v2_ai_quality_filter` and do not mix the baseline.

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
The `idea_scanning` job details include QualityGate counts, AI requests/tokens/
estimated cost/latency errors, cooldown suppressions and top-N suppressions.

## 4. Update and redeploy

```bash
git fetch origin
git checkout integrate-claude-version
git pull --ff-only origin integrate-claude-version
export GIT_COMMIT="$(git rev-parse --short HEAD)"
docker compose build --pull
docker compose up -d --remove-orphans
docker compose ps
docker compose logs --tail=200 app
```

The app performs startup recovery from PostgreSQL: open and pending ideas remain
in place, later candles continue their lifecycle, and the notification outbox
prevents a previously delivered event from being sent twice.
Open `candidate_experiments`, including AI-rejected rows, also resume lifecycle
tracking so cohort outcomes are not lost after restart.

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
