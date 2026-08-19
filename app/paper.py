from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.domain import IdeaStatus
from app.models import Instrument, PaperTrade, TradingIdea
from app.risk import calculate_position_size, calculate_trade_pnl

TERMINAL_IDEA_STATUSES = {
    IdeaStatus.TP_HIT.value,
    IdeaStatus.SL_HIT.value,
    IdeaStatus.EXPIRED.value,
    IdeaStatus.CANCELLED.value,
    IdeaStatus.INVALIDATED.value,
}


@dataclass(frozen=True, slots=True)
class PaperPortfolioSummary:
    account_size: float
    equity: float
    open_positions: int
    closed_trades: int
    wins: int
    losses: int
    win_rate: float
    net_pnl: float
    return_pct: float
    average_r: float


class PaperTradingService:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory

    async def _equity(self, session: AsyncSession) -> float:
        realized = await session.scalar(
            select(func.coalesce(func.sum(PaperTrade.net_pnl), 0.0)).where(
                PaperTrade.status == "CLOSED"
            )
        )
        return self.settings.paper_account_size + float(realized or 0)

    async def sync_idea(self, idea_id: int) -> PaperTrade | None:
        async with self.session_factory() as session, session.begin():
            idea = await session.get(TradingIdea, idea_id)
            if idea is None or idea.activation_price is None or idea.activated_at is None:
                return None
            trade = await session.scalar(select(PaperTrade).where(PaperTrade.idea_id == idea.id))
            if trade is None:
                instrument = await session.get(Instrument, idea.ticker)
                equity = await self._equity(session)
                size = calculate_position_size(
                    deposit=equity,
                    risk_per_trade_pct=self.settings.default_risk_per_trade_pct,
                    entry=idea.activation_price,
                    stop_loss=idea.stop_loss,
                    lot_size=instrument.lot_size if instrument and instrument.lot_size else 1,
                )
                trade = PaperTrade(
                    idea_id=idea.id,
                    ticker=idea.ticker,
                    direction=idea.direction,
                    status="OPEN",
                    entry_price=idea.activation_price,
                    units=size.units,
                    lots=size.lots,
                    risk_budget=size.risk_budget,
                    actual_risk=size.actual_risk,
                    position_value=size.position_value,
                    opened_at=idea.activated_at,
                )
                session.add(trade)
                await session.flush()

            if (
                trade.status == "OPEN"
                and idea.status in TERMINAL_IDEA_STATUSES
                and idea.close_price is not None
                and idea.closed_at is not None
            ):
                pnl = calculate_trade_pnl(
                    direction=trade.direction,
                    entry_price=trade.entry_price,
                    exit_price=idea.close_price,
                    units=trade.units,
                    commission_pct=self.settings.backtest_commission_pct,
                    actual_risk=trade.actual_risk,
                )
                trade.status = "CLOSED"
                trade.exit_price = idea.close_price
                trade.gross_pnl = pnl.gross_pnl
                trade.commission = pnl.commission
                trade.net_pnl = pnl.net_pnl
                trade.r_multiple = pnl.r_multiple
                trade.closed_at = idea.closed_at
                trade.exit_reason = idea.close_reason or idea.status
            return trade

    async def sync_all(self) -> dict[str, int]:
        async with self.session_factory() as session:
            idea_ids = list(
                await session.scalars(
                    select(TradingIdea.id).where(TradingIdea.activation_price.is_not(None))
                )
            )
        opened = 0
        closed = 0
        for idea_id in idea_ids:
            trade = await self.sync_idea(idea_id)
            if trade is None:
                continue
            if trade.status == "OPEN":
                opened += 1
            else:
                closed += 1
        return {"open": opened, "closed": closed}

    async def summary(self) -> PaperPortfolioSummary:
        async with self.session_factory() as session:
            trades = list(await session.scalars(select(PaperTrade)))
        closed = [trade for trade in trades if trade.status == "CLOSED"]
        wins = [trade for trade in closed if trade.net_pnl > 0]
        losses = [trade for trade in closed if trade.net_pnl < 0]
        net_pnl = sum(trade.net_pnl for trade in closed)
        average_r = sum(trade.r_multiple for trade in closed) / len(closed) if closed else 0.0
        return PaperPortfolioSummary(
            account_size=self.settings.paper_account_size,
            equity=self.settings.paper_account_size + net_pnl,
            open_positions=sum(trade.status == "OPEN" for trade in trades),
            closed_trades=len(closed),
            wins=len(wins),
            losses=len(losses),
            win_rate=len(wins) / len(closed) * 100 if closed else 0.0,
            net_pnl=net_pnl,
            return_pct=net_pnl / self.settings.paper_account_size * 100,
            average_r=average_r,
        )


def format_paper_summary(summary: PaperPortfolioSummary) -> str:
    return (
        "📊 <b>Paper trading</b>\n\n"
        f"Стартовый капитал: <b>{summary.account_size:,.2f} ₽</b>\n"
        f"Текущий капитал: <b>{summary.equity:,.2f} ₽</b>\n"
        f"Открытых позиций: <b>{summary.open_positions}</b>\n"
        f"Закрытых сделок: <b>{summary.closed_trades}</b>\n"
        f"Win rate: <b>{summary.win_rate:.1f}%</b>\n"
        f"P&L: <b>{summary.net_pnl:+,.2f} ₽ ({summary.return_pct:+.2f}%)</b>\n"
        f"Средний R: <b>{summary.average_r:+.2f}</b>"
    )
