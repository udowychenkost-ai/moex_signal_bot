# V2.4 risk budget

The V2.4 risk policy is effective-dated and append-only. There are no financial
defaults that look like user-approved limits.

Required policy fields are:

- working capital;
- max risk per trade;
- max daily loss;
- max portfolio heat;
- max sector heat;
- max correlated-factor heat;
- available-capital percentage.

Until all values are explicitly persisted, the status is `NOT_CONFIGURED`,
`FULL_RISK_PASS=false`, the risk cap is unavailable and recommended position is
provisional/not reliably calculable.

## Position and portfolio calculation

The engine distinguishes gross loss to stop from costs, stop slippage and net
stress loss. It then applies daily, portfolio, sector and deterministic shared
factor caps (`MARKET`, `SECTOR`, `COMMODITY`, `FX`, `RATE`, `EVENT`). It does not
invent a precise correlation-adjusted amount when factor exposure is missing.

Reliable position size is the minimum of every mandatory available cap:
liquidity, per-trade risk, available capital, portfolio and correlation. A
missing cap returns `NOT_RELIABLY_CALCULABLE`; it is never treated as infinity
or zero.

## Administrator workflow

An ID listed in `TELEGRAM_ADMIN_CHAT_IDS` can use `/riskpolicy` or Settings →
Risk Budget. The wizard shows the effective policy, collects every field above,
renders a preview and activates only after explicit confirmation. Activation
inserts a new unique configuration version with creator and effective time;
database triggers preserve older versions. Unauthorized users and an
unconfirmed preview cannot write a policy.

The legacy per-user “risk %” setting is not a V2.4 portfolio-risk policy and is
never substituted for one. Historical `DecisionSnapshotV24` rows retain the
exact risk-policy version used at decision time.
