import pytest

from app.risk import atr_risk_levels, position_size


def test_atr_levels_for_buy_keep_two_to_one_ratio() -> None:
    result = atr_risk_levels(
        action="BUY", entry=100, atr=2, stop_multiplier=1.5, take_multiplier=3
    )
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
        atr_risk_levels(
            action="BUY", entry=100, atr=2, stop_multiplier=2, take_multiplier=3
        )


def test_position_size_respects_lot() -> None:
    assert position_size(
        deposit=100_000, risk_per_trade_pct=1, entry=100, stop_loss=95, lot_size=10
    ) == 200

