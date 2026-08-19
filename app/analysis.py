from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd
import ta

from app.domain import InsufficientDataError, TechnicalResult

DEFAULT_SCORING_WEIGHTS = {
    "trend": 25.0,
    "momentum": 25.0,
    "macd": 20.0,
    "bollinger": 15.0,
    "volume": 15.0,
}


@dataclass(frozen=True, slots=True)
class TechnicalFeatures:
    current_price: float
    previous_close: float
    rsi: float
    macd: float
    macd_signal: float
    macd_histogram: float
    previous_macd_histogram: float
    atr: float
    ema20: float
    ema50: float
    volume_ratio: float | None
    support: float | None
    resistance: float | None
    legacy_support: float | None
    legacy_resistance: float | None
    sma20: float | None
    sma50: float | None
    sma200: float | None
    adx: float | None
    stochastic_k: float | None
    stochastic_d: float | None
    cci: float | None
    bb_high: float | None
    bb_low: float | None
    bb_percent: float | None
    obv: float | None
    support_levels: list[float]
    resistance_levels: list[float]


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
    """Wilder-style RSI retained for backwards-compatible signal behaviour."""
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


def add_indicators(frame: pd.DataFrame) -> pd.DataFrame:
    """Return one canonical indicator frame without changing the input frame."""
    out = frame.copy()
    close = out["close"].astype(float)
    high = out["high"].astype(float)
    low = out["low"].astype(float)
    volume = out["volume"].astype(float)

    out["sma_20"] = close.rolling(20).mean()
    out["sma_50"] = close.rolling(50).mean()
    out["sma_200"] = close.rolling(200).mean()
    out["ema_20"] = close.ewm(span=20, adjust=False).mean()
    out["ema_50"] = close.ewm(span=50, adjust=False).mean()
    out["rsi_14"] = calculate_rsi(close)
    out["macd"], out["macd_signal"], out["macd_histogram"] = calculate_macd(close)
    out["atr_14"] = calculate_atr(out)

    out["adx_14"] = ta.trend.ADXIndicator(high, low, close, window=14).adx()
    stochastic = ta.momentum.StochasticOscillator(high, low, close, window=14)
    out["stochastic_k"] = stochastic.stoch()
    out["stochastic_d"] = stochastic.stoch_signal()
    out["cci_20"] = ta.trend.CCIIndicator(high, low, close, window=20).cci()

    bollinger = ta.volatility.BollingerBands(close, window=20, window_dev=2)
    out["bb_high"] = bollinger.bollinger_hband()
    out["bb_low"] = bollinger.bollinger_lband()
    out["bb_mid"] = bollinger.bollinger_mavg()
    out["bb_percent"] = bollinger.bollinger_pband()

    out["obv"] = ta.volume.OnBalanceVolumeIndicator(close, volume).on_balance_volume()
    out["volume_sma_20"] = volume.rolling(20).mean()
    out["volume_ratio"] = volume / out["volume_sma_20"].replace(0, np.nan)
    return out


def _cluster_levels(levels: pd.Series, tolerance_pct: float) -> list[float]:
    values = sorted(float(value) for value in levels.dropna())
    if not values:
        return []
    clusters: list[list[float]] = [[values[0]]]
    for value in values[1:]:
        center = sum(clusters[-1]) / len(clusters[-1])
        if center and abs(value - center) / center * 100 <= tolerance_pct:
            clusters[-1].append(value)
        else:
            clusters.append([value])
    return [round(sum(cluster) / len(cluster), 6) for cluster in clusters]


def detect_support_resistance(
    frame: pd.DataFrame,
    *,
    window: int = 5,
    tolerance_pct: float = 0.5,
) -> dict[str, list[float]]:
    """Find true local extrema and cluster nearby price levels."""
    if window < 1:
        raise ValueError("window must be positive")
    if tolerance_pct < 0:
        raise ValueError("tolerance_pct must be non-negative")
    span = window * 2 + 1
    local_lows = frame["low"].where(
        frame["low"] == frame["low"].rolling(span, center=True, min_periods=span).min()
    )
    local_highs = frame["high"].where(
        frame["high"] == frame["high"].rolling(span, center=True, min_periods=span).max()
    )
    return {
        "support": _cluster_levels(local_lows, tolerance_pct),
        "resistance": _cluster_levels(local_highs, tolerance_pct),
    }


def _legacy_levels(frame: pd.DataFrame, window: int = 5) -> tuple[float | None, float | None]:
    """Preserve the pre-integration level selection used by legacy scoring."""
    current = float(frame["close"].iloc[-1])
    local_lows = frame["low"].where(frame["low"] == frame["low"].rolling(window, center=True).min())
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


def _optional_last(frame: pd.DataFrame, column: str) -> float | None:
    value = float(frame[column].iloc[-1])
    return value if math.isfinite(value) else None


def _legacy_score(
    features: TechnicalFeatures,
    support: float | None,
    resistance: float | None,
) -> tuple[float, list[str], dict[str, float]]:
    rsi = features.rsi
    histogram = features.macd_histogram
    previous_histogram = features.previous_macd_histogram
    ema20 = features.ema20
    ema50 = features.ema50
    volume_ratio = features.volume_ratio
    close = features.current_price
    previous_close = features.previous_close

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

    if histogram > 0:
        score += 20
        label = "бычье пересечение" if previous_histogram <= 0 else "бычий импульс"
        explanations.append(f"MACD: {label}")
    elif histogram < 0:
        score -= 20
        label = "медвежье пересечение" if previous_histogram >= 0 else "медвежий импульс"
        explanations.append(f"MACD: {label}")

    if ema20 > ema50:
        score += 20
        explanations.append("EMA20 выше EMA50: восходящий тренд")
    else:
        score -= 20
        explanations.append("EMA20 ниже EMA50: нисходящий тренд")

    price_change = close / previous_close - 1
    if volume_ratio is not None and volume_ratio >= 1.8:
        score += 15 if price_change > 0 else -15
        explanations.append(
            f"Объём {volume_ratio:.1f}× к среднему на {'росте' if price_change > 0 else 'снижении'}"
        )

    if support and (close - support) / close <= 0.015:
        score += 10
        explanations.append(f"Цена у поддержки {support:.2f}")
    if resistance and (resistance - close) / close <= 0.015:
        score -= 10
        explanations.append(f"Цена у сопротивления {resistance:.2f}")

    bounded = max(-100.0, min(100.0, score))
    return bounded, explanations, {"legacy": bounded}


def _weighted_score(
    features: TechnicalFeatures,
    weights: dict[str, float],
) -> tuple[float, list[str], dict[str, float]]:
    close = features.current_price
    previous_close = features.previous_close
    explanations: list[str] = []

    trend = 0.0
    sma20 = features.sma20
    sma50 = features.sma50
    sma200 = features.sma200
    if sma20 is not None and sma50 is not None:
        if close > sma20 > sma50:
            trend = 0.8
            explanations.append("Цена выше SMA20 и SMA50: восходящий тренд")
        elif close < sma20 < sma50:
            trend = -0.8
            explanations.append("Цена ниже SMA20 и SMA50: нисходящий тренд")
        elif close > sma50:
            trend = 0.35
        elif close < sma50:
            trend = -0.35
    if sma200 is not None:
        trend = max(-1.0, min(1.0, trend + (0.2 if close > sma200 else -0.2)))
    adx = features.adx
    if adx is not None and adx < 20:
        trend *= 0.6
    elif adx is not None and adx >= 25 and trend:
        explanations.append(f"ADX {adx:.1f}: тренд подтверждён")

    rsi = features.rsi
    if rsi <= 30:
        rsi_component = 1.0
        explanations.append(f"RSI {rsi:.1f}: перепроданность")
    elif rsi >= 70:
        rsi_component = -1.0
        explanations.append(f"RSI {rsi:.1f}: перекупленность")
    elif rsi >= 55:
        rsi_component = 0.3
    elif rsi <= 45:
        rsi_component = -0.3
    else:
        rsi_component = 0.0

    stochastic = features.stochastic_k
    stochastic_component = 0.0
    if stochastic is not None and stochastic <= 20:
        stochastic_component = 1.0
        explanations.append(f"Stochastic {stochastic:.1f}: перепроданность")
    elif stochastic is not None and stochastic >= 80:
        stochastic_component = -1.0
        explanations.append(f"Stochastic {stochastic:.1f}: перекупленность")

    cci = features.cci
    cci_component = 0.0
    if cci is not None and cci <= -100:
        cci_component = 1.0
    elif cci is not None and cci >= 100:
        cci_component = -1.0
    momentum = rsi_component * 0.6 + stochastic_component * 0.2 + cci_component * 0.2

    histogram = features.macd_histogram
    previous_histogram = features.previous_macd_histogram
    if previous_histogram <= 0 < histogram:
        macd_component = 1.0
        explanations.append("MACD пересёк сигнальную линию вверх")
    elif previous_histogram >= 0 > histogram:
        macd_component = -1.0
        explanations.append("MACD пересёк сигнальную линию вниз")
    else:
        macd_component = 0.5 if histogram > 0 else (-0.5 if histogram < 0 else 0.0)

    bb_percent = features.bb_percent
    bollinger = 0.0
    if bb_percent is not None and bb_percent <= 0:
        bollinger = 1.0
        explanations.append("Цена у или ниже нижней полосы Боллинджера")
    elif bb_percent is not None and bb_percent >= 1:
        bollinger = -1.0
        explanations.append("Цена у или выше верхней полосы Боллинджера")
    elif bb_percent is not None and bb_percent <= 0.15:
        bollinger = 0.4
    elif bb_percent is not None and bb_percent >= 0.85:
        bollinger = -0.4

    volume_ratio = features.volume_ratio
    volume = 0.0
    if volume_ratio is not None and volume_ratio >= 1.8:
        volume = 1.0 if close > previous_close else -1.0
        explanations.append(
            f"Объём {volume_ratio:.1f}× к среднему на {'росте' if volume > 0 else 'снижении'}"
        )

    raw_components = {
        "trend": trend,
        "momentum": momentum,
        "macd": macd_component,
        "bollinger": bollinger,
        "volume": volume,
    }
    contributions = {
        name: round(raw_components[name] * weights[name], 2) for name in raw_components
    }
    score = max(-100.0, min(100.0, sum(contributions.values())))
    return round(score, 2), explanations, contributions


def prepare_technical_features(candles: list[object]) -> TechnicalFeatures:
    if len(candles) < 60:
        raise InsufficientDataError(
            f"Нужно минимум 60 свечей для сигнала, сейчас доступно {len(candles)}"
        )
    frame = add_indicators(candle_frame(candles))
    last = frame.iloc[-1]
    levels = detect_support_resistance(frame)
    current_price = float(last["close"])
    supports_below = [level for level in levels["support"] if level < current_price]
    resistances_above = [level for level in levels["resistance"] if level > current_price]
    support = max(supports_below, default=None)
    resistance = min(resistances_above, default=None)
    legacy_support, legacy_resistance = _legacy_levels(frame)
    return TechnicalFeatures(
        current_price=current_price,
        previous_close=float(frame["close"].iloc[-2]),
        rsi=_finite(float(last["rsi_14"]), "RSI"),
        macd=_finite(float(last["macd"]), "MACD"),
        macd_signal=_finite(float(last["macd_signal"]), "MACD signal"),
        macd_histogram=_finite(float(last["macd_histogram"]), "MACD histogram"),
        previous_macd_histogram=float(frame["macd_histogram"].iloc[-2]),
        atr=_finite(float(last["atr_14"]), "ATR"),
        ema20=_finite(float(last["ema_20"]), "EMA20"),
        ema50=_finite(float(last["ema_50"]), "EMA50"),
        volume_ratio=_optional_last(frame, "volume_ratio"),
        support=support,
        resistance=resistance,
        legacy_support=legacy_support,
        legacy_resistance=legacy_resistance,
        sma20=_optional_last(frame, "sma_20"),
        sma50=_optional_last(frame, "sma_50"),
        sma200=_optional_last(frame, "sma_200"),
        adx=_optional_last(frame, "adx_14"),
        stochastic_k=_optional_last(frame, "stochastic_k"),
        stochastic_d=_optional_last(frame, "stochastic_d"),
        cci=_optional_last(frame, "cci_20"),
        bb_high=_optional_last(frame, "bb_high"),
        bb_low=_optional_last(frame, "bb_low"),
        bb_percent=_optional_last(frame, "bb_percent"),
        obv=_optional_last(frame, "obv"),
        support_levels=levels["support"],
        resistance_levels=levels["resistance"],
    )


def score_technical_features(
    features: TechnicalFeatures,
    *,
    scoring_model: str = "legacy",
    weights: dict[str, float] | None = None,
) -> TechnicalResult:
    support = features.support
    resistance = features.resistance

    if scoring_model == "legacy":
        support = features.legacy_support
        resistance = features.legacy_resistance
        score, explanations, component_scores = _legacy_score(features, support, resistance)
    elif scoring_model == "weighted":
        configured_weights = weights or DEFAULT_SCORING_WEIGHTS
        missing = set(DEFAULT_SCORING_WEIGHTS) - set(configured_weights)
        if missing:
            raise ValueError(f"Missing technical score weights: {', '.join(sorted(missing))}")
        total = sum(configured_weights.values())
        if total <= 0:
            raise ValueError("At least one technical score weight must be positive")
        normalized = {name: configured_weights[name] / total * 100 for name in configured_weights}
        score, explanations, component_scores = _weighted_score(features, normalized)
    else:
        raise ValueError(f"Unsupported scoring model: {scoring_model}")

    return TechnicalResult(
        score=score,
        rsi=features.rsi,
        macd=features.macd,
        macd_signal=features.macd_signal,
        macd_histogram=features.macd_histogram,
        atr=features.atr,
        ema20=features.ema20,
        ema50=features.ema50,
        support=support,
        resistance=resistance,
        volume_ratio=features.volume_ratio if features.volume_ratio is not None else 1.0,
        explanations=explanations,
        sma20=features.sma20,
        sma50=features.sma50,
        sma200=features.sma200,
        adx=features.adx,
        stochastic_k=features.stochastic_k,
        stochastic_d=features.stochastic_d,
        cci=features.cci,
        bb_high=features.bb_high,
        bb_low=features.bb_low,
        bb_percent=features.bb_percent,
        obv=features.obv,
        support_levels=features.support_levels,
        resistance_levels=features.resistance_levels,
        component_scores=component_scores,
    )


def analyze_technical(
    candles: list[object],
    *,
    scoring_model: str = "legacy",
    weights: dict[str, float] | None = None,
    features: TechnicalFeatures | None = None,
) -> TechnicalResult:
    prepared = features or prepare_technical_features(candles)
    return score_technical_features(
        prepared,
        scoring_model=scoring_model,
        weights=weights,
    )
