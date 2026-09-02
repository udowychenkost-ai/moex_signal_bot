# MASTER PROMPT v2.4 — gap analysis

Audit baseline: application `0.5.6`, branch `integrate-claude-version`, commit
`66f14878320449e8488ecd58bda89d5ad6b52dfc`, Alembic head
`20260827_0013`. The audit checks callable execution paths, not names of fields.

Status vocabulary:

- **IMPLEMENTED** — reachable production path and persistence/tests exist;
- **PARTIAL** — a reusable component exists, but the v2.4 contract is incomplete;
- **MISSING** — no real execution path exists;
- **BLOCKED BY DATA SOURCE** — safe interface/status can be built, facts cannot;
- **CONFIGURATION REQUIRED** — code can gate the action, user-approved limits are absent;
- **NOT APPLICABLE** — deliberately outside the no-broker scope.

## Safety baseline

The v2.4 layer will use strategy version `intraday_v2_4` and a new journal. It
will not mutate old `TradingIdea`, `TradingIdeaSnapshot`, `CandidateExperiment`,
`PaperTrade`, lifecycle or AI rows. Old horizon profiles and
`v2_ai_quality_filter` remain reproducible. Every new production classification
is fail-closed. Missing facts are `NULL` plus typed availability status, never a
market-looking zero. No broker adapter or order-placement operation is in scope.

## Requirement matrix

| § | Status | Existing execution path / table | Gap and proposed implementation | Migration impact | Test plan |
|---|---|---|---|---|---|
| 0. Version isolation | PARTIAL | `strategy_version` on ideas/experiments; immutable legacy snapshot | Introduce `intraday_v2_4`; never route legacy candidates through new rules | Additive journal/version columns only | Legacy cohort regression and row preservation |
| 1. Gap audit | IMPLEMENTED | This document | Keep it current after every phase | None | Documentation review plus file/path checks |
| 2. New INTRADAY mode | MISSING | Historical `INTRADAY_1D` in `domain.py`/`horizons.py` | Add separate `INTRADAY_V2_4`, max two trading days, trend-only/no-quota/no-trade behavior | New journal values; do not alter old enum rows | Profile/version, third-echelon and countertrend rejection tests |
| 3. Multi-timeframe | PARTIAL | `MoexClient`, `IngestionService`; 5m/15m/1h/1d/1w aware candles | Create v2.4 MTF view for D/60m/15m/5m; make 1m execution-only and unavailable when quality is insufficient | None expected | Resampling, timezone, closed-candle and no-1m-fabrication tests |
| 4. Technical extension | PARTIAL | `analysis.py`: EMA/SMA/MACD/ADX/RSI/Stoch/CCI/BB/ATR/OBV/RVOL/levels | Add VWAP, anchored-VWAP input architecture, previous-day/opening-range/gap/structure/compression/breakout states. Volume profile remains unavailable without intraday price-volume distribution | Snapshot JSON typed evidence | Deterministic feature fixtures and missing-volume-profile test |
| 5. Setup classification | MISSING | No setup enum/classifier | Deterministic `SetupType`/assessment with evidence, invalidation and timeframes; AI may critique only detected setup | Journal setup fields | Positive/negative fixtures; AI cannot invent setup |
| 6. Market Regime V2 | PARTIAL | `MarketRegimeService`: BULL/BEAR/SIDEWAYS and volatility | Add five-level trend, four-level vol, event availability and five-level bias as v2.4 view; trend-only direction gate | Journal snapshot/context JSON | Direction/no-trade matrix and causal-data tests |
| 7. Fundamental/news/cross-asset | PARTIAL / BLOCKED BY DATA SOURCE | Point-in-time `FundamentalReport` and official JSON provider | Add typed `FundamentalContext`, `NewsContext`, `CorporateEventContext`, `CrossAssetContext` envelopes. Keep news/cross-asset providers disabled and `DATA_NOT_AVAILABLE` until reliable sources are configured | Add append-only context records or snapshot JSON | Point-in-time, missing provider and no-fake-sentiment tests |
| 8. Data integrity | MISSING | Candle freshness guard only | Add `DataIntegrityService` for source class, timestamps, freshness, conflicts and critical fields; PASS/WARN/FAIL + confidence | Persist gate result in snapshot/journal | Conflicting/missing/stale source tests |
| 9. Data SLA | MISSING / CONFIGURATION REQUIRED | Timeframe freshness settings are strategy-data guards, not action SLA | Add versioned optional SLA settings and assessment. No ENTER_NOW/MOVE_STOP/CLOSE_NOW unless configured and PASS | New versioned settings table and snapshot fields | NOT_CONFIGURED/FAIL/PASS action-policy tests |
| 10. Microstructure guard | PARTIAL / BLOCKED BY DATA SOURCE | Market-hours helper, lot size, level-1 spread/freshness | Add guard for session, prices, lot/tick/spread/book. Borrow, full depth, bands and corporate actions return DATA_NOT_AVAILABLE unless sourced | Persist assessment JSON/status | Auction/halt/spread/stale/missing-field tests |
| 11. Persistent journal | MISSING | `TradingIdea`, snapshot, paper and events are legacy analogues only | Add `IdeaJournal`, `ModelTradeJournal`, `ActualTradeJournal`, `TradeEventJournal` under one service | Four additive tables plus trade ID counter | Persistence/restart and transactional-write tests |
| 12. Decision snapshot | PARTIAL | `TradingIdeaSnapshot` is immutable but lacks v2.4 gate matrix | Add one-to-one immutable `DecisionSnapshotV24` linked by trade ID; no backfill from current facts | Additive table | Insert once, update rejection and missing-evidence tests |
| 13. Idea journal | MISSING | No v2.4 typed journal row | Store specified nullable values and typed statuses; original row is immutable after creation | Additive table with JSON evidence for evolving schemas | Full round-trip and no-fake-zero tests |
| 14. Model trade journal | MISSING | Legacy `PaperTrade` cannot represent uncertain fills/calibration | Add separate model trade entity/sample/fill statuses and metrics | Additive table | Model lifecycle and nullable outcome tests |
| 15. Model execution engine | PARTIAL | Backtest/lifecycle has conservative same-candle handling and slippage | Add explicit order/fill assessment, lower-TF disambiguation hook, uncertain/excluded outcome, first-available gap stop | Model fields/events | LIMIT uncertainty, ambiguity and gap-stop tests |
| 16. Actual trade journal | MISSING | No user-confirmed real-execution record | Add separate actual trade table; never create from market/lifecycle automatically | Additive table | Explicit-confirmation-only and model/actual separation tests |
| 17. Telegram actual confirmation | MISSING | Existing callbacks do not create actual positions | Add FSM/callback flow for price, shares/amount, time and optional commission; actions remain advisory and require confirmation | Telegram state remains ephemeral; results persist as events | Authorization, validation, idempotency and cancellation tests |
| 18. Append-only event journal | MISSING | Legacy idea events are mutable only by convention and lack actual/model fields | Add append-only service/table; application exposes insert/list only | Additive table | Update/delete prevention at service/DB trigger level and ordering tests |
| 19. Entry model | PARTIAL | Entry zone + material reassessment in `ideas.py` | Add optimal/acceptable/no-chase and immutable execution reassessment event | Snapshot + event JSON | Chased-price and `SIGNAL_VALID_EXECUTION_INVALID` tests |
| 20. Path to TP | MISSING | Support/resistance affects legacy level risk only | Add typed CLEAN/ACCEPTABLE/BLOCKED/UNKNOWN assessment from verified barriers | Snapshot fields | Blocked/unknown/clean fixtures |
| 21. Cost model | PARTIAL / CONFIGURATION REQUIRED | Paper/backtest commission and slippage settings | Add typed CONFIGURED/PARTIAL/NOT_CONFIGURED cost inputs; separate gross/net RR. No assumed broker commission | Versioned configuration or journal JSON | Missing/partial/configured and direction tests |
| 22. Risk budget | MISSING / CONFIGURATION REQUIRED | Per-user risk percentage and paper capital are not portfolio limits | Add versioned persistent optional limits and Telegram configuration. Defaults are NULL/NOT_CONFIGURED | Additive settings/version table | Version immutability and no full-risk pass tests |
| 23. Risk engine | PARTIAL | ATR/level stop and lot-aware sizing in `risk.py` | Add stress loss/cost/caps and deterministic factor exposure; precise correlation stays unavailable | Snapshot/journal fields | Stress, portfolio heat and missing-correlation tests |
| 24. Liquidity model V2 | PARTIAL / BLOCKED BY DATA SOURCE | `LiquidityService`: ADV20, turnover, spread, optional depth | Extend typed caps and exit scenarios only from known inputs. Public `quantity=NULL` keeps depth caps unavailable | Snapshot fields only | NULL depth, min-cap and exit availability tests |
| 25. Position size | PARTIAL / CONFIGURATION REQUIRED | Basic risk/cash sizing and read-only comfortable size | Compute min of available approved caps; if any mandatory cap is missing return NOT_RELIABLY_CALCULABLE, not a number | Journal fields | Cap minimum and missing-cap tests |
| 26. Opportunity cost | MISSING | Scanner ranks quality score only | Add qualitative cold-start rank; enable EV/capital-time only for calibrated comparable candidates | Persist ranking/evidence | EV-disabled fallback and deterministic ordering tests |
| 27. Statistical admission | MISSING / CONFIGURATION REQUIRED | Research split config is not a production admission contract | Add append-only versioned thresholds; NOT_CONFIGURED by default | Additive table | Historical-version selection and threshold tests |
| 28. Calibration engine | MISSING | No probability calibration service | Add Wilson-based status/service grouped by comparable model trades; no probability until CALIBRATED | No mandatory new table beyond config/journal | Wilson lower bound/width/sample/degradation tests |
| 29. Brier/ECE | MISSING | AI score is correctly not called probability | Add pure metrics for eligible stated probabilities only | Model outcome fields already planned | Brier/ECE/bucket/exclusion tests |
| 30. Cold start | MISSING | Existing V2 publishes AI-approved ideas without v2.4 classification | v2.4 permits only structural/watch/shadow/model statuses until admission/calibration | Journal classification field | No statistical-70 claim test |
| 31. Global statistics | PARTIAL | `/stats` has generated/activated/TP/SL/PF/R/P&L | Add v2.4 journal stats and keep MODEL/ACTUAL separate | None | Separation and metric fixtures |
| 32. Setup statistics | MISSING | No setup dimension | Aggregate eligible outcomes by deterministic setup and conservative status | Optional status table; derived by default | Small-N/no-random-retire tests |
| 33. Regime statistics | MISSING | Existing cohort stats only by horizon/AI cohort | Add comparable dimensional grouping; insufficient N => UNKNOWN | None | Group isolation/small-N tests |
| 34. Rolling/degradation | MISSING | No 20/50/100 model comparison | Add rolling metrics and non-mutating degradation detector | Persist kill-switch evidence/event, not strategy mutation | Window and material-degradation tests |
| 35. Model/actual comparison | MISSING | No actual journal | Add direction-correct entry/exit slippage and P&L gap | Derived from new tables | LONG/SHORT sign and separation tests |
| 36. Open-position management | PARTIAL | Legacy tracker/paper scheduler exists | New priority path only for v2.4 actual/model positions; advisory assessment, no legacy changes | Event records | Priority and legacy-isolation tests |
| 37. Stop management | MISSING | Legacy stop is fixed | Add advisory checkpoint/structural trailing assessment; changes only after explicit user event | Append-only events | R checkpoints and no-auto-update tests |
| 38. Partial exit/runner | MISSING | Legacy paper closes fully | Add user-confirmed partial actual events and remaining-position projection | Events/actual summary fields | Partial arithmetic and snapshot immutability tests |
| 39. Time stop/overnight | PARTIAL | Existing expiry uses calendar timedelta | Add two-trading-day v2.4 deadline and advisory overnight assessment; no automatic actual action | Journal/event fields | Weekend/holiday-conservative deadline tests |
| 40. Re-entry | MISSING | Legacy material updates can reuse an open idea | v2.4 journal service always allocates a new trade ID | None beyond journal | New-ID test |
| 41. Daily/portfolio guards | MISSING / CONFIGURATION REQUIRED | No actual risk budget | Apply only configured versioned limits; otherwise not full pass | Uses risk settings table | Daily loss/heat/missing config tests |
| 42. Kill switch | MISSING | Scheduler job health is diagnostic only | Add persisted NORMAL/CAPITAL_PRESERVATION state/reasons; block new v2.4 positions | Additive singleton/history tables | Blocking, persistence and reset audit tests |
| 43. Adversarial check | MISSING | Gemini bull/bear analysis is not a hard gate | Add deterministic PASS/FAIL/WAIT with optional AI critique after verified snapshot | Snapshot evidence | Hard fail priority and missing-context tests |
| 44. Final audit | MISSING | QualityGate and AI do not cover v2.4 matrix | Add all-mandatory-pass `FinalAuditService`; calibration may be NOT_REQUIRED | Snapshot gate matrix | One-fail-blocks and AI-cannot-override tests |
| 45. Final classification | MISSING | Legacy idea statuses are lifecycle states | Add separate v2.4 classification enum and strict classifier | Idea journal field | Cold-start/production/statistical rules |
| 46. AI pipeline V2.4 | PARTIAL | Provider/schema/no-invention/fallback telemetry is production-grade | Feed only verified v2.4 snapshot after hard gates; AI remains critique, never fact/probability source | AI logs can reuse request kind + usage JSON | Hard-gate short-circuit and prompt-contract tests |
| 47. Telegram idea card | MISSING | Legacy cards lack v2.4 gates/caps/probability status | Add compact v2.4 formatter and drill-down; unavailable values explicit | None | Render configured/unconfigured/missing cases |
| 48. Active actual card | MISSING | `/portfolio` is model paper only | Add actual card with timestamped price and advisory status | None | Rendering and source timestamp tests |
| 49. Journal update block | MISSING | No new journal service | Return typed persistent operation result for every mutation | Event journal | Storage reference/idempotency tests |
| 50. Daily journal summary | MISSING | Existing daily forward summary has idea counts only | Persist separate model/actual daily summary and observation-only lessons | Additive daily summary table or immutable report row | Daily aggregation/separation tests |
| 51. Daily market summary | PARTIAL | Scheduled daily summary + outbox dedup exists | Add first-after-11:00 idempotent market summary and material-alert policy | Reuse outbox plus report key | Once-per-day/restart/no-spam tests |
| 52. Journal storage status | MISSING | Generic DB/Alembic health exists | Add transactional journal probe/readiness check; broken journal hard-fails v2.4 | Uses required schema | Writable/read-only/missing migration tests |
| 53. Migrations | IMPLEMENTED infrastructure / MISSING v2.4 schema | Alembic chain through `0013`, legacy adoption | Add non-destructive revisions and previous-head upgrade tests | Multiple additive revisions by phase | Upgrade `0013 -> head`, row preservation and fresh DB |
| 54. Observability | PARTIAL | Job summaries, scan funnel, provider telemetry | Add structured gate/journal/classification/model/actual counters without secrets | Job details / logs; optional daily row | Log/metric payload tests |
| 55. `/status` | PARTIAL | App/git/DB/MOEX/orderbook/jobs/scheduler shown; Gemini has separate screen | Add Alembic, journal, SLA, risk budget, admission, calibration and kill switch | None | Compact status rendering for all states |
| 56. Config | PARTIAL / CONFIGURATION REQUIRED | Pydantic settings validate existing thresholds | Add explicit optional SLA/risk/admission/cost inputs; unset remains NOT_CONFIGURED | Versioned DB settings where historical semantics matter | Env parsing and invalid combination tests |
| 57. Testing | MISSING for v2.4 | Existing suite has 209 tests | Add requested unit/integration/migration/idempotency/restart cases phase-by-phase | Test-only | Full numbered acceptance matrix |
| 58. Required commands | IMPLEMENTED workflow | pytest/Ruff/compileall/pip; Compose YAML validated | Re-run exact commands. Docker only if actually available; never claim otherwise | None | Command transcript in final report |
| 59. Documentation | MISSING / PARTIAL | README/DEPLOY/architecture docs exist for v2.1.5.1 | Add all seven requested v2.4 documents and update runbooks/data boundaries | None | Link/file consistency check |
| 60. No fake completeness | IMPLEMENTED policy / PARTIAL architecture | Fundamentals and public order book already fail safely | Apply same availability envelopes to news/cross-asset/borrow/corporate facts | Context persistence | No-provider/no-fake-value tests |
| 61. Production safety | IMPLEMENTED | Non-destructive Alembic, persistent volume warnings, backup runbook | Retain and extend backup instructions for journal | Additive only | Migration/data-preservation tests |
| 62. Definition of done | NOT APPLICABLE YET | Baseline only | Track completion against this matrix; do not claim production-ready while gates can bypass | All phases | Final acceptance checklist |
| 63. Workflow | IMPLEMENTED FOR THIS CHANGE | Phase plan and atomic commits | Targeted tests/lint and gap update after every phase | Per-phase revisions | Git log plus clean status |
| 64. Final report | NOT APPLICABLE YET | No v2.4 implementation report | Deliver exact results, blocked providers, commits and deploy verification | None | Cross-check command outputs |

## Existing data-source reality

| Source | Current reality | v2.4 behavior |
|---|---|---|
| MOEX candles/marketdata | Official ISS, public data may be delayed | Timestamp/source/delay propagated; SLA decides allowed action |
| Public best bid/offer | Real level-1; public `BIDDEPTH/OFFERDEPTH` can be `NULL` | Spread can be known; depth cap stays unavailable |
| Full L2 | Subscription-dependent | `DATA_NOT_AVAILABLE` without callable subscribed endpoint |
| Fundamentals | Reviewed official JSON provider with point-in-time semantics | Use only available official records |
| Real-time news/events | No reliable configured provider | Typed interface, persistence and `DATA_NOT_AVAILABLE`; no sentiment |
| Cross-asset | IMOEX/RTSI/RGBITR/RVI candles exist; RUB/oil/commodities feed absent | Only verified indices; remaining components unavailable |
| Borrow/SHORT availability | No reliable provider | `DATA_NOT_AVAILABLE`; cannot produce ENTER_NOW short approval |
| Broker fills/fees | Only user can know actual execution | Actual journal requires explicit Telegram confirmation |

## Implementation order and migration plan

1. **Journal foundation**: additive tables for trade sequence, idea/snapshot,
   model, actual and append-only event journal.
2. **Hard prerequisite gates**: integrity, optional versioned SLA and
   microstructure availability.
3. **Risk/config layer**: versioned risk budget, cost/liquidity caps and
   reliable-calculation statuses.
4. **Intraday strategy**: isolated profile/features/setup/execution assessments.
5. **User-confirmed actual execution**: Telegram FSM plus append-only events.
6. **Calibration/statistics**: admission configuration and pure metrics.
7. **Kill switch/audit**: persisted state and all-mandatory hard gate.
8. **Presentation/observability**: cards, status and daily reports.

Every revision is forward-only and additive. No historical v1/v2 row is
backfilled with current evidence. A missing historical v2.4 decision snapshot is
reported as `MISSING_HISTORICAL_EVIDENCE`.

## Audit conclusion

The baseline is strong enough to reuse its MOEX, async DB, analysis, legacy
strategy, Gemini, Telegram, scheduler and migration infrastructure. It is not a
v2.4 production layer yet. The principal missing boundary is an auditable,
versioned, fail-closed journal/gate system separating model and user-confirmed
actual execution. Real-time news, borrow information, full public L2 and several
cross-asset facts are externally blocked and must remain unavailable until a
reliable provider is explicitly configured.

## Post-implementation status (0.6.0)

The table above is the immutable phase-0 baseline. The implementation was then
delivered in atomic phases through Alembic `20260901_0020`. This section is the
authoritative final status; it deliberately distinguishes implemented reusable
services from a registered live production path.

| Sections | Final status | Evidence / remaining gap |
|---|---|---|
| 0–1 | IMPLEMENTED | `intraday_v2_4` isolation and this baseline/final audit |
| 2 | PARTIAL | Isolated config/profile, universe/leverage/holding constraints exist; live scan-to-journal orchestrator is not registered |
| 3 | IMPLEMENTED | Canonical ingestion for 1d/1h/15m/5m; 1m stays optional and quality-gated |
| 4 | PARTIAL / BLOCKED BY DATA SOURCE | Extended technical snapshot, VWAP, structure, ranges and anchors exist; reliable volume profile is unavailable |
| 5–6 | IMPLEMENTED | Deterministic setup classifier and V2.4 regime/bias services |
| 7 | PARTIAL / BLOCKED BY DATA SOURCE | Point-in-time context envelopes/persistence; no fabricated real-time news, events, full cross-asset or borrow facts |
| 8–10 | IMPLEMENTED / DATA DEPENDENT | Integrity, SLA and microstructure guards fail closed; production PASS depends on configured/available facts |
| 11–18 | IMPLEMENTED | Separate immutable idea/snapshot, MODEL/ACTUAL, append-only events and explicit Telegram confirmation |
| 19–20 | IMPLEMENTED | Entry/no-chase/reassessment and path-to-target services |
| 21 | IMPLEMENTED / CONFIGURATION REQUIRED | Gross/net cost model; no assumed broker costs |
| 22 | PARTIAL / CONFIGURATION REQUIRED | Persistent versioned policy exists; dedicated Telegram administrator risk-policy wizard remains missing |
| 23 | IMPLEMENTED | Stress loss, heat/factor caps and fail-closed correlation behavior |
| 24 | PARTIAL / BLOCKED BY DATA SOURCE | Scenario caps implemented; full depth-dependent cap unavailable on public quantity-less level-1 |
| 25–26 | IMPLEMENTED / CONFIGURATION REQUIRED | Min-cap sizing and calibrated/cold-start opportunity ranking; missing inputs do not produce numbers |
| 27–35 | IMPLEMENTED | Admission, Wilson/Brier/ECE, cold start, MODEL/ACTUAL and rolling/setup/regime/degradation statistics |
| 36–39 | PARTIAL | Advisory manager, priorities, stop/runner/time checkpoints and manual events exist; live current-mark orchestration/overnight provider is not registered |
| 40 | IMPLEMENTED | Every re-entry allocates a new concurrent-safe trade ID |
| 41 | IMPLEMENTED / CONFIGURATION REQUIRED | Engine enforces configured guards; no user policy means no full pass |
| 42–45 | IMPLEMENTED | Persistent fail-closed kill switch, adversarial check, all-mandatory audit and exclusive classification |
| 46 | PARTIAL | Existing Gemini provider is safe and bounded; V2.4 verified-snapshot adapter awaits the live orchestrator |
| 47–51 | IMPLEMENTED | Compact/full cards, actual card, persistent updates, immutable daily report and idempotent after-11 market outbox |
| 52–56 | IMPLEMENTED / CONFIGURATION REQUIRED | Transactional journal health, additive migrations, structured status/metrics and explicit optional settings |
| 57–58 | IMPLEMENTED | Numbered unit/integration/migration coverage and recorded final commands |
| 59 | IMPLEMENTED | Requested V2.4 documents and deployment runbook |
| 60–61 | IMPLEMENTED | Missing provider facts remain unavailable; migrations/backups are non-destructive |
| 62 | NOT COMPLETE FOR LIVE V2.4 | Persistence and gates are ready, but production publication stays disabled until the orchestrator/provider/config gaps close |
| 63–64 | IMPLEMENTED | Atomic phase commits and final evidence report |

### Final conclusion

The reusable V2.4 journal, safety, analysis, execution, statistics, audit,
Telegram presentation and observability layers are implemented without changing
legacy strategy behavior. This is not claimed as an enabled production trading
signal path: `INTRADAY_V24_ENABLED=false` remains the safe deployment default.
The two implementable integration gaps are the end-to-end scan-to-journal/live
position orchestrator and a dedicated administrator workflow for versioned risk
policy. Provider-dependent gaps remain full L2, borrow, real-time news,
corporate actions, some cross-asset data and actual broker costs/fills.

## Final integration update (0.7.0)

Alembic `20260902_0021` and the final integration phase close the two previously
identified implementation gaps:

- `IntradayV24Orchestrator` now composes the existing fail-closed services and
  commits candidate claim, immutable journal/snapshot, optional model record and
  notification outbox atomically;
- the single scheduler runs ACTUAL recovery, MODEL lifecycle, ingestion and
  V2.4 scan without coupling failures to the legacy scan;
- persistent `LEGACY_ONLY` / `INTRADAY_V24_ONLY` / `BOTH` modes control
  user-facing delivery and views without mixing cohorts or deleting history;
- the Telegram admin Risk Budget wizard creates confirmed, effective-dated
  immutable policy versions;
- snapshot policy references and `strategy_family` make decisions reproducible
  and prevent cross-strategy deduplication.

This changes implementation readiness, not market-data or statistical
readiness. Production remains `INTRADAY_V24_ENABLED=false`; the exact remaining
provider/configuration/calibration gates are listed in
`INTRADAY_V2_4_PRODUCTION_CHECKLIST.md`.
