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


def test_the_gate_grants_the_whole_ceiling_the_operator_authorised(tmp_path):
    """``evaluate`` clamped the grant with a hardcoded 2.0.

    That literal was a stale copy of the config bound. When the bound moved to
    3.0, an operator who had explicitly authorised 3x — and then earned it —
    was silently held at 2x, with nothing in the record saying why. This is the
    call site that *persists* the number, so the cap outlived the run that made
    it. ``allowed_leverage`` had the same literal removed; this is its sibling.
    """
    gate = LeverageGate(tmp_path / "promotion.json")
    status = gate.evaluate(
        first_trade_ts=0,
        trades=150,
        profit_factor=1.6,
        expectancy_r=0.25,
        max_drawdown=0.09,
        oos_validated=True,
        user_ceiling=3.0,
        now=200 * 86_400_000,
    )
    assert status.eligible
    assert status.granted_leverage == 3.0
    assert gate.allowed_leverage(3.0) == 3.0


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


# --------------------------------------------------------------------------
# Minimum viable capital
# --------------------------------------------------------------------------


def test_minimum_capital_accounts_for_volatility_scaling():
    """Ignoring the volatility scaler makes the requirement look far too low.

    Volatility targeting shrinks size on volatile symbols — exactly the ones
    with the widest stops. The two effects compound, so the stop distribution
    alone understates what the account has to support.
    """
    from tbot.core.config import load_config
    from tbot.risk.capital import minimum_viable_equity

    cfg = load_config("config/base.yaml")
    stops = [0.05] * 50 + [0.15] * 50
    scales = [1.5] * 50 + [0.4] * 50  # calm symbols scaled up, wild ones down

    stops_only = minimum_viable_equity(cfg, stops, min_notional=5.0)
    with_scales = minimum_viable_equity(cfg, stops, min_notional=5.0, vol_scales=scales)

    assert with_scales.minimum_equity > stops_only.minimum_equity


def test_minimum_capital_scales_inversely_with_risk_fraction():
    """A larger risk fraction reaches wider stops, so the floor drops."""
    from tbot.core.config import load_config
    from tbot.risk.capital import minimum_viable_equity

    stops = [0.03, 0.05, 0.08, 0.12, 0.20]

    low = load_config("config/base.yaml")
    low.risk.risk_per_trade = 0.005
    high = load_config("config/base.yaml")
    high.risk.risk_per_trade = 0.02

    assert (
        minimum_viable_equity(high, stops).minimum_equity
        < minimum_viable_equity(low, stops).minimum_equity
    )


def test_comfortable_capital_exceeds_the_minimum():
    from tbot.core.config import load_config
    from tbot.risk.capital import minimum_viable_equity

    result = minimum_viable_equity(load_config("config/base.yaml"), [0.02, 0.05, 0.09, 0.15, 0.30])
    assert result.comfortable_equity >= result.minimum_equity


def test_minimum_capital_handles_no_observations():
    from tbot.core.config import load_config
    from tbot.risk.capital import minimum_viable_equity

    result = minimum_viable_equity(load_config("config/base.yaml"), [])
    assert result.minimum_equity == 0.0
    assert result.observed_stops == 0


def test_shipped_min_equity_matches_the_measured_requirement():
    """The config floor must not drift away from what the data says.

    The floor exists because below it the bot stops trading a smaller version
    of this strategy and starts trading a quieter subset of the universe, which
    the backtest does not describe (invariant 18). It is set by the exchange's
    $5 minimum against the widest stop the account can still size, so it moves
    **inversely** with risk per trade: bigger positions clear the minimum more
    easily, and the floor falls.

    Asserting the product rather than the two numbers is deliberate. The pair
    was pinned literally before and went stale the moment risk_per_trade
    changed, which tells you the test was guarding the numbers instead of the
    relationship between them. Measured with scripts/capital_check.py:
    0.75% -> 179.23 and 1.2% -> 112.02, and 0.0075 x 179.23 == 0.012 x 112.02.
    """
    from tbot.core.config import load_config

    cfg = load_config("config/base.yaml")
    product = cfg.risk.risk_per_trade * cfg.risk.min_equity
    assert product == pytest.approx(1.344, rel=0.05), (
        f"risk_per_trade={cfg.risk.risk_per_trade} and min_equity={cfg.risk.min_equity} "
        f"no longer agree. Re-run scripts/capital_check.py and update the floor."
    )


def test_starting_balance_is_not_used_as_a_live_constraint():
    """starting_balance is a paper/backtest figure; live reads the venue."""
    import inspect

    from tbot.engine.runner import Runner

    source = inspect.getsource(Runner._equity_now)
    assert "fetch_balance" in source


# --------------------------------------------------------------------------
# Position size ceilings
# --------------------------------------------------------------------------


UNCONSTRAINED = MarketSpec(
    symbol="X/USDT",
    base="X",
    quote="USDT",
    min_notional=5.0,
    min_amount=1e-9,
    amount_step=1e-9,
    price_step=1e-6,
    maker_fee=0.001,
    taker_fee=0.001,
)


def _size(config: RiskConfig, equity: float, stop_pct: float = 0.05):
    return size_position(
        equity=equity,
        risk_amount=equity * config.risk_per_trade,
        entry_price=100.0,
        stop_price=100.0 * (1 - stop_pct),
        spec=UNCONSTRAINED,
        config=config,
    )


def test_percentage_ceiling_binds_on_tight_stops():
    """A tight stop produces a large position even at a small risk fraction."""
    uncapped = _size(RiskConfig(max_position_pct=0.0), equity=10_000, stop_pct=0.01)
    capped = _size(RiskConfig(max_position_pct=0.40), equity=10_000, stop_pct=0.01)

    assert uncapped.notional == pytest.approx(7_500, rel=1e-3)  # 75% of equity
    assert capped.notional == pytest.approx(4_000, rel=1e-3)
    assert capped.capped_by == "max_position_pct"


def test_absolute_notional_ceiling_binds_as_the_account_grows():
    config = RiskConfig(max_position_pct=0.0, max_position_notional=500.0)
    assert _size(config, equity=1_000).notional < 500.0  # not yet binding
    large = _size(config, equity=50_000)
    assert large.notional == pytest.approx(500.0, rel=1e-3)
    assert large.capped_by == "max_position_notional"


def test_risk_amount_ceiling_caps_the_loss_not_the_exposure():
    """The most direct ceiling: it bounds what a stop-out costs."""
    config = RiskConfig(max_position_pct=0.0, max_risk_amount=50.0)
    result = _size(config, equity=100_000, stop_pct=0.05)
    assert result.risk_amount == pytest.approx(50.0, rel=1e-2)
    assert result.capped_by == "max_risk_amount"


def test_tightest_ceiling_wins():
    config = RiskConfig(max_position_pct=0.40, max_position_notional=500.0, max_risk_amount=50.0)
    result = _size(config, equity=50_000, stop_pct=0.05)
    assert result.notional == pytest.approx(500.0, rel=1e-3)
    assert result.capped_by == "max_position_notional"


def test_ceilings_can_only_shrink_never_grow():
    """A ceiling above the natural size must leave the position untouched."""
    natural = _size(RiskConfig(max_position_pct=0.0), equity=1_000)
    generous = _size(
        RiskConfig(max_position_pct=0.0, max_position_notional=1_000_000.0), equity=1_000
    )
    assert generous.notional == pytest.approx(natural.notional)
    assert generous.capped_by == ""


def test_zero_disables_an_individual_ceiling():
    config = RiskConfig(max_position_pct=0.0, max_position_notional=0.0, max_risk_amount=0.0)
    result = _size(config, equity=100_000, stop_pct=0.01)
    # Only the portfolio exposure ceiling remains.
    assert result.capped_by in ("", "portfolio_exposure")


def test_capping_preserves_the_risk_relationship():
    """After capping, risk_amount must still equal quantity x stop distance.

    Otherwise every R figure downstream would be measured in a unit that
    silently changed when a ceiling bound.
    """
    config = RiskConfig(max_position_pct=0.20)
    result = _size(config, equity=10_000, stop_pct=0.01)
    assert result.risk_amount == pytest.approx(result.quantity * result.stop_distance)
    assert result.capped_by == "max_position_pct"


def test_shipped_defaults_do_not_bind_a_small_account():
    """The default ceilings must not silently shrink positions at the minimum balance."""
    from tbot.core.config import load_config

    cfg = load_config("config/base.yaml")
    result = _size(cfg.risk, equity=cfg.risk.min_equity, stop_pct=0.05)
    assert result.capped_by == ""
