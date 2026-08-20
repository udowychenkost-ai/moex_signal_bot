# Market context and fundamentals — pre-deploy boundary

## Production decision

`baseline_v1` remains the production scoring default until a contextual variant
beats it out of sample and across walk-forward folds. All new context is still
ingested and frozen in the decision snapshot, so forward observation can audit
the factors without silently changing historical signal behavior.

## Market context

IMOEX is fetched by the existing async MOEX ISS client into `market_candles`.
The benchmark uses the same completed-candle rule as stocks. The regime starts
as a continuous score in `[-100, 100]` from:

- close versus EMA20, EMA50 and SMA200;
- EMA50 20-bar slope and 20-bar index return;
- 60-bar drawdown from the local high.

Scores at least +25 are `BULL`, scores at most -25 are `BEAR`, and the middle
band is `SIDEWAYS`. Volatility is a separate causal LOW/NORMAL/HIGH/EXTREME
state based on the percentile of 20-bar annualized realized volatility within
the preceding 250 observations. ATR percentage is retained as diagnostics.

RTSI, RGBITR and RVI are ingested daily when available, but do not block ideas.
IMOEX is the only mandatory benchmark in this version. The public Central Bank
interfaces remain suitable future providers for USD/RUB and key-rate context;
they are not mixed into the current score without OOS evidence.

Relative strength equals the instrument 20-bar return minus the matching IMOEX
20-bar return. The difference is scaled and bounded to `[-100, 100]`; labels are
`выше рынка`, `на уровне рынка` and `ниже рынка`.

## Technical components

The contextual technical score is a weighted sum configured in each
`HorizonProfile`:

| Component | Meaning |
|---|---|
| trend | EMA/SMA structure, price position and ADX trend quality |
| momentum | RSI/MACD/Stochastic directional continuation |
| momentum_extreme | confirmed recovery/rollover around RSI/Stochastic/CCI/Bollinger extremes |
| volume | directional relative volume, normalized OBV change, weak-move penalty |
| levels | proximity and volume-confirmed support/resistance breaks |
| volatility | ATR quality applied in the direction of the prevailing trend |
| relative_strength | stock return relative to IMOEX |
| market_regime | continuous IMOEX regime score |

An oscillator extreme is not a reversal signal. Positive oversold credit needs
at least two confirmations among improving MACD/RSI, non-falling price,
support/level behavior and positive volume. In a BEAR market the oversold credit
is reduced further. A dedicated crash regression prevents mass BUY behavior
from `RSI < 30` alone.

Volume state thresholds are NORMAL `<1.3x`, ELEVATED `1.3–2x`, HIGH `2–3x` and
EXTREME `>=3x`. High volume follows price direction; it is not always positive.
Breaks receive stronger credit/penalty only with relative volume confirmation.
OBV uses a baseline-independent ten-period change normalized by average volume.
An additional price/volume divergence label was deliberately not added: the
available OHLCV-only rule was not robust enough to justify another production
factor before deployment.

## Fundamental boundary

`FundamentalDataProvider` is a source-neutral contract. The currently connected
adapter is a validated JSON import for manually reviewed normalized facts from
MOEX, e-disclosure or official issuer reports. It does not scrape one website,
does not call an aggregator and does not fabricate unavailable ratios.

Supported metrics:

- valuation: P/E, P/B, EV/EBITDA;
- profitability: ROE, ROA, operating margin, net margin;
- debt: Debt/EBITDA, Net Debt/EBITDA;
- growth: revenue YoY, earnings YoY, EPS growth;
- cash flow: FCF, FCF growth, FCF yield;
- dividends: yield, consistency and payout ratio.

Every version stores `report_period`, `publication_date`, `available_from`,
`source`, `source_url` and normalized metrics. Queries apply
`available_from <= decision_at` before selecting the latest known report. This
is the fundamental look-ahead barrier. Component scores use sector peers known
at the same timestamp; fewer than two comparable values cannot create a
percentile score. Raw scale-dependent FCF and payout ratio stay diagnostic and
do not create an unsafe direct company ranking.

Actual research coverage for the 20-stock dataset is currently **0/20**. D and
E fundamental ablations are therefore not evaluable, and the fundamental leg
of F cannot be claimed. This is intentional: zero coverage is more honest than
synthetic history or today's ratios replayed into the past.

Official capability references used for this boundary:

- [MOEX ISS reference](https://www.moex.com/a2920) — securities, trades,
  candles and market history;
- [e-disclosure issuer files](https://www.e-disclosure.ru/portal/files.aspx?id=3043&type=5)
  — report documents and publication placement dates, not a stable normalized
  ratios API;
- [Bank of Russia XML interfaces](https://cbr.ru/development/SXML/) and
  [daily web service](https://www.cbr.ru/development/DWS/) — future official
  currency/key-rate context;
- [Bank of Russia XBRL project](https://www.cbr.ru/projects_xbrl/) — official
  reporting taxonomy/infrastructure.

## Configured horizon weights

| Horizon | Factor mix T/F/N | trend | momentum | extreme | volume | levels | volatility | RS | regime |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| INTRADAY_1D | 90/8/2 | 24 | 16 | 8 | 14 | 12 | 6 | 8 | 12 |
| SWING_5D | 75/20/5 | 23 | 12 | 8 | 13 | 10 | 6 | 13 | 15 |
| POSITION_1M | 60/30/10 | 22 | 8 | 5 | 8 | 8 | 7 | 20 | 22 |

Optional factor weights are renormalized over actually available providers.
Therefore missing fundamentals do not act as a zero score and do not dilute the
technical model.

## Ablation and promotion

Results are written to `reports/backtests/ablation_oos.json`. A is the unchanged
legacy baseline; B isolates market regime, C isolates improved volume, and F is
the full set of currently available market/technical components. D/E are marked
not evaluable at zero fundamental coverage. No TEST result participates in
parameter selection.

### Position OOS and walk-forward

| Variant | OOS activated | OOS PF | OOS expectancy | OOS net P&L | OOS max DD | WF PF | WF expectancy | WF max DD |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| A baseline_v1 | 598 | 1.137 | +0.063R | +19,986 ₽ | 2.44% | 1.219 | +0.102R | 2.31% |
| B market regime | 772 | 1.145 | +0.073R | +27,605 ₽ | 3.66% | 1.191 | +0.091R | 3.45% |
| C volume | 733 | 1.250 | +0.116R | +43,956 ₽ | 2.08% | 1.220 | +0.101R | 1.96% |
| F full available | 682 | 1.213 | +0.104R | +36,162 ₽ | 3.47% | 1.250 | +0.121R | 3.26% |

Volume produced the clearest isolated OOS improvement. Regime alone was not
stable: its WF PF/expectancy fell below A and drawdown increased. The declared
full candidate improved PF and expectancy on OOS and WF, but increased maximum
drawdown by about 42% OOS and had no fundamental leg. It is retained as a
research candidate, not promoted from TEST.

### Swing OOS and walk-forward

| Variant | OOS activated | OOS PF | OOS expectancy | OOS net P&L | OOS max DD | WF PF | WF expectancy | WF max DD |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| A baseline_v1 | 1,567 | 0.886 | -0.077R | -52,038 ₽ | 8.21% | 1.013 | +0.003R | 8.32% |
| B market regime | 2,497 | 0.880 | -0.075R | -85,219 ₽ | 13.44% | 0.954 | -0.027R | 16.96% |
| C volume | 2,558 | 0.887 | -0.071R | -81,459 ₽ | 13.24% | 0.972 | -0.018R | 15.26% |
| F full available | 2,113 | 0.926 | -0.044R | -44,941 ₽ | 10.33% | 0.982 | -0.009R | 12.62% |

F reduced the OOS loss but did not create an edge: PF and expectancy remained
negative, WF deteriorated versus A, and drawdown increased. Swing contextual is
rejected for promotion.

### Final promotion policy

- `INTRADAY_1D`: `RESEARCH`, `legacy`; not re-optimized.
- `SWING_5D`: `RESEARCH`, `legacy`; contextual rejected.
- `POSITION_1M`: `PAPER`, `legacy`; context is collected in immutable snapshots,
  but the higher-risk full candidate is not selected from TEST.

The production `.env.example` therefore stays on horizon-specific `legacy`
selectors. The complete machine-readable results, including TP/SL counts and
all metrics, are committed in `reports/backtests/ablation_oos.json`.
