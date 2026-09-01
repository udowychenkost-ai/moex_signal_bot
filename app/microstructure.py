from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime

from app.v24_domain import JournalDirection, MicrostructureStatus, TradingSessionState


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class CorporateActionFlags:
    dividend_ex_date: bool | None = None
    split_or_reverse_split: bool | None = None
    additional_issue: bool | None = None
    buyback_or_tender: bool | None = None
    conversion: bool | None = None
    reorganization: bool | None = None
    delisting: bool | None = None


@dataclass(frozen=True, slots=True)
class MicrostructureInput:
    direction: JournalDirection
    session_state: TradingSessionState
    current_price: float | None
    tick_size: float | None
    lot_size: int | None
    best_bid: float | None
    best_ask: float | None
    orderbook_timestamp: datetime | None
    orderbook_quality: str | None
    lower_price_band: float | None = None
    upper_price_band: float | None = None
    corporate_actions: CorporateActionFlags | None = None
    short_available: bool | None = None
    borrow_carry_pct: float | None = None


@dataclass(frozen=True, slots=True)
class MicrostructureIssue:
    code: str
    details: str
    blocking: bool


@dataclass(frozen=True, slots=True)
class MicrostructureAssessment:
    status: MicrostructureStatus
    spread_pct: float | None
    orderbook_age_seconds: float | None
    issues: tuple[MicrostructureIssue, ...]
    unavailable_fields: tuple[str, ...]
    checked_at: datetime

    @property
    def allows_enter_now(self) -> bool:
        return self.status in {MicrostructureStatus.PASS, MicrostructureStatus.WARN}


class MicrostructureGuard:
    def __init__(
        self,
        *,
        max_spread_pct: float | None = None,
        max_orderbook_age_seconds: float | None = None,
    ) -> None:
        if max_spread_pct is not None and max_spread_pct < 0:
            raise ValueError("max_spread_pct cannot be negative")
        if max_orderbook_age_seconds is not None and max_orderbook_age_seconds < 0:
            raise ValueError("max_orderbook_age_seconds cannot be negative")
        self.max_spread_pct = max_spread_pct
        self.max_orderbook_age_seconds = max_orderbook_age_seconds

    def evaluate(
        self,
        inputs: MicrostructureInput,
        *,
        now: datetime | None = None,
    ) -> MicrostructureAssessment:
        checked_at = _aware_utc(now or datetime.now(UTC))
        issues: list[MicrostructureIssue] = []
        unavailable: set[str] = set()

        if inputs.session_state in {
            TradingSessionState.AUCTION,
            TradingSessionState.HALTED,
            TradingSessionState.SUSPENDED,
            TradingSessionState.CLOSED,
        }:
            issues.append(
                MicrostructureIssue(
                    "SESSION_NOT_CONTINUOUS", inputs.session_state.value, blocking=True
                )
            )
        elif inputs.session_state is TradingSessionState.UNKNOWN:
            unavailable.add("trading_session")
        elif inputs.session_state is TradingSessionState.RESUMPTION:
            issues.append(
                MicrostructureIssue(
                    "SESSION_RESUMPTION", "Execution conditions may be unstable", blocking=False
                )
            )

        for name, value in (("tick_size", inputs.tick_size), ("lot_size", inputs.lot_size)):
            if value is None:
                unavailable.add(name)
            elif value <= 0:
                issues.append(MicrostructureIssue("INVALID_INSTRUMENT_METADATA", name, True))

        spread_pct: float | None = None
        if inputs.best_bid is None or inputs.best_ask is None:
            unavailable.add("best_bid_ask")
        elif not all(
            math.isfinite(value) and value > 0 for value in (inputs.best_bid, inputs.best_ask)
        ):
            issues.append(
                MicrostructureIssue("INVALID_BEST_QUOTE", "Bid/ask must be positive", True)
            )
        elif inputs.best_ask < inputs.best_bid:
            issues.append(MicrostructureIssue("CROSSED_BOOK", "best_ask < best_bid", True))
        else:
            mid = (inputs.best_bid + inputs.best_ask) / 2
            spread_pct = (inputs.best_ask - inputs.best_bid) / mid
            if self.max_spread_pct is None:
                unavailable.add("abnormal_spread_threshold")
            elif spread_pct > self.max_spread_pct:
                issues.append(
                    MicrostructureIssue(
                        "ABNORMAL_SPREAD",
                        f"spread={spread_pct:.6f} limit={self.max_spread_pct:.6f}",
                        True,
                    )
                )

        orderbook_age: float | None = None
        if inputs.orderbook_timestamp is None:
            unavailable.add("orderbook_freshness")
        else:
            orderbook_age = (checked_at - _aware_utc(inputs.orderbook_timestamp)).total_seconds()
            if orderbook_age < 0:
                issues.append(
                    MicrostructureIssue(
                        "FUTURE_ORDERBOOK_TIMESTAMP", f"age={orderbook_age:.3f}s", True
                    )
                )
            elif self.max_orderbook_age_seconds is None:
                unavailable.add("orderbook_freshness_threshold")
            elif orderbook_age > self.max_orderbook_age_seconds:
                issues.append(
                    MicrostructureIssue(
                        "STALE_ORDERBOOK",
                        (f"age={orderbook_age:.3f}s limit={self.max_orderbook_age_seconds:.3f}s"),
                        True,
                    )
                )
        if inputs.orderbook_quality is None:
            unavailable.add("orderbook_quality")
        elif inputs.orderbook_quality.upper() == "FAIL":
            issues.append(MicrostructureIssue("ORDERBOOK_QUALITY_FAIL", "FAIL", True))
        elif inputs.orderbook_quality.upper() not in {"PASS", "HIGH", "GOOD"}:
            issues.append(
                MicrostructureIssue(
                    "ORDERBOOK_QUALITY_WARN", inputs.orderbook_quality, blocking=False
                )
            )

        if inputs.current_price is None:
            unavailable.add("current_price")
        elif not math.isfinite(inputs.current_price) or inputs.current_price <= 0:
            issues.append(MicrostructureIssue("INVALID_CURRENT_PRICE", "non-positive", True))
        else:
            if inputs.lower_price_band is None or inputs.upper_price_band is None:
                unavailable.add("price_bands")
            elif not inputs.lower_price_band <= inputs.current_price <= inputs.upper_price_band:
                issues.append(
                    MicrostructureIssue(
                        "OUTSIDE_PRICE_BANDS",
                        (
                            f"price={inputs.current_price} band="
                            f"[{inputs.lower_price_band},{inputs.upper_price_band}]"
                        ),
                        True,
                    )
                )

        actions = inputs.corporate_actions
        if actions is None:
            unavailable.add("corporate_actions")
        else:
            action_values = {
                "dividend_ex_date": actions.dividend_ex_date,
                "split_or_reverse_split": actions.split_or_reverse_split,
                "additional_issue": actions.additional_issue,
                "buyback_or_tender": actions.buyback_or_tender,
                "conversion": actions.conversion,
                "reorganization": actions.reorganization,
                "delisting": actions.delisting,
            }
            unavailable.update(name for name, value in action_values.items() if value is None)
            for name in (
                "dividend_ex_date",
                "split_or_reverse_split",
                "conversion",
                "reorganization",
                "delisting",
            ):
                if action_values[name] is True:
                    issues.append(MicrostructureIssue("CORPORATE_ACTION_RISK", name, True))
            for name in ("additional_issue", "buyback_or_tender"):
                if action_values[name] is True:
                    issues.append(MicrostructureIssue("CORPORATE_ACTION_WARN", name, False))

        if inputs.direction is JournalDirection.SHORT:
            if inputs.short_available is False:
                issues.append(
                    MicrostructureIssue("SHORT_UNAVAILABLE", "Confirmed unavailable", True)
                )
            elif inputs.short_available is None:
                unavailable.add("short_availability")
            if inputs.borrow_carry_pct is None:
                unavailable.add("borrow_carry")

        if any(issue.blocking for issue in issues):
            status = MicrostructureStatus.FAIL
        elif unavailable:
            status = MicrostructureStatus.DATA_NOT_AVAILABLE
        elif issues:
            status = MicrostructureStatus.WARN
        else:
            status = MicrostructureStatus.PASS
        return MicrostructureAssessment(
            status=status,
            spread_pct=spread_pct,
            orderbook_age_seconds=orderbook_age,
            issues=tuple(issues),
            unavailable_fields=tuple(sorted(unavailable)),
            checked_at=checked_at,
        )
