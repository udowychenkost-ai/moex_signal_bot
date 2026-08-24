from __future__ import annotations

import json
from dataclasses import dataclass
from html import escape

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai_analyst import AIAnalystService
from app.analysis import prepare_technical_features
from app.domain import InsufficientDataError
from app.models import AIRequestLog
from app.observation import completed_candles
from app.repositories import get_candles, get_market_candles, list_active_instruments


@dataclass(frozen=True, slots=True)
class MarketOverview:
    regime: str
    volatility: str
    bullish: int
    bearish: int
    sideways: int
    oversold: int
    overbought: int
    relative_strength_leaders: tuple[tuple[str, float], ...]
    relative_strength_laggards: tuple[tuple[str, float], ...]
    oversold_tickers: tuple[tuple[str, float], ...]
    overbought_tickers: tuple[tuple[str, float], ...]
    anomalous_volume: tuple[tuple[str, float], ...]
    summary: str
    ai_summary: str


class MarketOverviewService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        benchmark: str = "IMOEX",
        ai_analyst: AIAnalystService | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.benchmark = benchmark
        self.ai_analyst = ai_analyst

    async def current(self) -> MarketOverview:
        async with self.session_factory() as session:
            instruments = await list_active_instruments(session)
            benchmark_rows = completed_candles(
                await get_market_candles(session, self.benchmark, "1d", limit=250),
                "1d",
            )
            histories = {
                item.secid: completed_candles(
                    await get_candles(session, item.secid, "1d", limit=250),
                    "1d",
                )
                for item in instruments
            }
        try:
            benchmark = prepare_technical_features(benchmark_rows)
            bull_regime = benchmark.current_price > benchmark.ema20 > benchmark.ema50
            bear_regime = benchmark.current_price < benchmark.ema20 < benchmark.ema50
            regime = "BULL" if bull_regime else ("BEAR" if bear_regime else "SIDEWAYS")
            atr_pct = benchmark.atr / benchmark.current_price * 100
            volatility = "HIGH" if atr_pct >= 3 else ("LOW" if atr_pct <= 1 else "NORMAL")
            benchmark_return = (
                benchmark_rows[-1].close / benchmark_rows[-21].close - 1
                if len(benchmark_rows) >= 21
                else 0.0
            )
        except (InsufficientDataError, IndexError, ZeroDivisionError):
            regime = "UNAVAILABLE"
            volatility = "UNAVAILABLE"
            benchmark_return = 0.0

        bullish = bearish = sideways = oversold = overbought = 0
        relative: list[tuple[str, float]] = []
        oversold_rows: list[tuple[str, float]] = []
        overbought_rows: list[tuple[str, float]] = []
        volume_rows: list[tuple[str, float]] = []
        for ticker, rows in histories.items():
            try:
                features = prepare_technical_features(rows)
            except InsufficientDataError:
                continue
            if features.current_price > features.ema20 > features.ema50:
                bullish += 1
            elif features.current_price < features.ema20 < features.ema50:
                bearish += 1
            else:
                sideways += 1
            oversold += features.rsi <= 30
            overbought += features.rsi >= 70
            if features.rsi <= 30:
                oversold_rows.append((ticker, features.rsi))
            if features.rsi >= 70:
                overbought_rows.append((ticker, features.rsi))
            if features.volume_ratio is not None and features.volume_ratio >= 1.8:
                volume_rows.append((ticker, features.volume_ratio))
            if len(rows) >= 21 and rows[-21].close:
                relative.append(
                    (ticker, (rows[-1].close / rows[-21].close - 1 - benchmark_return) * 100)
                )
        relative.sort(key=lambda item: item[1], reverse=True)
        observed = bullish + bearish + sideways
        summary = (
            f"IMOEX: {regime}. Из {observed} рассчитанных бумаг: "
            f"bullish {bullish}, bearish {bearish}, sideways {sideways}. "
            f"BUY требует повышенной relative strength и подтверждённого объёма."
            if regime == "BEAR"
            else (
                f"IMOEX: {regime}. Из {observed} рассчитанных бумаг: "
                f"bullish {bullish}, bearish {bearish}, sideways {sideways}. "
                f"Слабые SELL блокируются без независимых подтверждений."
                if regime == "BULL"
                else (
                    f"IMOEX: {regime}. Из {observed} рассчитанных бумаг: "
                    f"bullish {bullish}, bearish {bearish}, sideways {sideways}."
                )
            )
        )
        ai_summary = "AI summary unavailable; показан только deterministic market summary."
        if self.ai_analyst is not None:
            review = await self.ai_analyst.summarize_market(
                {
                    "imoex_regime": regime,
                    "volatility": volatility,
                    "bullish": bullish,
                    "bearish": bearish,
                    "sideways": sideways,
                    "oversold": oversold,
                    "overbought": overbought,
                    "relative_strength_leaders": relative[:5],
                    "relative_strength_laggards": relative[-5:],
                    "deterministic_summary": summary,
                }
            )
            ai_summary = review.summary
            async with self.session_factory() as session, session.begin():
                if review.attempts:
                    for attempt in review.attempts:
                        session.add(
                            AIRequestLog(
                                request_kind="MARKET_SUMMARY",
                                provider=attempt.provider,
                                model=attempt.model,
                                status=attempt.status,
                                input_tokens=attempt.input_tokens,
                                output_tokens=attempt.output_tokens,
                                estimated_cost_usd=attempt.estimated_cost_usd,
                                latency_ms=attempt.latency_ms,
                                error=attempt.error,
                                fallback_used=attempt.fallback_used,
                                usage_json=json.dumps(attempt.usage or {}, sort_keys=True),
                                created_at=review.reviewed_at,
                            )
                        )
                else:
                    session.add(
                        AIRequestLog(
                            request_kind="MARKET_SUMMARY",
                            provider=review.provider,
                            model=review.model,
                            status=review.status,
                            input_tokens=review.input_tokens,
                            output_tokens=review.output_tokens,
                            estimated_cost_usd=review.estimated_cost_usd,
                            latency_ms=review.latency_ms,
                            error=review.error,
                            fallback_used=review.fallback_used,
                            usage_json=json.dumps(review.usage or {}, sort_keys=True),
                            created_at=review.reviewed_at,
                        )
                    )
        return MarketOverview(
            regime=regime,
            volatility=volatility,
            bullish=bullish,
            bearish=bearish,
            sideways=sideways,
            oversold=oversold,
            overbought=overbought,
            relative_strength_leaders=tuple(relative[:5]),
            relative_strength_laggards=tuple(relative[-5:]),
            oversold_tickers=tuple(sorted(oversold_rows, key=lambda item: item[1])[:10]),
            overbought_tickers=tuple(
                sorted(overbought_rows, key=lambda item: item[1], reverse=True)[:10]
            ),
            anomalous_volume=tuple(
                sorted(volume_rows, key=lambda item: item[1], reverse=True)[:10]
            ),
            summary=summary,
            ai_summary=ai_summary,
        )


def format_market_overview(overview: MarketOverview) -> str:
    leaders = (
        ", ".join(
            f"{escape(ticker)} {score:+.1f}%"
            for ticker, score in overview.relative_strength_leaders
        )
        or "нет данных"
    )
    laggards = (
        ", ".join(
            f"{escape(ticker)} {score:+.1f}%"
            for ticker, score in overview.relative_strength_laggards
        )
        or "нет данных"
    )
    return (
        "🧠 <b>АНАЛИЗ РЫНКА</b>\n\n"
        f"IMOEX: <b>{escape(overview.regime)}</b>\n"
        f"Volatility: <b>{escape(overview.volatility)}</b>\n\n"
        f"Bullish: <b>{overview.bullish}</b> · Bearish: <b>{overview.bearish}</b> · "
        f"Sideways: <b>{overview.sideways}</b>\n"
        f"Oversold: <b>{overview.oversold}</b> · Overbought: <b>{overview.overbought}</b>\n\n"
        f"💪 RS leaders: {leaders}\n"
        f"📉 RS laggards: {laggards}\n\n"
        f"<b>Системный вывод</b>\n{escape(overview.summary)}\n\n"
        f"<b>AI summary</b>\n{escape(overview.ai_summary)}"
    )
