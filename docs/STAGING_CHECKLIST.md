# Staging and forward-paper checklist

Staging is a forward simulation only. The application has no broker adapter and
does not place real orders. The permitted flow is:

```text
MOEX live data → signal → TradingIdea → Telegram → lifecycle → PaperTrade → report
```

## Deployment

1. Copy `.env.staging.example` to `.env.staging` and set a Telegram bot token and
   a strong PostgreSQL password.
2. Keep the calibrated strategy settings unchanged after the OOS result is
   produced. Do not tune them from forward-paper outcomes without starting a new
   declared research cycle.
3. Start PostgreSQL and the bot:

   ```powershell
   docker compose --env-file .env.staging -f docker-compose.staging.yml up --build -d
   ```

4. Confirm the database migration and service state:

   ```powershell
   docker compose --env-file .env.staging -f docker-compose.staging.yml exec app python -m app migrate
   docker compose --env-file .env.staging -f docker-compose.staging.yml ps
   docker compose --env-file .env.staging -f docker-compose.staging.yml logs --tail 200 app
   ```

## Acceptance checks

| Area | Evidence required |
|---|---|
| PostgreSQL | Alembic is at head; restart preserves instruments, candles, ideas, events and paper trades |
| MOEX ingestion | Universe sync succeeds; all configured timeframes advance without duplicate candle keys |
| Scheduler | One scheduler owns five non-overlapping ingestion, scan, lifecycle, Telegram and daily jobs |
| Telegram | Bot starts polling; `/status`, `/stats`, `/ideas`, `/idea`, `/best` and `/portfolio` respond |
| TradingIdea | A pending idea activates only after a later candle touches the frozen entry zone |
| Lifecycle | TP/SL/expiry creates one terminal event and one paper close; repeat scan is idempotent |
| Reporting | Notification uniqueness holds for `(user, idea, version)` |
| Paper costs | Reference and fill prices, gross P&L, commission, slippage and net P&L are stored |
| Safety | No broker credentials, order endpoint or real execution adapter is configured |

## Backtest versus forward comparison

Use the same commission and slippage assumptions in backtest and staging unless
the observed execution sample supports a documented change. Compare by horizon,
ticker, direction and confidence bucket. Forward results must not be appended to
the historical OOS set or used to rewrite the published OOS report.

Docker runtime and PostgreSQL integration remain external deployment checks on a
development machine where Docker/PostgreSQL are unavailable.
