# V2.4 final audit

`FinalAuditService` is an all-mandatory deterministic gate. It does not use a
weighted average, so a strong score cannot hide a missing or failed production
prerequisite.

The matrix covers data, Data SLA, microstructure, market trend, setup,
fundamental/news, liquidity, entry/stop/TP, path to TP, costs, execution, risk
budget, portfolio/correlation, opportunity cost, journal, calibration,
adversarial review, reproducibility and strategy version. Every mandatory item
must be `PASS`; only calibration may be `NOT_REQUIRED` for a non-statistical
classification.

The selected AI provider receives only verified structured evidence and may critique or summarize
it. AI cannot change a hard-fail result, create a missing fact, supply a
probability or rescue a rejected candidate.

## Classification order

- `STATISTICALLY_QUALIFIED_70`: audit pass plus calibrated/admitted exact cohort
  and Wilson lower bound at least 70%.
- `PRODUCTION_QUALIFIED`: audit pass without the statistical-70 claim.
- `STRUCTURALLY_QUALIFIED`: strong setup, but one or more production
  prerequisites are incomplete.
- `WATCH`, `SHADOW`, `MODEL_CANDIDATE`: non-production observation states.
- `NO_TRADE`, `REJECT`, `CANCEL`: terminal/non-entry decisions.

One idea receives exactly one classification. In cold start no probability or
`STATISTICALLY_QUALIFIED_70` claim is permitted.

## Operational caveat

The audit/classification services are connected through
`IntradayV24Orchestrator`. Immediately before persistence/publication the
orchestrator recomputes Final Audit from the deterministic gate map, so a forged
or stale AI verdict cannot bypass a hard failure. Production publication still
remains disabled because current providers and unapproved policy values cannot
produce an honest all-gates PASS.
