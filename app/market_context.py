from __future__ import annotations

import math
from datetime import UTC, datetime

import pandas as pd
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain import MarketContextData, StaleMarketDataError
from app.observation import TIMEFRAME_DURATIONS, aware_utc, completed_candles
from app.repositories import get_candles, get_market_candles

ANNUALIZATION = {
    "5m": 252 * 78,
    "15m": 252 * 26,
    "1h": 252 * 7,
    "4h": 252 * 2,
    "1d": 252,
    "1w": 52,
}


def _frame(candles: list[object], as_of: datetime) -> pd.DataFrame:
    cutoff = aware_utc(as_of)
    rows = [
        {
            "end": aware_utc(candle.end),
            "high": float(candle.high),
            "low": float(candle.low),
            "close": float(candle.close),
        }
        for candle in candles
        if aware_utc(candle.end) <= cutoff
    ]
    if not rows:
        return pd.DataFrame(columns=["high", "low", "close"])
    return pd.DataFrame(rows).drop_duplicates("end").set_index("end").sort_index()


def _return_pct(frame: pd.DataFrame, periods: int = 20) -> float:
    if len(frame) < 2:
        return 0.0
    first = float(frame["close"].iloc[max(0, len(frame) - periods - 1)])
    return (float(frame["close"].iloc[-1]) / first - 1) * 100


def analyze_market_context(
    benchmark_candles: list[object],
    instrument_candles: list[object],
    *,
    benchmark: str = "IMOEX",
    timeframe: str = "1d",
    as_of: datetime | None = None,
) -> MarketContextData:
    """Classify market state using decision-time candles only.

    The regime is intentionally a continuous score first and a label second, so
    it can penalize or support an idea without becoming an absolute trade ban.
    """
    decision_at = aware_utc(
        as_of or (instrument_candles[-1].end if instrument_candles else datetime.now(UTC))
    )
    market = _frame(benchmark_candles, decision_at)
    instrument = _frame(instrument_candles, decision_at)
    if len(market) < 60:
        raise StaleMarketDataError(
            f"{benchmark} {timeframe}: need 60 completed benchmark candles, got {len(market)}"
        )
    if len(instrument) < 21:
        raise StaleMarketDataError(
            f"instrument {timeframe}: need 21 completed candles, got {len(instrument)}"
        )

    close = market["close"]
    last = float(close.iloc[-1])
    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    sma200 = close.rolling(200).mean()
    return20 = _return_pct(market)
    slope20 = (float(ema50.iloc[-1]) / float(ema50.iloc[-21]) - 1) * 100
    local_high = float(close.iloc[-60:].max())
    drawdown = (last / local_high - 1) * 100

    points = 0.0
    points += 22 if last > float(ema20.iloc[-1]) else -22
    points += 22 if float(ema20.iloc[-1]) > float(ema50.iloc[-1]) else -22
    if math.isfinite(float(sma200.iloc[-1])):
        points += 24 if last > float(sma200.iloc[-1]) else -24
    points += max(-18.0, min(18.0, return20 * 2.0))
    points += max(-10.0, min(10.0, slope20 * 3.0))
    if drawdown <= -15:
        points -= 12
    elif drawdown <= -8:
        points -= 6
    regime_score = max(-100.0, min(100.0, points))
    regime = "BULL" if regime_score >= 25 else ("BEAR" if regime_score <= -25 else "SIDEWAYS")

    returns = close.pct_change()
    rolling_vol = returns.rolling(20).std() * math.sqrt(ANNUALIZATION[timeframe]) * 100
    realized_vol = float(rolling_vol.iloc[-1])
    vol_history = rolling_vol.dropna().iloc[-250:]
    percentile = (
        float((vol_history <= realized_vol).sum()) / len(vol_history) * 100
        if not vol_history.empty and math.isfinite(realized_vol)
        else 50.0
    )
    if percentile >= 90:
        volatility = "EXTREME"
    elif percentile >= 70:
        volatility = "HIGH"
    elif percentile <= 30:
        volatility = "LOW"
    else:
        volatility = "NORMAL"

    previous = close.shift(1)
    true_range = pd.concat(
        [
            market["high"] - market["low"],
            (market["high"] - previous).abs(),
            (market["low"] - previous).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr_pct = float(true_range.ewm(alpha=1 / 14, adjust=False).mean().iloc[-1]) / last * 100

    benchmark_return = return20
    instrument_return = _return_pct(instrument)
    relative = instrument_return - benchmark_return
    relative_score = max(-100.0, min(100.0, relative * 8.0))
    if relative_score >= 20:
        relative_label = "выше рынка"
    elif relative_score <= -20:
        relative_label = "ниже рынка"
    else:
        relative_label = "на уровне рынка"

    return MarketContextData(
        benchmark=benchmark.upper(),
        regime=regime,
        volatility=volatility,
        regime_score=round(regime_score, 4),
        relative_strength_score=round(relative_score, 4),
        relative_strength_label=relative_label,
        benchmark_return_pct=round(benchmark_return, 4),
        instrument_return_pct=round(instrument_return, 4),
        drawdown_pct=round(drawdown, 4),
        realized_volatility_pct=round(realized_vol, 4),
        atr_pct=round(atr_pct, 4),
        as_of=decision_at,
    )


class MarketRegimeService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        benchmark: str = "IMOEX",
    ) -> None:
        self.session_factory = session_factory
        self.benchmark = benchmark.upper()

    async def analyze(
        self,
        ticker: str,
        timeframe: str,
        *,
        as_of: datetime | None = None,
        instrument_candles: list[object] | None = None,
    ) -> MarketContextData:
        async with self.session_factory() as session:
            benchmark_candles = await get_market_candles(
                session, self.benchmark, timeframe, limit=500
            )
            stock_candles = instrument_candles or await get_candles(
                session, ticker, timeframe, limit=500
            )
        checked_at = aware_utc(as_of or datetime.now(UTC))
        benchmark_candles = completed_candles(benchmark_candles, timeframe, now=checked_at)
        stock_candles = completed_candles(stock_candles, timeframe, now=checked_at)
        decision_at = aware_utc(stock_candles[-1].end) if stock_candles else checked_at
        market_as_of = [
            candle for candle in benchmark_candles if aware_utc(candle.end) <= decision_at
        ]
        if not market_as_of:
            raise StaleMarketDataError(
                f"{self.benchmark} {timeframe}: no completed benchmark candle at decision time"
            )
        gap = decision_at - aware_utc(market_as_of[-1].end)
        if gap > TIMEFRAME_DURATIONS[timeframe] * 2:
            raise StaleMarketDataError(f"{self.benchmark} {timeframe}: benchmark is stale by {gap}")
        return analyze_market_context(
            market_as_of,
            stock_candles,
            benchmark=self.benchmark,
            timeframe=timeframe,
            as_of=decision_at,
        )
