from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.risk_v24 import RiskBudgetPolicy, RiskBudgetRepository


class RiskPolicyAuthorizationError(PermissionError):
    pass


class RiskPolicyConfirmationRequired(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RiskPolicyDraft:
    configuration_version: str
    working_capital_rub: float
    max_risk_per_trade_pct: float
    max_daily_loss_pct: float
    max_portfolio_heat_pct: float
    max_sector_heat_pct: float
    max_correlated_factor_heat_pct: float
    available_capital_pct: float

    def __post_init__(self) -> None:
        values = (
            self.working_capital_rub,
            self.max_risk_per_trade_pct,
            self.max_daily_loss_pct,
            self.max_portfolio_heat_pct,
            self.max_sector_heat_pct,
            self.max_correlated_factor_heat_pct,
            self.available_capital_pct,
        )
        if not self.configuration_version.strip() or any(value <= 0 for value in values):
            raise ValueError("Risk policy version and all limits must be positive")
        if any(value > 100 for value in values[1:]):
            raise ValueError("Percentage risk limits cannot exceed 100")

    def policy(self, *, effective_from: datetime) -> RiskBudgetPolicy:
        return RiskBudgetPolicy(
            scope="GLOBAL",
            configuration_version=self.configuration_version,
            working_capital_rub=self.working_capital_rub,
            max_risk_per_trade_pct=self.max_risk_per_trade_pct,
            max_daily_loss_pct=self.max_daily_loss_pct,
            max_portfolio_heat_pct=self.max_portfolio_heat_pct,
            max_sector_heat_pct=self.max_sector_heat_pct,
            max_correlated_factor_heat_pct=self.max_correlated_factor_heat_pct,
            available_capital_pct=self.available_capital_pct,
            configured_at=effective_from,
            effective_from=effective_from,
        )


class RiskPolicyAdminService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        admin_ids: list[int],
    ) -> None:
        self.session_factory = session_factory
        self.admin_ids = frozenset(admin_ids)
        self.repository = RiskBudgetRepository()

    def require_admin(self, telegram_id: int) -> None:
        if telegram_id not in self.admin_ids:
            raise RiskPolicyAuthorizationError("Risk Budget доступен только администратору")

    async def current(self, telegram_id: int) -> RiskBudgetPolicy | None:
        self.require_admin(telegram_id)
        async with self.session_factory() as session:
            return await self.repository.effective_policy(session)

    async def activate(
        self,
        telegram_id: int,
        draft: RiskPolicyDraft,
        *,
        confirmed: bool,
        now: datetime | None = None,
    ) -> RiskBudgetPolicy:
        self.require_admin(telegram_id)
        if not confirmed:
            raise RiskPolicyConfirmationRequired("Explicit confirmation is required")
        effective = now or datetime.now(UTC)
        if effective.tzinfo is None:
            raise ValueError("Risk policy effective time must be timezone-aware")
        policy = draft.policy(effective_from=effective)
        async with self.session_factory() as session, session.begin():
            await self.repository.create_policy(
                session,
                policy,
                created_by_telegram_id=telegram_id,
                notes="Activated through Telegram admin wizard",
            )
        return policy


def format_risk_policy(policy: RiskBudgetPolicy | RiskPolicyDraft | None) -> str:
    if policy is None:
        return "Risk Budget: <b>NOT CONFIGURED</b>"
    return (
        f"Версия: <code>{policy.configuration_version}</code>\n"
        f"Рабочий капитал: <b>{policy.working_capital_rub:,.2f} ₽</b>\n"
        f"Риск на сделку: <b>{policy.max_risk_per_trade_pct:g}%</b>\n"
        f"Дневной лимит: <b>{policy.max_daily_loss_pct:g}%</b>\n"
        f"Portfolio heat: <b>{policy.max_portfolio_heat_pct:g}%</b>\n"
        f"Sector heat: <b>{policy.max_sector_heat_pct:g}%</b>\n"
        f"Correlation/crowding heat: <b>{policy.max_correlated_factor_heat_pct:g}%</b>\n"
        f"Доступный капитал: <b>{policy.available_capital_pct:g}%</b>"
    )
