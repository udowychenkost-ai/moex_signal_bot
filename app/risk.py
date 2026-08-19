from __future__ import annotations

from app.domain import RiskLevels


def atr_risk_levels(
    *,
    action: str,
    entry: float,
    atr: float,
    stop_multiplier: float,
    take_multiplier: float,
) -> RiskLevels:
    if entry <= 0 or atr <= 0:
        raise ValueError("entry and ATR must be positive")
    if stop_multiplier <= 0 or take_multiplier <= 0:
        raise ValueError("ATR multipliers must be positive")
    if take_multiplier / stop_multiplier < 2:
        raise ValueError("Configured reward:risk ratio must be at least 2:1")

    if action == "SELL":
        stop_loss = entry + stop_multiplier * atr
        take_profit = max(0.0, entry - take_multiplier * atr)
    else:
        stop_loss = max(0.0, entry - stop_multiplier * atr)
        take_profit = entry + take_multiplier * atr
    risk = abs(entry - stop_loss)
    reward = abs(take_profit - entry)
    return RiskLevels(
        entry=entry,
        stop_loss=stop_loss,
        take_profit=take_profit,
        risk_pct=risk / entry * 100,
        reward_risk_ratio=reward / risk,
    )


def position_size(
    *, deposit: float, risk_per_trade_pct: float, entry: float, stop_loss: float, lot_size: int = 1
) -> int:
    if min(deposit, risk_per_trade_pct, entry, lot_size) <= 0:
        raise ValueError("deposit, risk, entry and lot size must be positive")
    risk_per_unit = abs(entry - stop_loss)
    if risk_per_unit == 0:
        raise ValueError("stop loss must differ from entry")
    units = int((deposit * risk_per_trade_pct / 100) // risk_per_unit)
    return (units // lot_size) * lot_size

