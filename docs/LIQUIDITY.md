# V2.1.5 liquidity and comfortable execution size

This module is an execution-liquidity UX layer, not a trading strategy or risk
manager. It never changes QualityGate, scoring, AI verdicts, TP/SL, quantity,
paper trades, backtests or the `TradingIdea` lifecycle.

## Reused data

- `instruments.daily_turnover`: current MOEX `VALTODAY`, accepted only while the
  instrument row is fresh for the current session;
- `instruments.last_price`, `lot_size`, `short_name`;
- completed `candles` rows with `timeframe=1d`;
- the single latest `order_book_levels.snapshot_at` and its B/S levels;
- `TradingIdea.market_volatility`: the existing LOW/NORMAL/HIGH/EXTREME context.

No liquidity table or historical-row rewrite is required.

## ADV20 and book depth

ADV20 is the mean of up to 20 latest completed daily RUB turnovers. Direct
`Candle.value` is preferred. Only when it is absent/non-positive does the module
reconstruct a value as `close × volume`. The current forming daily candle is
excluded.

MOEX aggregate order-book `QUANTITY` is [expressed in lots](https://ftp.moex.com/pub/ClientsAPI/ASTS/Bridge_Interfaces/Equities/Equities55_Broker_English.htm).
RUB depth is therefore `price × quantity_lots × instrument.lot_size`. For BUY, asks are entry depth and
bids are exit depth; for SELL the sides are reversed. The usable depth is always
`min(entry_depth, exit_depth)`. Bands ±0.25%, ±0.50% and ±1.00% are calculated;
±0.50% is the configurable size cap band.

The latest book is usable only while the market is considered open and its age
does not exceed `LIQUIDITY_ORDERBOOK_FRESHNESS_SECONDS` (300 seconds by default).
Stale or closed-market depth and spread are displayed only as historical context
and are not used as current execution capacity.

## Comfortable liquidity size

```text
turnover_cap = ADV20 × 0.0025
depth_cap = relevant_depth_0.50% × 0.10
raw = min(turnover_cap, depth_cap)       # fresh book
raw = turnover_cap                      # no usable book
comfortable_size = raw × volatility_modifier × spread_modifier
```

Volatility modifiers reuse the existing class: LOW 1.00, NORMAL 0.90, HIGH 0.70,
EXTREME 0.45 (UNKNOWN 0.75). Spread modifiers are 1.00 at ≤0.10%, 0.85 at
≤0.25%, 0.60 at ≤0.50%, and 0.35 above 0.50% (UNKNOWN 0.85). The displayed size
is rounded down to a 1/2.5/5/7.5 × 10^n step.

## Rating

The rating is a normalized known-data score: ADV20 30%, relative turnover 15%,
current spread 20%, current relevant book depth 25%, and book freshness 10%.
At least 50% of configured evidence weight must be known; otherwise the result is
UNKNOWN. HIGH requires score ≥0.75, MEDIUM ≥0.45, otherwise LOW.

Metric tiers start at:

- ADV20: MEDIUM 100 million RUB, HIGH 1 billion RUB;
- relative turnover: MEDIUM 0.35×, HIGH 0.75×;
- relevant depth: MEDIUM 2 million RUB, HIGH 10 million RUB;
- spread: best tier ≤0.10%, middle tier ≤0.50%, otherwise weak.

These are centralized configurable starting heuristics, not claims of guaranteed
execution quality. `LiquidityAssessment.ai_snapshot()` exposes optional future
context fields, but V2.1.5 does not send them to Gemini.
