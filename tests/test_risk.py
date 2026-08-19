import pytest

from app.risk import (
    atr_risk_levels,
    calculate_position_size,
    calculate_trade_pnl,
    level_risk_levels,
    position_size,
)


def test_atr_levels_for_buy_keep_two_to_one_ratio() -> None:
    result = atr_risk_levels(action="BUY", entry=100, atr=2, stop_multiplier=1.5, take_multiplier=3)
    assert result.stop_loss == pytest.approx(97)
    assert result.take_profit == pytest.approx(106)
    assert result.reward_risk_ratio == pytest.approx(2)
    assert result.risk_pct == pytest.approx(3)


def test_atr_levels_for_sell_are_mirrored() -> None:
    result = atr_risk_levels(
        action="SELL", entry=100, atr=2, stop_multiplier=1.5, take_multiplier=3
    )
    assert result.stop_loss == pytest.approx(103)
    assert result.take_profit == pytest.approx(94)


def test_invalid_reward_risk_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least 2:1"):
        atr_risk_levels(action="BUY", entry=100, atr=2, stop_multiplier=2, take_multiplier=3)


def test_position_size_respects_lot() -> None:
    assert (
        position_size(deposit=100_000, risk_per_trade_pct=1, entry=100, stop_loss=95, lot_size=10)
        == 200
    )


def test_level_based_levels_for_buy_require_valid_reward_risk() -> None:
    result = level_risk_levels(
        action="BUY",
        entry=100,
        support_levels=[90, 95],
        resistance_levels=[112, 120],
        buffer_pct=0,
    )
    assert result is not None
    assert result.stop_loss == pytest.approx(95)
    assert result.take_profit == pytest.approx(112)
    assert result.reward_risk_ratio == pytest.approx(2.4)
    assert result.method == "levels"


def test_level_based_levels_reject_bad_direction_or_low_reward() -> None:
    assert (
        level_risk_levels(
            action="BUY",
            entry=100,
            support_levels=[99.9],
            resistance_levels=[100.1],
            buffer_pct=0.3,
        )
        is None
    )


def test_level_based_levels_for_sell_are_directional() -> None:
    result = level_risk_levels(
        action="SELL",
        entry=100,
        support_levels=[88],
        resistance_levels=[105, 110],
        buffer_pct=0,
    )
    assert result is not None
    assert result.stop_loss == pytest.approx(105)
    assert result.take_profit == pytest.approx(88)


def test_detailed_position_size_caps_to_cash_and_lot() -> None:
    result = calculate_position_size(
        deposit=10_000,
        risk_per_trade_pct=1,
        entry=1_000,
        stop_loss=999,
        lot_size=3,
    )
    assert result.units == 9
    assert result.lots == 3
    assert result.position_value == pytest.approx(9_000)
    assert result.actual_risk == pytest.approx(9)
    assert result.capped_by_cash is True


@pytest.mark.parametrize(
    ("direction", "entry", "exit_price", "expected_gross"),
    [
        ("BUY", 100, 110, 1000),
        ("SELL", 100, 90, 1000),
    ],
)
def test_trade_pnl_is_shared_by_buy_and_sell_simulations(
    direction: str,
    entry: float,
    exit_price: float,
    expected_gross: float,
) -> None:
    result = calculate_trade_pnl(
        direction=direction,
        entry_price=entry,
        exit_price=exit_price,
        units=100,
        commission_pct=0.05,
        actual_risk=500,
    )
    assert result.gross_pnl == expected_gross
    assert result.commission == pytest.approx((entry + exit_price) * 100 * 0.0005)
    assert result.net_pnl == pytest.approx(result.gross_pnl - result.commission)
    assert result.r_multiple == pytest.approx(result.net_pnl / 500)
