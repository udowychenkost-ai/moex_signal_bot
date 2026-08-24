# V2 quality filter and AI verdict

## Scope and invariants

V2 extends the existing `TradingIdea`, lifecycle, backtest, paper-trading and
scheduler architecture. It does not add a broker adapter, order placement or a
second data model. Every old row remains part of the V1 baseline; new forward
observations use `strategy_version=v2_ai_quality_filter`.

```text
quant candidate
  -> freshness + deterministic QualityGate
  -> frozen candidate_experiment
  -> PASS candidates ranked by final_quality_score
  -> cooldown and per-horizon AI candidate cap
  -> structured provider second opinion (Gemini default)
  -> APPROVE / STRONG_APPROVE only
  -> per-scan and per-day top-N
  -> existing TradingIdea repository and lifecycle
  -> Telegram
```

`WEAK`, `REJECT`, AI `WAIT`, AI `REJECT`, cooldown and rank-suppressed rows stay
in `candidate_experiments`. Their lifecycle is evaluated from the same completed
candles as published ideas so future cohort comparisons include actual outcomes.

## Integration points

| Boundary | V2 implementation | Preserved component |
|---|---|---|
| Quant quality | `app/quality.py` | existing signal/scoring/risk pipeline |
| Batch decision | `app/scanner.py` | one scanner and one scheduler |
| AI second opinion | `app/ai_analyst.py` | deterministic score remains authoritative |
| Experiment lifecycle | `app/experiments.py` | shared lifecycle evaluator |
| Persistence | additive migrations `20260824_0010` + `0011` | existing `TradingIdea`, snapshots and experiment rows |
| Telegram | menus and callbacks in `app/bot.py` | slash commands remain fallback |
| Metrics | cohort statistics in `app/operations.py` | V1 results remain separate |
| Research | `app/quality_research.py` | existing leakage-safe backtest engine |

## Deterministic QualityGate

The gate returns `PASS`, `WEAK` or `REJECT`. It evaluates directional technical
and total scores, R:R, liquidity, relative volume, completed-timeframe agreement,
independent supporting factors, explicit conflicts and strong opposing IMOEX
regime. Stale data, insufficient directional strength, low R:R, known inadequate
liquidity, insufficient timeframe agreement, excessive conflicts and an
unoverridden strong opposing regime are hard rejects. Missing/weak volume,
unknown liquidity or too few independent confirmations cannot produce `PASS`.

A BEAR-market BUY or BULL-market SELL can override the regime block only when
relative strength, volume, levels and momentum/reversal evidence are all strong.
RSI oversold/overbought alone is never sufficient.

The horizon confirmation policy was selected without looking at TEST:

- `INTRADAY_1D=4`, research-only; the current legacy selector produced no OOS
  candidate, so the value is not claimed as validated;
- `SWING_5D=5`, selected on TRAIN/VALIDATION;
- `POSITION_1M=5`, selected on TRAIN/VALIDATION.

## Historical comparison

Dataset: fixed liquid universe of 20 MOEX stocks through 2026-08-18. Selection
used TRAIN/VALIDATION; the following numbers are from untouched TEST. AI was not
replayed historically because a current LLM verdict would not reproduce the
future live model state.

| Horizon | N | Ideas before | Ideas after | Reduction | WR before/after | PF before/after | Expectancy before/after |
|---|---:|---:|---:|---:|---:|---:|---:|
| 5 days | 5 | 2,016 | 406 | 79.86% | 37.40% / 36.02% | 0.886 / 0.901 | -0.077R / -0.071R |
| 1 month | 5 | 723 | 207 | 71.37% | 42.14% / 46.02% | 1.139 / 1.297 | +0.064R / +0.166R |
| 1 day | 4 | 0 | 0 | n/a | n/a | n/a | n/a |

The result supports a much smaller POSITION candidate set with better quality.
SWING loss magnitude fell, but the OOS edge remains negative, so it stays
RESEARCH. Top-N, cooldown and AI are forward controls and are intentionally not
claimed in these historical figures. Full metrics and limitations are in
`reports/backtests/V2_QUALITY_COMPARISON.md` and
`reports/backtests/v2_quality_comparison.json`.

## AI contract

Default provider: Google Gemini `gemini-2.5-flash`, configurable through
`AI_PROVIDER` and `AI_MODEL`. `OpenAIProvider` remains available as an explicit
alternative. Both adapters use a Pydantic-generated JSON schema, a bounded
output budget and only the same compact decision snapshot. Gemini uses
`responseMimeType=application/json` plus `responseJsonSchema`; unsupported
Pydantic validation keywords are removed from the wire schema and validated
locally after receipt. API details: [Gemini structured outputs](https://ai.google.dev/gemini-api/docs/structured-output),
[Gemini 2.5 Flash-Lite](https://ai.google.dev/gemini-api/docs/models/gemini-2.5-flash-lite)
and [API errors](https://ai.google.dev/gemini-api/docs/generate-content/api-errors).

The prompt forbids invented news, financial figures, levels, prices, events and
forecasts. Missing inputs are marked unavailable. The result schema contains:

- `verdict`: `STRONG_APPROVE`, `APPROVE`, `WAIT` or `REJECT`;
- `score` from 0 to 100, explicitly not a calibrated probability;
- `analysis_confidence`, `bull_case`, `bear_case`, `why_now`, `key_risks`,
  `invalidation_conditions` and `short_summary`.

Timeout/rate-limit/temporary-unavailable errors from the primary Gemini model
permit exactly one request to `gemini-2.5-flash-lite`. No fallback is attempted
for malformed JSON, schema mismatch, authentication/configuration errors or
other permanent failures. If no valid review is received, the result is
`AI_NOT_REVIEWED / WAIT`. Request provider, exact returned model, raw usage,
input/output tokens, estimated cost, latency, error and `fallback_used` are
stored per attempt in `ai_request_logs` and summarized in the frozen candidate
row. Only a quantitative `PASS` can invoke AI; AI cannot rescue `WEAK/REJECT`.

## Telegram UX

The main reply keyboard contains six sections: best ideas, active ideas,
statistics, settings, market analysis and system status. Inline buttons cover
period/horizon selection, all user settings and idea details. Each idea exposes
AI, technical, fundamental, market and lifecycle screens. Event preferences are
applied to the owner as well as ordinary users. Disabling the owner AI filter
publishes quant-PASS ideas with an explicit “Идея не проходила AI second
opinion” label; AI remains enabled by default.

Statistics are version-isolated and show published V2 results plus `ALL QUANT
CANDIDATES`, `AI APPROVED` and `AI REJECTED` cohorts for 7 days, 30 days and all
time, separately for 1D/5D/1M.

## Database and deployment

Alembic `20260824_0010` is additive: it adds user preferences and V2 fields,
`candidate_experiments` and `ai_request_logs`. Revision `20260824_0011` adds
only fallback/raw usage telemetry columns with safe defaults. Existing ideas
receive only compatibility defaults `v1 / LEGACY / NOT_REQUESTED`; no old
outcome is rewritten. Startup migration and experiment lifecycle recovery run
before polling.

Deployment, healthchecks, logs, PostgreSQL backup/restore and redeploy commands
are documented in `DEPLOY.md`. `GEMINI_API_KEY` is required for the default
fail-closed publication policy.

## Verification

- full suite: `131 passed`;
- Ruff format/check: passed;
- Python compileall and dependency check: passed;
- local existing-schema upgrade and application healthcheck: passed;
- Compose YAML parsing: passed;
- Docker build and live PostgreSQL container upgrade: not runnable on the local
  workstation because Docker/PostgreSQL are not installed; exact VPS checks are
  retained in `DEPLOY.md`.
