"""Backtest machinery: alignment, exits, statistics, and the overfitting guards."""

import numpy as np
import pandas as pd
import pytest

from tbot.backtest.align import align_slow_to_fast, project_series
from tbot.backtest.cpcv import make_splits, run_cpcv, walk_forward_splits
from tbot.backtest.dsr import (
    deflated_sharpe,
    expected_max_sharpe,
    probability_of_backtest_overfitting,
)
from tbot.backtest.excursion import analyse, edge_decay, suggest_decay_bars
from tbot.backtest.metrics import buy_and_hold, compute
from tbot.backtest.montecarlo import bootstrap_expectancy, cost_shock, shuffle_trades
from tbot.core.clock import to_ms
from tbot.core.config import ExitConfig
from tbot.core.models import EquityPoint, ExitReason, Position, Regime, Side, Trade
from tbot.strategy.exits import ExitManager

# --------------------------------------------------------------------------
# Multi-timeframe alignment — the quietest lookahead in the system
# --------------------------------------------------------------------------


def test_fast_bar_only_sees_slow_bars_that_have_already_closed():
    # 1h bars from 08:00; 4h bars from 00:00
    fast = pd.Series([to_ms(f"2026-01-01T{h:02d}:00:00") for h in range(8, 20)])
    slow = pd.Series([to_ms(f"2026-01-01T{h:02d}:00:00") for h in (0, 4, 8, 12, 16)])

    mapping = align_slow_to_fast(fast, slow, "1h", "4h")

    # The 1h bar opening 08:00 closes at 09:00. The 4h bar opening 04:00 closed
    # at 08:00 -> visible. The 4h bar opening 08:00 closes at 12:00 -> not.
    assert slow.iloc[mapping[0]] == to_ms("2026-01-01T04:00:00")
    # The 1h bar opening 12:00 closes at 13:00; the 08:00 4h bar closed at 12:00.
    assert slow.iloc[mapping[4]] == to_ms("2026-01-01T08:00:00")


def test_alignment_never_maps_to_a_bar_that_has_not_closed():
    fast = pd.Series([to_ms(f"2026-01-01T{h:02d}:00:00") for h in range(24)])
    slow = pd.Series([to_ms(f"2026-01-01T{h:02d}:00:00") for h in (0, 4, 8, 12, 16, 20)])
    mapping = align_slow_to_fast(fast, slow, "1h", "4h")

    for i, position in enumerate(mapping):
        if position < 0:
            continue
        fast_close = int(fast.iloc[i]) + 3_600_000
        slow_close = int(slow.iloc[position]) + 14_400_000
        assert slow_close <= fast_close, "mapped to a slow bar that had not closed"


def test_alignment_returns_minus_one_before_any_slow_bar_closes():
    fast = pd.Series([to_ms("2026-01-01T00:00:00"), to_ms("2026-01-01T01:00:00")])
    slow = pd.Series([to_ms("2026-01-01T00:00:00")])
    mapping = align_slow_to_fast(fast, slow, "1h", "4h")
    assert (mapping < 0).all()


def test_projection_leaves_nan_where_no_slow_bar_is_available():
    values = pd.Series([10.0, 20.0, 30.0])
    projected = project_series(values, np.array([-1, 0, 2]))
    assert np.isnan(projected[0])
    assert projected[1] == 10.0
    assert projected[2] == 30.0


# --------------------------------------------------------------------------
# Exits
# --------------------------------------------------------------------------


def _position(entry=100.0, stop=90.0, side=Side.BUY) -> Position:
    return Position(
        symbol="BTC/USDT",
        side=side,
        quantity=1.0,
        entry_price=entry,
        entry_ts=0,
        initial_stop=stop,
        current_stop=stop,
        setup="donchian",
        risk_amount=10.0,
    )


def test_stop_fires_when_the_bar_range_covers_it():
    manager = ExitManager(ExitConfig())
    decision = manager.evaluate(
        _position(),
        high=101.0,
        low=89.0,
        close=95.0,
        atr=3.0,
        entry_atr=3.0,
        regime=Regime.TREND_UP,
        conviction_now=None,
        collapse_threshold=30.0,
    )
    assert decision.should_exit
    assert decision.reason is ExitReason.INITIAL_STOP
    assert decision.exit_price == pytest.approx(90.0)  # at the stop, not the close


def test_trailing_stop_ratchets_up_and_never_loosens():
    manager = ExitManager(ExitConfig(trailing_atr_mult=2.0, breakeven_at_r=99.0))
    position = _position()

    first = manager.update_stop(position, close=120.0, atr=3.0)
    assert first == pytest.approx(114.0)
    position.current_stop = first

    # Price falls back: the stop must stay where it was.
    assert manager.update_stop(position, close=105.0, atr=3.0) is None
    assert position.current_stop == pytest.approx(114.0)


def test_stop_moves_to_breakeven_once_the_trade_has_paid_for_itself():
    manager = ExitManager(ExitConfig(trailing_atr_mult=5.0, breakeven_at_r=1.0))
    position = _position()
    new_stop = manager.update_stop(position, close=111.0, atr=3.0)
    assert new_stop == pytest.approx(100.0)  # entry price


def test_volatility_shock_exits_before_the_stop_is_reached():
    manager = ExitManager(ExitConfig(vol_shock_atr_ratio=2.5))
    decision = manager.evaluate(
        _position(),
        high=105.0,
        low=99.0,
        close=103.0,
        atr=10.0,
        entry_atr=3.0,
        regime=Regime.TREND_UP,
        conviction_now=None,
        collapse_threshold=30.0,
    )
    assert decision.should_exit
    assert decision.reason is ExitReason.VOLATILITY_SHOCK


def test_regime_flip_closes_a_long():
    manager = ExitManager(ExitConfig())
    decision = manager.evaluate(
        _position(),
        high=105.0,
        low=99.0,
        close=103.0,
        atr=3.0,
        entry_atr=3.0,
        regime=Regime.TREND_DOWN,
        conviction_now=None,
        collapse_threshold=30.0,
    )
    assert decision.should_exit
    assert decision.reason is ExitReason.REGIME_FLIP


def test_conviction_collapse_exits_before_price_moves_against_us():
    manager = ExitManager(ExitConfig())
    decision = manager.evaluate(
        _position(),
        high=105.0,
        low=99.0,
        close=103.0,
        atr=3.0,
        entry_atr=3.0,
        regime=Regime.TREND_UP,
        conviction_now=10.0,
        collapse_threshold=30.0,
    )
    assert decision.should_exit
    assert decision.reason is ExitReason.CONVICTION_COLLAPSE


def test_edge_decay_tightens_the_trail_instead_of_closing_on_a_schedule():
    manager = ExitManager(
        ExitConfig(trailing_atr_mult=3.0, edge_decay_tighten_factor=0.5, breakeven_at_r=99.0),
        edge_decay_bars={"donchian": 10},
    )
    young, old = _position(), _position()
    old.bars_held = 20

    assert manager.update_stop(young, close=120.0, atr=3.0) == pytest.approx(111.0)
    assert manager.update_stop(old, close=120.0, atr=3.0) == pytest.approx(115.5)


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------


def _trade(r: float, pnl: float, mfe: float = 1.0, mae: float = -0.5, bars: int = 10) -> Trade:
    return Trade(
        symbol="BTC/USDT",
        side=Side.BUY,
        setup="s",
        entry_ts=0,
        exit_ts=bars * 3_600_000,
        entry_price=100.0,
        exit_price=100.0 + pnl,
        quantity=1.0,
        pnl=pnl,
        fees=0.2,
        r_multiple=r,
        mfe_r=mfe,
        mae_r=mae,
        bars_held=bars,
        exit_reason=ExitReason.TRAILING_STOP,
        regime_at_entry=Regime.TREND_UP,
    )


def test_metrics_on_an_empty_run_do_not_crash():
    stats = compute([], [], 100.0)
    assert stats.trades == 0
    assert stats.max_drawdown == 0


def test_drawdown_measures_depth_from_the_running_peak():
    curve = [
        EquityPoint(ts=i, equity=e, cash=e, unrealized=0, open_positions=0, drawdown=0)
        for i, e in enumerate([100, 120, 90, 110])
    ]
    stats = compute([_trade(1.0, 10.0)], curve, 100.0, "1d")
    assert stats.max_drawdown == pytest.approx(0.25)  # 120 -> 90


def test_fee_drag_reports_the_share_of_gross_profit_lost_to_costs():
    trades = [_trade(1.0, 10.0), _trade(-1.0, -5.0)]
    stats = compute(trades, [], 100.0)
    assert stats.fee_drag == pytest.approx(0.4 / 10.0)


def test_buy_and_hold_benchmark_includes_entry_and_exit_fees():
    prices = pd.Series([100.0, 150.0, 200.0])
    result = buy_and_hold(prices, 100.0, fee=0.001)
    assert result["total_return"] < 1.0  # under the naive 2x, because of fees
    assert result["total_return"] > 0.99


# --------------------------------------------------------------------------
# Overfitting guards
# --------------------------------------------------------------------------


def test_cpcv_splits_never_overlap_and_apply_purging():
    splits = list(make_splits(1000, n_blocks=10, n_test_blocks=2, label_horizon=30))
    assert splits
    for split in splits:
        assert not set(split.train_indices) & set(split.test_indices)
    assert any(s.purged > 0 for s in splits)
    assert any(s.embargoed > 0 for s in splits)


def test_cpcv_produces_many_paths_not_one():
    result = run_cpcv(600, lambda _tr, te: float(len(te)), n_blocks=8, n_test_blocks=2)
    assert result.n_splits > 10  # walk-forward would give a handful


def test_walk_forward_splits_move_forward_in_time():
    splits = list(walk_forward_splits(1000, train_size=400, test_size=100))
    assert splits
    for train, test in splits:
        assert train.max() < test.min()


def test_deflated_sharpe_rejects_noise():
    noise = np.random.default_rng(0).normal(0, 0.02, 800)
    assert not deflated_sharpe(noise, trials=1, periods_per_year=365).is_significant


def test_deflated_sharpe_accepts_a_real_edge_tested_once():
    edge = np.random.default_rng(0).normal(0.0018, 0.02, 800)
    assert deflated_sharpe(edge, trials=1, periods_per_year=365).is_significant


def test_many_trials_penalise_the_same_result():
    """Ten trials producing a Sharpe of 1.8 is not the same claim as one trial."""
    edge = np.random.default_rng(0).normal(0.0018, 0.02, 800)
    once = deflated_sharpe(edge, trials=1, periods_per_year=365)
    many = deflated_sharpe(edge, trials=500, periods_per_year=365)
    assert once.sharpe == pytest.approx(many.sharpe)  # same observed Sharpe
    assert many.p_value > once.p_value  # but far weaker evidence
    assert not many.is_significant


def test_expected_max_sharpe_grows_with_the_number_of_trials():
    assert expected_max_sharpe(1) == 0.0
    assert expected_max_sharpe(10) < expected_max_sharpe(100) < expected_max_sharpe(1000)


def test_pbo_flags_selection_on_pure_noise():
    noise = np.random.default_rng(4).normal(0, 1, (400, 25))
    result = probability_of_backtest_overfitting(noise)
    assert result.pbo > 0.4
    assert "OVERFIT" in result.verdict or "FRAGILE" in result.verdict


# --------------------------------------------------------------------------
# Monte Carlo and excursion
# --------------------------------------------------------------------------


def test_shuffling_trades_reveals_drawdown_the_single_path_hid():
    trades = [_trade(1.0, 5.0) for _ in range(20)] + [_trade(-1.0, -5.0) for _ in range(20)]
    result = shuffle_trades(trades, 100.0, simulations=300)
    assert result.worst_max_drawdown >= result.median_max_drawdown
    assert 0 <= result.ruin_probability <= 1


def test_cost_shock_shows_how_much_slippage_the_edge_can_absorb():
    trades = [_trade(1.0, 0.5) for _ in range(10)]
    survives = cost_shock(trades, 100.0, extra_bps=1)
    breaks = cost_shock(trades, 100.0, extra_bps=500)
    assert survives["survives"]
    assert not breaks["survives"]


def test_bootstrap_interval_is_honest_about_small_samples():
    trades = [_trade(0.1, 0.1) for _ in range(6)]
    result = bootstrap_expectancy(trades, simulations=200)
    assert result["samples"] == 6
    assert result["ci_low"] <= result["expectancy_r"] <= result["ci_high"]


def test_excursion_suggests_a_tighter_stop_when_winners_never_needed_the_room():
    # Winners only ever went -0.3R against; a 1R stop is far wider than needed.
    trades = [_trade(2.0, 20.0, mfe=2.5, mae=-0.3) for _ in range(15)]
    trades += [_trade(-1.0, -10.0, mfe=0.2, mae=-1.0) for _ in range(5)]
    stats = analyse(trades)
    assert stats is not None
    assert stats.suggested_stop_r < 1.0
    assert stats.stop_too_wide_by > 1.0


def test_capture_ratio_detects_exiting_too_early():
    trades = [_trade(1.2, 12.0, mfe=2.5) for _ in range(20)]
    stats = analyse(trades)
    assert stats.capture_ratio == pytest.approx(1.2 / 2.5, rel=1e-3)


def test_edge_decay_buckets_returns_by_holding_time():
    trades = [_trade(0.5, 5.0, bars=b) for b in range(5, 60)]
    table = edge_decay(trades)
    assert not table.empty
    assert {"trades", "avg_r", "median_bars"} <= set(table.columns)


def test_decay_bars_need_enough_history_before_suggesting_anything():
    assert suggest_decay_bars([_trade(1.0, 1.0) for _ in range(5)]) == {}
