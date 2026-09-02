# V2.4 data SLA

V2.4 separates general candle freshness from action-specific Data SLA. The SLA
is intentionally `NOT_CONFIGURED` unless every required latency and source
class is explicitly supplied.

Required environment settings:

```env
DATA_SLA_VERSION=
MAX_LATENCY_ENTER_NOW=
MAX_LATENCY_POSITION_MANAGEMENT=
MAX_LATENCY_INTRADAY_ANALYSIS=
REQUIRED_QUOTE_SOURCE_CLASS=
REQUIRED_VOLUME_SOURCE_CLASS=
REQUIRED_ORDERBOOK_SOURCE_CLASS=
MICROSTRUCTURE_MAX_SPREAD_PCT=
MICROSTRUCTURE_ORDERBOOK_MAX_AGE_SECONDS=
```

No numerical examples are presented as recommended limits. They must reflect
the actual provider contract and operator-approved policy.

## Source classes

The supported classes are `OFFICIAL_REALTIME`, `OFFICIAL_PUBLIC`,
`BROKER_REALTIME`, `LICENSED_REALTIME`, `AGGREGATED`, `MANUAL_CONFIRMED` and
`UNKNOWN`. A source must have a timestamp and must satisfy the configured class
and age. `fetched_at` is not a substitute for the market source timestamp.

## Decision policy

| State | Immediate action |
|---|---|
| `CONFIGURED + PASS` | May continue to the next hard gate |
| `CONFIGURED + WARN` | Analysis/watch only; no immediate action |
| `CONFIGURED + FAIL` | Block |
| `NOT_CONFIGURED` | Block |

`ENTER_NOW`, `MOVE_STOP` and `CLOSE_NOW` require the action-specific SLA to
pass. A failed position mark results in `VERIFY_DATA`, not an automatic stop or
exit instruction.

## Current source reality

- MOEX ISS candles and public market data are official but can be delayed.
- Public level-1 may provide bid/offer with `quantity=NULL`; this confirms
  price/spread, not depth.
- Full L2, borrow availability, corporate actions and real-time news are not
  assumed. Their missing fields remain unavailable and prevent gates that need
  them.

