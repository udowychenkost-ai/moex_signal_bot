# V2.4 calibration and statistical admission

Calibration uses comparable MODEL outcomes only from declared `OOS` and
`FORWARD` samples. Development rows, ambiguous execution and incomplete
outcomes cannot silently enter the eligible cohort.

The exact cohort key contains setup, direction, market regime, trend,
volatility, time of day, R:R bucket, liquidity state and event/context state.
Aggregation across a different key is not allowed merely to increase sample
size.

## Admission

An effective-dated admission policy defines minimum OOS/forward/total samples,
maximum Wilson confidence-interval width and degradation thresholds. With no
policy the state is `NOT_CONFIGURED`. With insufficient evidence it is
`PRELIMINARY`; only a passing comparable cohort is `CALIBRATED`.

Numeric probability may be shown only when:

1. the exact cohort is calibrated;
2. OOS and forward minimums pass;
3. the Wilson interval and width gates pass;
4. the recent model is not materially degraded.

Otherwise Telegram prints `NOT RELIABLY CALIBRATED`, even if a raw model or AI
score exists. AI score/confidence is not an empirical win probability.

## Metrics

The statistics service calculates Wilson intervals, Brier score, ECE/reliability
buckets, rolling 20/50/100/all windows, setup/regime views, execution quality,
drawdown and separate MODEL/ACTUAL performance. The degradation detector only
emits evidence/state; it never tunes the strategy automatically.

