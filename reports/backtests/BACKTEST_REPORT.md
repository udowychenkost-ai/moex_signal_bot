# Backtest validation and calibration report

Generated: 2026-08-19T20:24:14.747023+00:00

## Executive verdict

| Horizon | Selected | OOS PF | OOS expectancy | OOS net P&L | Verdict |
|---|---|---:|---:|---:|---|
| INTRADAY_1D | weighted_trend_context | 0.51 | -0.46 R | -55329.76 RUB | REJECT: no positive OOS expectancy after execution costs. |
| SWING_5D | weighted_trend_context | 1.00 | -0.00 R | 586.24 RUB | REJECT: no positive OOS expectancy after execution costs. |
| POSITION_1M | legacy_default | 1.14 | 0.07 R | 20969.06 RUB | MODEST POSITIVE EDGE: suitable for forward paper validation, not real capital. |

## Method and limitations

- Signals are formed only from candles closed at the decision time and execute no earlier than the next primary candle.
- TRAIN selects a shortlist; VALIDATION selects the configuration; OOS TEST is not used for parameter selection.
- Each walk-forward fold recalibrates between the predeclared fixed legacy and weighted defaults using only that fold's past.
- The universe is a fixed current-liquid universe, so survivorship bias remains.
- MOEX candles are not adjusted for corporate actions; historical returns around splits/dividends can be distorted.
- Sharpe is trade-level and non-annualized. Same-candle TP/SL ambiguity is resolved conservatively as SL first.
- Maximum drawdown uses realized equity after trade closes, not intratrade mark-to-market, and can understate adverse excursion.
- Research uses causal batch indicator features for runtime. Every ticker/timeframe is sampled against the canonical 500-candle analyzer; the run aborts on score, level, or material numeric divergence.

## Dataset splits

| Horizon | Primary | TRAIN | VALIDATION | OOS TEST | Tickers |
|---|---|---|---|---|---:|
| INTRADAY_1D | 15m | 2026-01-01T00:00:00+00:00 → 2026-05-18T22:05:59.400001+00:00 | 2026-05-18T22:05:59.400001+00:00 → 2026-07-03T21:27:59.200001+00:00 | 2026-07-03T21:27:59.200001+00:00 → 2026-08-18T20:49:59.000001+00:00 | 20 |
| SWING_5D | 4h | 2020-01-01T00:00:00+00:00 → 2023-12-24T02:59:59.400001+00:00 | 2023-12-24T02:59:59.400001+00:00 → 2025-04-21T11:59:59.200001+00:00 | 2025-04-21T11:59:59.200001+00:00 → 2026-08-18T20:59:59.000001+00:00 | 20 |
| POSITION_1M | 1d | 2014-01-01T00:00:00+00:00 → 2021-07-30T17:23:59.400001+00:00 | 2021-07-30T17:23:59.400001+00:00 → 2024-02-08T07:11:59.200001+00:00 | 2024-02-08T07:11:59.200001+00:00 → 2026-08-18T20:59:59.000001+00:00 | 20 |

## INTRADAY_1D — OUT-OF-SAMPLE

Selected configuration: `weighted_trend_context` (`weighted`)

Verdict: **REJECT: no positive OOS expectancy after execution costs.**

| Metric | Result |
|---|---:|
| Ideas | 680 |
| Activated | 473 |
| Activation rate | 69.56% |
| TP / SL / expired / invalidated | 126 / 338 / 10 / 206 |
| Win rate | 27.70% |
| Profit factor | 0.51 |
| Expectancy | -0.46 R |
| Average / median R | -0.46 / -1.15 |
| Maximum drawdown | 5.53% |
| Sharpe | -0.31 |
| Average holding | 3.96 h |
| Gross P&L | -10550.30 RUB |
| Commission | 22389.73 RUB |
| Slippage | 22389.73 RUB |
| Net P&L | -55329.76 RUB |

### Fixed legacy vs weighted baselines

| Period | Model | Ideas | Activated | Win rate | Profit factor | Expectancy R | Max DD | Net P&L |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| TRAIN | legacy | 9915 | 7674 | 26.88% | 0.32 | -0.68 | 54.61% | -546050.52 |
| TRAIN | weighted | 3178 | 2473 | 27.21% | 0.33 | -0.67 | 22.58% | -225728.30 |
| VALIDATION | legacy | 3631 | 2694 | 29.25% | 0.47 | -0.53 | 22.93% | -229259.53 |
| VALIDATION | weighted | 1283 | 938 | 29.96% | 0.49 | -0.50 | 8.26% | -82550.46 |
| TEST | legacy | 2906 | 2033 | 32.17% | 0.68 | -0.33 | 14.26% | -140273.13 |
| TEST | weighted | 962 | 654 | 30.12% | 0.58 | -0.40 | 6.28% | -62564.16 |

### Selected configuration stability

| Period | Ideas | Activated | Profit factor | Expectancy R | Max DD | Net P&L |
|---|---:|---:|---:|---:|---:|---:|
| TRAIN | 2238 | 1761 | 0.36 | -0.65 | 15.82% | -158101.47 |
| VALIDATION | 1007 | 730 | 0.52 | -0.47 | 6.22% | -62238.42 |
| TEST | 680 | 473 | 0.51 | -0.46 | 5.53% | -55329.76 |

### OOS research-only benchmarks

| Strategy | Trades | Win rate | Profit factor | Net P&L | Max DD |
|---|---:|---:|---:|---:|---:|
| buy_hold | 20 | 50.00% | 0.62 | -23629.66 | 3.50% |
| ema_trend | 1989 | 15.79% | 0.65 | -148050.79 | 16.67% |
| rsi_reversion | 334 | 56.89% | 0.52 | -81166.28 | 8.82% |

### Top / worst tickers

| Top | Net P&L | Worst | Net P&L |
|---|---:|---|---:|
| MGNT | 653.33 | NLMK | -6112.73 |
| MTSS | -9.83 | GMKN | -5294.75 |
| CHMF | -273.37 | LKOH | -4541.69 |
| NVTK | -552.76 | VTBR | -4120.98 |
| ALRS | -1919.35 | GAZP | -3854.99 |

### BUY vs SELL

| Direction | Ideas | Activated | Win rate | Expectancy R | Net P&L |
|---|---:|---:|---:|---:|---:|
| SELL | 384 | 277 | 27.08% | -0.45 | -35511.48 |
| BUY | 296 | 196 | 28.57% | -0.47 | -19818.28 |

### Confidence calibration

| Bucket | Ideas | Activated | Win rate | Expectancy R | Profit factor |
|---|---:|---:|---:|---:|---:|
| 60-69 | 673 | 467 | 27.62% | -0.46 | 0.51 |
| 70-79 | 7 | 6 | 33.33% | -0.36 | 0.55 |

Insufficient activated ideas per confidence bucket to assess confidence ordering reliably.

### Calendar-year OOS breakdown

| Year | Ideas | Activated | Win rate | Expectancy R | Net P&L |
|---|---:|---:|---:|---:|---:|
| 2026 | 680 | 473 | 27.70% | -0.46 | -55329.76 |

### Walk-forward aggregate unseen windows

Folds: 3; ideas: 3357; activated: 2451; profit factor: 0.47; expectancy: -0.55 R; net P&L: -235314.89 RUB.

## SWING_5D — OUT-OF-SAMPLE

Selected configuration: `weighted_trend_context` (`weighted`)

Verdict: **REJECT: no positive OOS expectancy after execution costs.**

| Metric | Result |
|---|---:|
| Ideas | 712 |
| Activated | 556 |
| Activation rate | 78.09% |
| TP / SL / expired / invalidated | 142 / 298 / 120 / 146 |
| Win rate | 39.75% |
| Profit factor | 1.00 |
| Expectancy | -0.00 R |
| Average / median R | -0.00 / -1.04 |
| Maximum drawdown | 2.11% |
| Sharpe | 0.00 |
| Average holding | 52.59 h |
| Gross P&L | 37071.63 RUB |
| Commission | 18242.69 RUB |
| Slippage | 18242.70 RUB |
| Net P&L | 586.24 RUB |

### Fixed legacy vs weighted baselines

| Period | Model | Ideas | Activated | Win rate | Profit factor | Expectancy R | Max DD | Net P&L |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| TRAIN | legacy | 3785 | 2845 | 44.39% | 1.15 | 0.07 | 2.57% | 109347.06 |
| TRAIN | weighted | 1323 | 959 | 44.32% | 1.12 | 0.06 | 1.20% | 29309.73 |
| VALIDATION | legacy | 1558 | 1158 | 43.01% | 1.07 | 0.04 | 2.80% | 21566.27 |
| VALIDATION | weighted | 600 | 421 | 43.47% | 1.09 | 0.06 | 1.42% | 10048.66 |
| TEST | legacy | 2014 | 1565 | 37.06% | 0.89 | -0.08 | 8.23% | -51876.26 |
| TEST | weighted | 888 | 685 | 39.85% | 1.05 | 0.02 | 1.98% | 9404.12 |

### Selected configuration stability

| Period | Ideas | Activated | Profit factor | Expectancy R | Max DD | Net P&L |
|---|---:|---:|---:|---:|---:|---:|
| TRAIN | 1077 | 785 | 1.20 | 0.10 | 0.89% | 37417.38 |
| VALIDATION | 511 | 380 | 1.30 | 0.16 | 1.05% | 28769.46 |
| TEST | 712 | 556 | 1.00 | -0.00 | 2.11% | 586.24 |

### OOS research-only benchmarks

| Strategy | Trades | Win rate | Profit factor | Net P&L | Max DD |
|---|---:|---:|---:|---:|---:|
| buy_hold | 20 | 5.00% | 0.00 | -296671.60 | 29.67% |
| ema_trend | 1434 | 17.09% | 0.66 | -172192.53 | 24.32% |
| rsi_reversion | 327 | 56.27% | 0.58 | -139143.07 | 17.00% |

### Top / worst tickers

| Top | Net P&L | Worst | Net P&L |
|---|---:|---|---:|
| PLZL | 4169.45 | TATN | -5475.78 |
| VTBR | 3471.40 | GAZP | -5207.50 |
| MGNT | 3408.41 | ROSN | -4337.20 |
| PHOR | 3383.79 | SBER | -4252.66 |
| LKOH | 3041.27 | YDEX | -2741.27 |

### BUY vs SELL

| Direction | Ideas | Activated | Win rate | Expectancy R | Net P&L |
|---|---:|---:|---:|---:|---:|
| BUY | 176 | 144 | 40.97% | -0.01 | -278.70 |
| SELL | 536 | 412 | 39.32% | 0.00 | 864.95 |

### Confidence calibration

| Bucket | Ideas | Activated | Win rate | Expectancy R | Profit factor |
|---|---:|---:|---:|---:|---:|
| 60-69 | 696 | 544 | 39.52% | -0.01 | 0.99 |
| 70-79 | 16 | 12 | 50.00% | 0.25 | 1.69 |

Insufficient activated ideas per confidence bucket to assess confidence ordering reliably.

### Calendar-year OOS breakdown

| Year | Ideas | Activated | Win rate | Expectancy R | Net P&L |
|---|---:|---:|---:|---:|---:|
| 2025 | 400 | 309 | 38.83% | -0.04 | -6707.88 |
| 2026 | 312 | 247 | 40.89% | 0.05 | 7294.12 |

### Walk-forward aggregate unseen windows

Folds: 3; ideas: 1912; activated: 1406; profit factor: 1.08; expectancy: 0.04 R; net P&L: 28823.46 RUB.

## POSITION_1M — OUT-OF-SAMPLE

Selected configuration: `legacy_default` (`legacy`)

Verdict: **MODEST POSITIVE EDGE: suitable for forward paper validation, not real capital.**

| Metric | Result |
|---|---:|
| Ideas | 722 |
| Activated | 598 |
| Activation rate | 82.83% |
| TP / SL / expired / invalidated | 122 / 297 / 186 / 101 |
| Win rate | 42.31% |
| Profit factor | 1.14 |
| Expectancy | 0.07 R |
| Average / median R | 0.07 / -0.73 |
| Maximum drawdown | 2.45% |
| Sharpe | 0.06 |
| Average holding | 377.42 h |
| Gross P&L | 33432.35 RUB |
| Commission | 6231.64 RUB |
| Slippage | 6231.65 RUB |
| Net P&L | 20969.06 RUB |

### Fixed legacy vs weighted baselines

| Period | Model | Ideas | Activated | Win rate | Profit factor | Expectancy R | Max DD | Net P&L |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| TRAIN | legacy | 1696 | 1418 | 41.89% | 1.07 | 0.03 | 1.38% | 24382.18 |
| TRAIN | weighted | 1011 | 810 | 41.73% | 1.07 | 0.03 | 1.11% | 12263.19 |
| VALIDATION | legacy | 567 | 457 | 47.48% | 1.56 | 0.24 | 0.95% | 50434.48 |
| VALIDATION | weighted | 276 | 217 | 45.62% | 1.48 | 0.19 | 0.58% | 19677.41 |
| TEST | legacy | 722 | 598 | 42.31% | 1.14 | 0.07 | 2.45% | 20969.06 |
| TEST | weighted | 375 | 310 | 43.87% | 1.21 | 0.11 | 2.05% | 15391.06 |

### Selected configuration stability

| Period | Ideas | Activated | Profit factor | Expectancy R | Max DD | Net P&L |
|---|---:|---:|---:|---:|---:|---:|
| TRAIN | 1696 | 1418 | 1.07 | 0.03 | 1.38% | 24382.18 |
| VALIDATION | 567 | 457 | 1.56 | 0.24 | 0.95% | 50434.48 |
| TEST | 722 | 598 | 1.14 | 0.07 | 2.45% | 20969.06 |

### OOS research-only benchmarks

| Strategy | Trades | Win rate | Profit factor | Net P&L | Max DD |
|---|---:|---:|---:|---:|---:|
| buy_hold | 20 | 0.00% | 0.00 | -373669.92 | 37.37% |
| ema_trend | 512 | 18.95% | 0.80 | -73906.88 | 11.00% |
| rsi_reversion | 125 | 56.80% | 0.59 | -141239.23 | 17.26% |

### Top / worst tickers

| Top | Net P&L | Worst | Net P&L |
|---|---:|---|---:|
| NLMK | 6961.52 | TATN | -3214.56 |
| PLZL | 5980.18 | MOEX | -2603.63 |
| GMKN | 4035.30 | NVTK | -2129.32 |
| CHMF | 3927.14 | LKOH | -1836.29 |
| GAZP | 3751.38 | MTSS | -1587.75 |

### BUY vs SELL

| Direction | Ideas | Activated | Win rate | Expectancy R | Net P&L |
|---|---:|---:|---:|---:|---:|
| BUY | 270 | 229 | 36.68% | -0.12 | -10200.26 |
| SELL | 452 | 369 | 45.80% | 0.18 | 31169.31 |

### Confidence calibration

| Bucket | Ideas | Activated | Win rate | Expectancy R | Profit factor |
|---|---:|---:|---:|---:|---:|
| 60-69 | 667 | 554 | 43.14% | 0.10 | 1.21 |
| 70-79 | 55 | 44 | 31.82% | -0.29 | 0.51 |

WARNING: OOS expectancy is not monotonic across confidence buckets; confidence is not calibrated as a reliable quality rank.

### Calendar-year OOS breakdown

| Year | Ideas | Activated | Win rate | Expectancy R | Net P&L |
|---|---:|---:|---:|---:|---:|
| 2024 | 230 | 183 | 42.62% | 0.06 | 4444.53 |
| 2025 | 290 | 254 | 38.58% | -0.08 | -7824.46 |
| 2026 | 202 | 161 | 47.83% | 0.31 | 24348.99 |

### Walk-forward aggregate unseen windows

Folds: 3; ideas: 1605; activated: 1306; profit factor: 1.16; expectancy: 0.07 R; net P&L: 49524.80 RUB.

## Interpretation guardrails

A profitable result is not proof of future performance. A negative or unstable OOS result is retained as-is; no post-hoc TEST optimization is performed.
