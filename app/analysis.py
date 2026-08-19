from __future__ import annotations

import math

import numpy as np
import pandas as pd

from app.domain import InsufficientDataError, TechnicalResult


def candle_frame(candles: list[object]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "begin": candle.begin,
                "open": candle.open,
                "high": candle.high,
                "low": candle.low,
                "close": candle.close,
                "volume": candle.volume,
            }
            for candle in candles
        ]
    ).set_index("begin")


def calculate_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    average_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    average_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    relative_strength = average_gain / average_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + relative_strength))
    return rsi.where(average_loss.ne(0), 100).where(average_gain.ne(0), 0)


def calculate_macd(
    close: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal_period: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    fast_ema = close.ewm(span=fast, adjust=False).mean()
    slow_ema = close.ewm(span=slow, adjust=False).mean()
    macd = fast_ema - slow_ema
    signal = macd.ewm(span=signal_period, adjust=False).mean()
    return macd, signal, macd - signal


def calculate_atr(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    previous_close = frame["close"].shift(1)
    true_range = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def _levels(frame: pd.DataFrame, window: int = 5) -> tuple[float | None, float | None]:
    current = float(frame["close"].iloc[-1])
    local_lows = frame["low"].where(
        frame["low"] == frame["low"].rolling(window, center=True).min()
    )
    local_highs = frame["high"].where(
        frame["high"] == frame["high"].rolling(window, center=True).max()
    )
    supports = local_lows.dropna()
    supports = supports[supports < current]
    resistances = local_highs.dropna()
    resistances = resistances[resistances > current]
    support = float(supports.iloc[-1]) if not supports.empty else None
    resistance = float(resistances.iloc[-1]) if not resistances.empty else None
    return support, resistance


def _finite(value: float, name: str) -> float:
    if not math.isfinite(value):
        raise InsufficientDataError(f"Индикатор {name} ещё не рассчитан")
    return value


def analyze_technical(candles: list[object]) -> TechnicalResult:
    if len(candles) < 60:
        raise InsufficientDataError(
            f"Нужно минимум 60 свечей для сигнала, сейчас доступно {len(candles)}"
        )
    frame = candle_frame(candles)
    close = frame["close"].astype(float)
    rsi_series = calculate_rsi(close)
    macd, macd_signal, histogram = calculate_macd(close)
    atr_series = calculate_atr(frame)
    ema20_series = close.ewm(span=20, adjust=False).mean()
    ema50_series = close.ewm(span=50, adjust=False).mean()
    volume_average = frame["volume"].rolling(20).mean()

    rsi = _finite(float(rsi_series.iloc[-1]), "RSI")
    current_macd = _finite(float(macd.iloc[-1]), "MACD")
    current_signal = _finite(float(macd_signal.iloc[-1]), "MACD signal")
    current_histogram = _finite(float(histogram.iloc[-1]), "MACD histogram")
    atr = _finite(float(atr_series.iloc[-1]), "ATR")
    ema20 = _finite(float(ema20_series.iloc[-1]), "EMA20")
    ema50 = _finite(float(ema50_series.iloc[-1]), "EMA50")
    average_volume = float(volume_average.iloc[-1])
    volume_ratio = (
        float(frame["volume"].iloc[-1]) / average_volume
        if math.isfinite(average_volume) and average_volume > 0
        else 1.0
    )
    support, resistance = _levels(frame)

    score = 0.0
    explanations: list[str] = []
    if rsi <= 30:
        score += 30
        explanations.append(f"RSI {rsi:.1f}: перепроданность")
    elif rsi >= 70:
        score -= 30
        explanations.append(f"RSI {rsi:.1f}: перекупленность")
    elif rsi >= 55:
        score += 8
        explanations.append(f"RSI {rsi:.1f}: умеренный импульс вверх")
    elif rsi <= 45:
        score -= 8
        explanations.append(f"RSI {rsi:.1f}: умеренный импульс вниз")

    previous_histogram = float(histogram.iloc[-2])
    if current_histogram > 0:
        score += 20
        label = "бычье пересечение" if previous_histogram <= 0 else "бычий импульс"
        explanations.append(f"MACD: {label}")
    elif current_histogram < 0:
        score -= 20
        label = "медвежье пересечение" if previous_histogram >= 0 else "медвежий импульс"
        explanations.append(f"MACD: {label}")

    if ema20 > ema50:
        score += 20
        explanations.append("EMA20 выше EMA50: восходящий тренд")
    else:
        score -= 20
        explanations.append("EMA20 ниже EMA50: нисходящий тренд")

    price_change = float(close.iloc[-1] / close.iloc[-2] - 1)
    if volume_ratio >= 1.8:
        volume_score = 15 if price_change > 0 else -15
        score += volume_score
        explanations.append(
            f"Объём {volume_ratio:.1f}× к среднему на {'росте' if price_change > 0 else 'снижении'}"
        )

    current_price = float(close.iloc[-1])
    if support and (current_price - support) / current_price <= 0.015:
        score += 10
        explanations.append(f"Цена у поддержки {support:.2f}")
    if resistance and (resistance - current_price) / current_price <= 0.015:
        score -= 10
        explanations.append(f"Цена у сопротивления {resistance:.2f}")

    return TechnicalResult(
        score=max(-100.0, min(100.0, score)),
        rsi=rsi,
        macd=current_macd,
        macd_signal=current_signal,
        macd_histogram=current_histogram,
        atr=atr,
        ema20=ema20,
        ema50=ema50,
        support=support,
        resistance=resistance,
        volume_ratio=volume_ratio,
        explanations=explanations,
    )

