# V2 QualityGate historical comparison

Dataset: `moex-liquid-20-2026q3`
Strategy version: `v2_ai_quality_filter`

AI is intentionally excluded from historical replay. Its incremental value is measured prospectively through the candidate experiment cohorts.

| Horizon | Confirmations | Before | After | Reduction | WR before | WR after | PF before | PF after |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| SWING_5D | 5 | 2016 | 406 | 79.86% | 37.40% | 36.02% | 0.886 | 0.901 |
| POSITION_1M | 5 | 723 | 207 | 71.37% | 42.14% | 46.02% | 1.139 | 1.297 |
| INTRADAY_1D | 4 | 0 | 0 | 0.00% | 0.00% | 0.00% | 0.000 | 0.000 |

## Limitations

- AI verdict is not replayed historically; doing so now would not reproduce the future live model state.
- Top-N and cross-ticker cooldown are forward batch controls and are not simulated by the per-instrument backtest.
- Historical daily-turnover snapshots are unavailable; the fixed liquid research universe is treated as meeting the configured liquidity floor.
