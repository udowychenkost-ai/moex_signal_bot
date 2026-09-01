from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from app.analysis import add_indicators, candle_frame, detect_support_resistance
from app.observation import aware_utc
from app.v24_domain import (
    BreakoutState,
    DataAvailability,
    StructureState,
    VolumeProfileStatus,
)

MOEX_TZ = ZoneInfo("Europe/Moscow")


@dataclass(frozen=True, slots=True)
class IntradayTechnicalConfig:
    version: str = "intraday_v2_4_technical_1"
    opening_range_minutes: int = 30
    relative_lookback_bars: int = 20
    compression_lookback_bars: int = 20
    breakout_lookback_bars: int = 20
    breakout_tolerance_pct: float = 0.10
    pivot_window: int = 2
    minimum_rvol_days: int = 3
    volume_confirmation_rvol: float = 1.30

    def __post_init__(self) -> None:
        if (
            min(
                self.opening_range_minutes,
                self.relative_lookback_bars,
                self.compression_lookback_bars,
                self.breakout_lookback_bars,
                self.pivot_window,
                self.minimum_rvol_days,
            )
            <= 0
        ):
            raise ValueError("Intraday technical windows must be positive")
        if self.breakout_tolerance_pct < 0 or self.volume_confirmation_rvol <= 0:
            raise ValueError("Technical thresholds must be non-negative")


@dataclass(frozen=True, slots=True)
class AnchoredVWAPValue:
    anchor_name: str
    anchor_time: datetime
    value: float | None
    status: DataAvailability


@dataclass(frozen=True, slots=True)
class IntradayTechnicalSnapshot:
    timeframe: str
    as_of: datetime
    current_price: float
    previous_close: float
    ema20: float
    ema50: float
    atr: float | None
    session_vwap: float | None
    anchored_vwaps: tuple[AnchoredVWAPValue, ...]
    previous_day_high: float | None
    previous_day_low: float | None
    previous_day_close: float | None
    opening_range_high: float | None
    opening_range_low: float | None
    gap_pct: float | None
    instrument_return_pct: float | None
    benchmark_return_pct: float | None
    relative_strength_pct: float | None
    compression_ratio: float | None
    expansion_ratio: float | None
    breakout_state: BreakoutState
    breakout_direction: str | None
    breakout_level: float | None
    structure_state: StructureState
    market_structure_break: str | None
    rvol: float | None
    volume_confirmed: bool | None
    support_levels: tuple[float, ...]
    resistance_levels: tuple[float, ...]
    volume_profile_status: VolumeProfileStatus
    volume_profile_reason: str


def _moex_frame(candles: list[object]) -> pd.DataFrame:
    if not candles:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    frame = candle_frame(candles).sort_index()
    index = pd.DatetimeIndex(frame.index)
    if index.tz is None:
        index = index.tz_localize(UTC)
    frame.index = index.tz_convert(MOEX_TZ)
    return frame[~frame.index.duplicated(keep="last")]


def _vwap(frame: pd.DataFrame) -> pd.Series:
    typical = (frame["high"] + frame["low"] + frame["close"]) / 3
    volume = frame["volume"].where(frame["volume"] > 0)
    dates = pd.Series(frame.index.date, index=frame.index)
    cumulative_value = (typical * volume).groupby(dates).cumsum()
    cumulative_volume = volume.groupby(dates).cumsum()
    return cumulative_value / cumulative_volume.replace(0, np.nan)


def anchored_vwap(
    frame: pd.DataFrame,
    anchors: Mapping[str, datetime],
) -> tuple[AnchoredVWAPValue, ...]:
    values: list[AnchoredVWAPValue] = []
    for name, raw_anchor in anchors.items():
        anchor = aware_utc(raw_anchor).astimezone(MOEX_TZ)
        selected = frame.loc[frame.index >= anchor]
        if selected.empty or float(selected["volume"].sum()) <= 0:
            values.append(
                AnchoredVWAPValue(
                    anchor_name=name,
                    anchor_time=aware_utc(raw_anchor),
                    value=None,
                    status=DataAvailability.DATA_NOT_AVAILABLE,
                )
            )
            continue
        typical = (selected["high"] + selected["low"] + selected["close"]) / 3
        result = float((typical * selected["volume"]).sum() / selected["volume"].sum())
        values.append(
            AnchoredVWAPValue(
                anchor_name=name,
                anchor_time=aware_utc(raw_anchor),
                value=result,
                status=DataAvailability.AVAILABLE,
            )
        )
    return tuple(values)


def _previous_day_levels(frame: pd.DataFrame) -> tuple[float | None, float | None, float | None]:
    if frame.empty:
        return None, None, None
    groups = list(frame.groupby(frame.index.date, sort=True))
    if len(groups) < 2:
        return None, None, None
    previous = groups[-2][1]
    return (
        float(previous["high"].max()),
        float(previous["low"].min()),
        float(previous["close"].iloc[-1]),
    )


def _opening_range(
    frame: pd.DataFrame,
    minutes: int,
) -> tuple[float | None, float | None]:
    if frame.empty:
        return None, None
    current_date = frame.index[-1].date()
    start = datetime.combine(current_date, time(10, 0), tzinfo=MOEX_TZ)
    end = start + timedelta(minutes=minutes)
    selected = frame.loc[(frame.index >= start) & (frame.index < end)]
    if selected.empty:
        return None, None
    return float(selected["high"].max()), float(selected["low"].min())


def _relative_strength(
    instrument: pd.DataFrame,
    benchmark: pd.DataFrame,
    lookback: int,
) -> tuple[float | None, float | None, float | None]:
    if len(instrument) < 2 or len(benchmark) < 2:
        return None, None, None
    instrument_close = instrument["close"].iloc[-lookback - 1 :]
    benchmark_close = benchmark["close"].iloc[-lookback - 1 :]
    if len(instrument_close) < 2 or len(benchmark_close) < 2:
        return None, None, None
    instrument_return = (
        float(instrument_close.iloc[-1]) / float(instrument_close.iloc[0]) - 1
    ) * 100
    benchmark_return = (float(benchmark_close.iloc[-1]) / float(benchmark_close.iloc[0]) - 1) * 100
    return instrument_return, benchmark_return, instrument_return - benchmark_return


def _compression(frame: pd.DataFrame, lookback: int) -> tuple[float | None, float | None]:
    if len(frame) < lookback + 1:
        return None, None
    previous_close = frame["close"].shift(1)
    true_range = pd.concat(
        (
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ),
        axis=1,
    ).max(axis=1)
    range_baseline = float(true_range.iloc[-lookback - 1 : -1].median())
    rolling_mid = frame["close"].rolling(20).mean()
    rolling_std = frame["close"].rolling(20).std()
    band_width = (rolling_std * 4) / rolling_mid.replace(0, np.nan)
    width_baseline = float(band_width.iloc[-lookback - 1 : -1].median())
    current_width = float(band_width.iloc[-1])
    if (
        not math.isfinite(range_baseline)
        or range_baseline <= 0
        or not math.isfinite(width_baseline)
        or width_baseline <= 0
        or not math.isfinite(current_width)
    ):
        return None, None
    compression_ratio = current_width / width_baseline
    expansion_ratio = float(true_range.iloc[-1]) / range_baseline
    return compression_ratio, expansion_ratio


def _breakout_state(
    frame: pd.DataFrame,
    *,
    lookback: int,
    tolerance_pct: float,
) -> tuple[BreakoutState, str | None, float | None]:
    if len(frame) < lookback + 2:
        return BreakoutState.NONE, None, None
    tolerance = tolerance_pct / 100
    history = frame.iloc[-lookback - 2 : -2]
    previous = frame.iloc[-2]
    current = frame.iloc[-1]
    resistance = float(history["high"].max())
    support = float(history["low"].min())
    if (
        float(current["high"]) > resistance * (1 + tolerance)
        and float(current["close"]) <= resistance
    ):
        return BreakoutState.FALSE_BREAKOUT, "UP", resistance
    if float(current["low"]) < support * (1 - tolerance) and float(current["close"]) >= support:
        return BreakoutState.FALSE_BREAKOUT, "DOWN", support
    if (
        float(previous["close"]) > resistance
        and float(current["low"]) <= resistance * (1 + tolerance)
        and float(current["close"]) > resistance
    ):
        return BreakoutState.RETEST, "UP", resistance
    if (
        float(previous["close"]) < support
        and float(current["high"]) >= support * (1 - tolerance)
        and float(current["close"]) < support
    ):
        return BreakoutState.RETEST, "DOWN", support
    if float(previous["close"]) <= resistance and float(current["close"]) > resistance * (
        1 + tolerance
    ):
        return BreakoutState.BREAKOUT, "UP", resistance
    if float(previous["close"]) >= support and float(current["close"]) < support * (1 - tolerance):
        return BreakoutState.BREAKOUT, "DOWN", support
    return BreakoutState.NONE, None, None


def _structure(
    frame: pd.DataFrame,
    window: int,
) -> tuple[StructureState, str | None]:
    if len(frame) < window * 4 + 3:
        return StructureState.UNKNOWN, None
    span = window * 2 + 1
    highs = (
        frame["high"]
        .where(frame["high"] == frame["high"].rolling(span, center=True).max())
        .dropna()
    )
    lows = (
        frame["low"].where(frame["low"] == frame["low"].rolling(span, center=True).min()).dropna()
    )
    if len(highs) < 2 or len(lows) < 2:
        return StructureState.UNKNOWN, None
    higher_high = float(highs.iloc[-1]) > float(highs.iloc[-2])
    higher_low = float(lows.iloc[-1]) > float(lows.iloc[-2])
    lower_high = float(highs.iloc[-1]) < float(highs.iloc[-2])
    lower_low = float(lows.iloc[-1]) < float(lows.iloc[-2])
    state = (
        StructureState.HH_HL
        if higher_high and higher_low
        else (StructureState.LH_LL if lower_high and lower_low else StructureState.MIXED)
    )
    current = float(frame["close"].iloc[-1])
    structure_break = None
    if current > float(highs.iloc[-1]):
        structure_break = "BREAK_UP"
    elif current < float(lows.iloc[-1]):
        structure_break = "BREAK_DOWN"
    return state, structure_break


def _rvol_by_slot(frame: pd.DataFrame, minimum_days: int) -> float | None:
    if frame.empty:
        return None
    current_index = frame.index[-1]
    current_volume = float(frame["volume"].iloc[-1])
    historical = frame.loc[
        (frame.index.date != current_index.date())
        & (frame.index.hour == current_index.hour)
        & (frame.index.minute == current_index.minute),
        "volume",
    ]
    if len(historical) < minimum_days:
        return None
    average = float(historical.tail(20).mean())
    return current_volume / average if average > 0 else None


def analyze_intraday_technical(
    candles: list[object],
    *,
    timeframe: str,
    benchmark_candles: list[object] | None = None,
    anchors: Mapping[str, datetime] | None = None,
    config: IntradayTechnicalConfig | None = None,
) -> IntradayTechnicalSnapshot:
    policy = config or IntradayTechnicalConfig()
    frame = _moex_frame(candles)
    if len(frame) < max(55, policy.breakout_lookback_bars + 2):
        raise ValueError(f"{timeframe}: insufficient candles for v2.4 technical analysis")
    indicators = add_indicators(frame)
    current = float(frame["close"].iloc[-1])
    previous_close = float(frame["close"].iloc[-2])
    vwap = _vwap(frame)
    latest_vwap = float(vwap.iloc[-1]) if math.isfinite(float(vwap.iloc[-1])) else None
    previous_high, previous_low, previous_day_close = _previous_day_levels(frame)
    opening_high, opening_low = _opening_range(frame, policy.opening_range_minutes)
    current_day = frame.loc[frame.index.date == frame.index[-1].date()]
    gap_pct = (
        (float(current_day["open"].iloc[0]) / previous_day_close - 1) * 100
        if previous_day_close is not None and not current_day.empty
        else None
    )
    benchmark = _moex_frame(benchmark_candles or [])
    instrument_return, benchmark_return, relative = _relative_strength(
        frame, benchmark, policy.relative_lookback_bars
    )
    compression, expansion = _compression(frame, policy.compression_lookback_bars)
    breakout, breakout_direction, breakout_level = _breakout_state(
        frame,
        lookback=policy.breakout_lookback_bars,
        tolerance_pct=policy.breakout_tolerance_pct,
    )
    structure, structure_break = _structure(frame, policy.pivot_window)
    rvol = _rvol_by_slot(frame, policy.minimum_rvol_days)
    volume_confirmed = rvol >= policy.volume_confirmation_rvol if rvol is not None else None
    levels = detect_support_resistance(frame, window=policy.pivot_window)
    latest_end = getattr(candles[-1], "end", candles[-1].begin)
    atr_value = float(indicators["atr_14"].iloc[-1])
    return IntradayTechnicalSnapshot(
        timeframe=timeframe,
        as_of=aware_utc(latest_end),
        current_price=current,
        previous_close=previous_close,
        ema20=float(indicators["ema_20"].iloc[-1]),
        ema50=float(indicators["ema_50"].iloc[-1]),
        atr=atr_value if math.isfinite(atr_value) else None,
        session_vwap=latest_vwap,
        anchored_vwaps=anchored_vwap(frame, anchors or {}),
        previous_day_high=previous_high,
        previous_day_low=previous_low,
        previous_day_close=previous_day_close,
        opening_range_high=opening_high,
        opening_range_low=opening_low,
        gap_pct=gap_pct,
        instrument_return_pct=instrument_return,
        benchmark_return_pct=benchmark_return,
        relative_strength_pct=relative,
        compression_ratio=compression,
        expansion_ratio=expansion,
        breakout_state=breakout,
        breakout_direction=breakout_direction,
        breakout_level=breakout_level,
        structure_state=structure,
        market_structure_break=structure_break,
        rvol=rvol,
        volume_confirmed=volume_confirmed,
        support_levels=tuple(levels["support"]),
        resistance_levels=tuple(levels["resistance"]),
        volume_profile_status=VolumeProfileStatus.DATA_NOT_AVAILABLE,
        volume_profile_reason=(
            "Aggregated OHLCV candles do not provide trade-at-price volume distribution"
        ),
    )
