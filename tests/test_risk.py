"""Risk layer: continuous de-risking, sizing that never exceeds intent, and the
leverage gate that the code cannot open on its own."""

import numpy as np
import pytest

from tbot.core.config import RiskConfig
from tbot.core.models import ExitReason, MarketSpec, NoTradeReason, Regime, Side, Trade
from tbot.risk.budget import RiskBudget, RollingPerformance
from tbot.risk.leverage import LeverageGate, PromotionCriteria
from tbot.risk.sizing import correlation_conflict, size_position, volatility_scale


def _trade(r: float, pnl: float | None = None) -> Trade:
    return Trade(
        symbol="BTC/USDT",
        side=Side.BUY,
        setup="s",
        entry_ts=0,
        exit_ts=1,
        entry_price=100.0,
        exit_price=100.0 + r,
        quantity=1.0,
        pnl=pnl if pnl is not None else r,
        fees=0.1,
        r_multiple=r,
        mfe_r=max(r, 0),
        mae_r=min(r, 0),
        bars_held=10,
        exit_reason=ExitReason.TRAILING_STOP,
        regime_at_entry=Regime.TREND_UP,
    )


# --------------------------------------------------------------------------
# The continuous ladder
# --------------------------------------------------------------------------


def test_risk_multiplier_steps_down_as_drawdown_deepens():
    budget = RiskBudget(RiskConfig())
    budget.update_equity(100.0)
    assert budget.risk_multiplier == 1.0

    budget.update_equity(93.0)  # -7%
    assert budget.risk_multiplier == 0.75
    budget.update_equity(88.0)  # -12%
    assert budget.risk_multiplier == 0.50
    budget.update_equity(80.0)  # -20%
    assert budget.risk_multiplier == 0.25


def test_bot_never_stops_trading_on_losses():
    """The core design promise: it shrinks and gets pickier, it never locks out."""
    budget = RiskBudget(RiskConfig())
    budget.update_equity(100.0)
    budget.update_equity(20.0)  # catastrophic 80% drawdown
    assert budget.risk_multiplier > 0.0
    assert budget.risk_amount(20.0) > 0.0
    assert not budget.state.degraded  # losses are not a reason to degrade


def test_threshold_rises_with_drawdown():
    budget = RiskBudget(RiskConfig())
    budget.update_equity(100.0)
    assert budget.threshold_bump() == 0.0
    budget.update_equity(88.0)
    assert budget.threshold_bump() >= 10.0


def test_loss_streak_raises_the_bar_rather_than_banning_entries():
    config = RiskConfig()
    budget = RiskBudget(config)
    budget.update_equity(100.0)

    for _ in range(config.loss_streak_start + 2):
        budget.record_trade(_trade(-1.0))

    assert budget.threshold_bump() > 0.0
    assert budget.risk_multiplier > 0.0  # still sized to trade
    assert budget.state.consecutive_losses == config.loss_streak_start + 2


def test_win_resets_the_loss_streak():
    budget = RiskBudget(RiskConfig())
    budget.update_equity(100.0)
    for _ in range(4):
        budget.record_trade(_trade(-1.0))
    budget.record_trade(_trade(2.0))
    assert budget.state.consecutive_losses == 0


def test_recovery_needs_hysteresis_not_just_touching_the_edge():
    """Equity resting on a tier boundary must not flip position size every bar."""
    config = RiskConfig(tier_recovery_buffer=0.02)
    budget = RiskBudget(config)
    budget.update_equity(100.0)
    budget.update_equity(92.0)  # -8% -> tier 1
    assert budget.risk_multiplier == 0.75

    budget.update_equity(95.2)  # -4.8%, inside the buffer of the 5% edge
    assert budget.risk_multiplier == 0.75  # not yet restored

    budget.update_equity(98.0)  # -2%, clear of the buffer
    assert budget.risk_multiplier == 1.0


def test_deepening_drawdown_takes_effect_immediately():
    """Protection does not wait for confirmation, unlike recovery."""
    budget = RiskBudget(RiskConfig(tier_recovery_buffer=0.05))
    budget.update_equity(100.0)
    budget.update_equity(94.9)
    assert budget.risk_multiplier == 0.75


def test_degraded_mode_is_for_failures_not_losses():
    budget = RiskBudget(RiskConfig())
    budget.degrade("exchange unreachable")
    assert budget.state.degraded
    budget.recover()
    assert not budget.state.degraded


def test_rolling_performance_tracks_trades_not_days():
    window = RollingPerformance(window=3)
    for r in (1.0, -1.0, 2.0, -1.0):
        window.add(r)
    assert window.count == 3  # oldest dropped
    assert window.expectancy == pytest.approx((-1.0 + 2.0 - 1.0) / 3)
    assert window.profit_factor == pytest.approx(2.0 / 2.0)


# --------------------------------------------------------------------------
# Sizing
# --------------------------------------------------------------------------


SPEC = MarketSpec(
    symbol="BTC/USDT",
    base="BTC",
    quote="USDT",
    min_notional=5.0,
    min_amount=1e-6,
    amount_step=1e-6,
    price_step=0.1,
    maker_fee=0.001,
    taker_fee=0.001,
)


def test_size_is_derived_from_the_stop_not_chosen_first():
    result = size_position(
        equity=1000.0,
        risk_amount=10.0,
        entry_price=100.0,
        stop_price=90.0,
        spec=SPEC,
        config=RiskConfig(),
    )
    assert result.ok
    # 10 quote at risk over a 10-point stop = 1 unit
    assert result.quantity == pytest.approx(1.0, rel=1e-3)
    assert result.risk_amount == pytest.approx(10.0, rel=1e-2)


def test_sizing_rejects_rather_than_rounding_up_past_the_minimum():
    """Bumping size to clear a venue minimum would multiply the intended risk."""
    result = size_position(
        equity=100.0,
        risk_amount=0.75,
        entry_price=100.0,
        stop_price=80.0,
        spec=SPEC,
        config=RiskConfig(),
    )
    assert not result.ok
    assert result.rejected is NoTradeReason.MIN_NOTIONAL
    assert "risk-correct size is too small" in result.detail


def test_sizing_rejects_when_cash_is_short():
    result = size_position(
        equity=1000.0,
        risk_amount=100.0,
        entry_price=100.0,
        stop_price=99.0,
        spec=SPEC,
        config=RiskConfig(),
        available_cash=50.0,
    )
    assert result.rejected is NoTradeReason.INSUFFICIENT_BALANCE


def test_zero_stop_distance_is_rejected():
    result = size_position(
        equity=1000.0,
        risk_amount=10.0,
        entry_price=100.0,
        stop_price=100.0,
        spec=SPEC,
        config=RiskConfig(),
    )
    assert not result.ok


def test_quantity_rounds_down_so_rounding_never_adds_risk():
    spec = SPEC.replace(amount_step=0.1, min_notional=0.0)
    result = size_position(
        equity=1000.0,
        risk_amount=10.0,
        entry_price=100.0,
        stop_price=93.0,
        spec=spec,
        config=RiskConfig(),
    )
    # 10/7 = 1.428... must floor to 1.4, never 1.5
    assert result.quantity == pytest.approx(1.4)


def test_portfolio_exposure_cap_is_enforced():
    result = size_position(
        equity=100.0,
        risk_amount=50.0,
        entry_price=100.0,
        stop_price=99.9,
        spec=SPEC.replace(min_notional=0.0),
        config=RiskConfig(max_portfolio_exposure=1.0),
    )
    assert result.notional <= 100.0 * 1.0 + 1e-6


def test_volatility_scaling_is_bounded_in_both_directions():
    calm = np.full(50, 0.0001)
    wild = np.random.default_rng(3).normal(0, 0.15, 50)
    config = RiskConfig()

    calm_scale = volatility_scale(
        calm, config.vol_target_annual, 8760, config.vol_scale_min, config.vol_scale_max
    )
    wild_scale = volatility_scale(
        wild, config.vol_target_annual, 8760, config.vol_scale_min, config.vol_scale_max
    )

    assert calm_scale == pytest.approx(config.vol_scale_max)  # capped, not unbounded
    assert wild_scale == pytest.approx(config.vol_scale_min)
    assert calm_scale > wild_scale


def test_correlation_conflict_catches_holding_one_bet_five_times():
    correlations = {("BTC/USDT", "ETH/USDT"): 0.93, ("BTC/USDT", "XAUT/USDT"): 0.10}
    assert correlation_conflict("ETH/USDT", ["BTC/USDT"], correlations, 0.85)
    assert not correlation_conflict("XAUT/USDT", ["BTC/USDT"], correlations, 0.85)
    assert not correlation_conflict("ETH/USDT", [], correlations, 0.85)


# --------------------------------------------------------------------------
# Leverage gate
# --------------------------------------------------------------------------


def test_leverage_defaults_to_one_without_a_promotion_record(tmp_path):
    gate = LeverageGate(tmp_path / "promotion.json")
    assert gate.allowed_leverage(2.0) == 1.0  # absence is not a pass


def test_leverage_gate_refuses_a_short_track_record(tmp_path):
    gate = LeverageGate(tmp_path / "promotion.json", PromotionCriteria())
    status = gate.evaluate(
        first_trade_ts=0,
        trades=10,
        profit_factor=2.0,
        expectancy_r=0.5,
        max_drawdown=0.05,
        oos_validated=True,
        user_ceiling=2.0,
        now=30 * 86_400_000,
    )
    assert not status.eligible
    assert status.granted_leverage == 1.0
    assert any("days" in f for f in status.failures)


def test_leverage_gate_grants_only_up_to_the_operator_ceiling(tmp_path):
    gate = LeverageGate(tmp_path / "promotion.json")
    status = gate.evaluate(
        first_trade_ts=0,
        trades=150,
        profit_factor=1.6,
        expectancy_r=0.25,
        max_drawdown=0.09,
        oos_validated=True,
        user_ceiling=1.5,
        now=200 * 86_400_000,
    )
    assert status.eligible
    assert status.granted_leverage == 1.5  # not 2.0 — the ceiling is the operator's
    assert gate.allowed_leverage(1.5) == 1.5


def test_passing_the_gate_cannot_raise_leverage_above_a_one_x_ceiling(tmp_path):
    gate = LeverageGate(tmp_path / "promotion.json")
    gate.evaluate(
        first_trade_ts=0,
        trades=500,
        profit_factor=3.0,
        expectancy_r=1.0,
        max_drawdown=0.01,
        oos_validated=True,
        user_ceiling=2.0,
        now=400 * 86_400_000,
    )
    # Operator has since lowered the ceiling to 1.0: that wins, gate or no gate.
    assert gate.allowed_leverage(1.0) == 1.0


def test_gate_lists_every_reason_it_refused(tmp_path):
    gate = LeverageGate(tmp_path / "promotion.json")
    status = gate.evaluate(
        first_trade_ts=0,
        trades=5,
        profit_factor=0.8,
        expectancy_r=-0.2,
        max_drawdown=0.40,
        oos_validated=False,
        user_ceiling=2.0,
        now=10 * 86_400_000,
    )
    assert len(status.failures) >= 5
