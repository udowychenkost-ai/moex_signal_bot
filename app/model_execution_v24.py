from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from app.observation import aware_utc
from app.v24_domain import FillStatus, JournalDirection


class ModelOrderType(StrEnum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"


@dataclass(frozen=True, slots=True)
class ModelExecutionRequest:
    direction: JournalDirection
    order_type: ModelOrderType
    entry: float
    stop: float
    tp1: float
    candles: tuple[object, ...]
    lower_timeframe_candles: tuple[object, ...] = ()
    reliable_fill_data: bool = False
    market_price: float | None = None
    market_slippage_bps: float | None = None


@dataclass(frozen=True, slots=True)
class ModelExecutionResult:
    fill_status: FillStatus
    entry_time: datetime | None
    entry_fill: float | None
    exit_time: datetime | None
    exit_fill: float | None
    exit_reason: str | None
    ambiguous_execution: bool
    calibration_eligible: bool
    gap_slippage: float | None
    notes: tuple[str, ...]


def _entry_touched(candle: object, entry: float) -> bool:
    return float(candle.low) <= entry <= float(candle.high)


def _stop_hit(direction: JournalDirection, candle: object, stop: float) -> bool:
    return (
        float(candle.low) <= stop
        if direction is JournalDirection.LONG
        else float(candle.high) >= stop
    )


def _target_hit(direction: JournalDirection, candle: object, target: float) -> bool:
    return (
        float(candle.high) >= target
        if direction is JournalDirection.LONG
        else float(candle.low) <= target
    )


def _gap_through_stop(direction: JournalDirection, candle: object, stop: float) -> bool:
    return (
        float(candle.open) < stop
        if direction is JournalDirection.LONG
        else float(candle.open) > stop
    )


def _resolve_ambiguous_parent(
    request: ModelExecutionRequest,
    parent: object,
    *,
    entry_already_before_parent: bool,
) -> tuple[str | None, datetime | None, float | None, bool]:
    parent_begin = aware_utc(parent.begin)
    parent_end = aware_utc(parent.end)
    lower = sorted(
        (
            item
            for item in request.lower_timeframe_candles
            if parent_begin <= aware_utc(item.begin) and aware_utc(item.end) <= parent_end
        ),
        key=lambda item: aware_utc(item.begin),
    )
    entered = request.order_type is ModelOrderType.MARKET or entry_already_before_parent
    for candle in lower:
        if not entered:
            if not _entry_touched(candle, request.entry):
                continue
            entered = True
        stop_hit = _stop_hit(request.direction, candle, request.stop)
        target_hit = _target_hit(request.direction, candle, request.tp1)
        if stop_hit and target_hit:
            return "AMBIGUOUS_CONSERVATIVE_STOP", aware_utc(candle.end), request.stop, True
        if stop_hit:
            return "SL", aware_utc(candle.end), request.stop, False
        if target_hit:
            return "TP1", aware_utc(candle.end), request.tp1, False
    return "AMBIGUOUS_CONSERVATIVE_STOP", aware_utc(parent.end), request.stop, True


def simulate_model_execution(request: ModelExecutionRequest) -> ModelExecutionResult:
    if min(request.entry, request.stop, request.tp1) <= 0:
        raise ValueError("Entry, stop and target must be positive")
    if (
        request.direction is JournalDirection.LONG
        and not request.stop < request.entry < request.tp1
    ):
        raise ValueError("LONG execution levels must satisfy stop < entry < target")
    if (
        request.direction is JournalDirection.SHORT
        and not request.tp1 < request.entry < request.stop
    ):
        raise ValueError("SHORT execution levels must satisfy target < entry < stop")
    candles = sorted(request.candles, key=lambda item: aware_utc(item.begin))
    if not candles:
        return ModelExecutionResult(
            FillStatus.NOT_FILLED, None, None, None, None, None, False, False, None, ("NO_CANDLES",)
        )

    entry_time: datetime | None = None
    entry_fill: float | None = None
    fill_index = 0
    notes: list[str] = []
    if request.order_type is ModelOrderType.MARKET:
        if request.market_price is None or request.market_slippage_bps is None:
            return ModelExecutionResult(
                FillStatus.UNCERTAIN,
                None,
                None,
                None,
                None,
                None,
                False,
                False,
                None,
                ("MARKET_PRICE_OR_SLIPPAGE_NOT_CONFIGURED",),
            )
        slippage = request.market_slippage_bps / 10_000
        entry_fill = request.market_price * (
            1 + slippage if request.direction is JournalDirection.LONG else 1 - slippage
        )
        entry_time = aware_utc(candles[0].begin)
        fill_status = FillStatus.FILLED
    else:
        touched = next(
            (
                (index, candle)
                for index, candle in enumerate(candles)
                if _entry_touched(candle, request.entry)
            ),
            None,
        )
        if touched is None:
            return ModelExecutionResult(
                FillStatus.NOT_FILLED,
                None,
                None,
                None,
                None,
                None,
                False,
                False,
                None,
                ("LIMIT_NOT_TOUCHED",),
            )
        fill_index, fill_candle = touched
        entry_time = aware_utc(fill_candle.end)
        entry_fill = request.entry
        fill_status = FillStatus.FILLED if request.reliable_fill_data else FillStatus.UNCERTAIN
        if fill_status is FillStatus.UNCERTAIN:
            notes.append("LIMIT_TOUCH_WITHOUT_RELIABLE_VOLUME_OR_ORDERBOOK_FILL_DATA")

    ambiguous = False
    exit_time: datetime | None = None
    exit_fill: float | None = None
    exit_reason: str | None = None
    gap_slippage: float | None = None
    for candle_index, candle in enumerate(candles[fill_index:], start=fill_index):
        if _gap_through_stop(request.direction, candle, request.stop):
            exit_fill = float(candle.open)
            exit_time = aware_utc(candle.begin)
            exit_reason = "GAP_THROUGH_STOP"
            gap_slippage = abs(exit_fill - request.stop)
            break
        stop_hit = _stop_hit(request.direction, candle, request.stop)
        target_hit = _target_hit(request.direction, candle, request.tp1)
        if stop_hit and target_hit:
            exit_reason, exit_time, exit_fill, ambiguous = _resolve_ambiguous_parent(
                request,
                candle,
                entry_already_before_parent=candle_index > fill_index,
            )
            break
        if stop_hit:
            exit_time = aware_utc(candle.end)
            exit_fill = request.stop
            exit_reason = "SL"
            break
        if target_hit:
            exit_time = aware_utc(candle.end)
            exit_fill = request.tp1
            exit_reason = "TP1"
            break
    calibration_eligible = fill_status is FillStatus.FILLED and not ambiguous
    return ModelExecutionResult(
        fill_status=fill_status,
        entry_time=entry_time,
        entry_fill=entry_fill,
        exit_time=exit_time,
        exit_fill=exit_fill,
        exit_reason=exit_reason,
        ambiguous_execution=ambiguous,
        calibration_eligible=calibration_eligible,
        gap_slippage=gap_slippage,
        notes=tuple(notes),
    )
