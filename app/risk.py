from __future__ import annotations

from app.domain import PositionSize, RiskLevels


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
    if action not in {"BUY", "SELL"}:
        raise ValueError("action must be BUY or SELL")
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
        method="atr",
    )


def level_risk_levels(
    *,
    action: str,
    entry: float,
    support_levels: list[float],
    resistance_levels: list[float],
    buffer_pct: float = 0.3,
    minimum_reward_risk_ratio: float = 2.0,
) -> RiskLevels | None:
    """Build directional levels, returning None when the setup is unsafe."""
    if action not in {"BUY", "SELL"}:
        raise ValueError("action must be BUY or SELL")
    if entry <= 0:
        raise ValueError("entry must be positive")
    if buffer_pct < 0:
        raise ValueError("buffer_pct must be non-negative")
    if minimum_reward_risk_ratio < 1:
        raise ValueError("minimum_reward_risk_ratio must be at least 1")

    supports_below = [level for level in support_levels if 0 < level < entry]
    resistances_above = [level for level in resistance_levels if level > entry]
    if not supports_below or not resistances_above:
        return None

    buffer = buffer_pct / 100
    if action == "BUY":
        stop_loss = max(supports_below) * (1 - buffer)
        take_profit = min(resistances_above) * (1 - buffer)
        if not stop_loss < entry < take_profit:
            return None
    else:
        stop_loss = min(resistances_above) * (1 + buffer)
        take_profit = max(supports_below) * (1 + buffer)
        if not take_profit < entry < stop_loss:
            return None

    risk = abs(entry - stop_loss)
    reward = abs(take_profit - entry)
    if risk <= 0 or reward / risk < minimum_reward_risk_ratio:
        return None
    return RiskLevels(
        entry=entry,
        stop_loss=stop_loss,
        take_profit=take_profit,
        risk_pct=risk / entry * 100,
        reward_risk_ratio=reward / risk,
        method="levels",
    )


def calculate_position_size(
    *,
    deposit: float,
    risk_per_trade_pct: float,
    entry: float,
    stop_loss: float,
    lot_size: int = 1,
    cap_to_cash: bool = True,
) -> PositionSize:
    if min(deposit, risk_per_trade_pct, entry, lot_size) <= 0:
        raise ValueError("deposit, risk, entry and lot size must be positive")
    risk_per_unit = abs(entry - stop_loss)
    if risk_per_unit == 0:
        raise ValueError("stop loss must differ from entry")

    risk_budget = deposit * risk_per_trade_pct / 100
    risk_units = int(risk_budget // risk_per_unit)
    cash_units = int(deposit // entry)
    raw_units = min(risk_units, cash_units) if cap_to_cash else risk_units
    units = (raw_units // lot_size) * lot_size
    return PositionSize(
        units=units,
        lots=units // lot_size,
        risk_budget=risk_budget,
        actual_risk=units * risk_per_unit,
        position_value=units * entry,
        capped_by_cash=cap_to_cash and cash_units < risk_units,
    )


def position_size(
    *, deposit: float, risk_per_trade_pct: float, entry: float, stop_loss: float, lot_size: int = 1
) -> int:
    """Backwards-compatible risk-only sizing; use calculate_position_size for details."""
    return calculate_position_size(
        deposit=deposit,
        risk_per_trade_pct=risk_per_trade_pct,
        entry=entry,
        stop_loss=stop_loss,
        lot_size=lot_size,
        cap_to_cash=False,
    ).units
