"""Conviction scoring: selectivity, block counting, and the dynamic threshold.

The behaviours under test are the ones the design depends on: correlated
indicators must not vote twice, the threshold must rise under drawdown without
ever locking the bot out, and every rejection must carry a reason.
"""

import numpy as np
import pytest

from tbot.core.config import ConvictionConfig
from tbot.core.models import NoTradeReason, Regime, Side
from tbot.strategy.conviction import ConvictionEngine, ScoringContext


@pytest.fixture
def config() -> ConvictionConfig:
    return ConvictionConfig()


@pytest.fixture
def engine(config) -> ConvictionEngine:
    # Three trend indicators collapsed into one block, mirroring what the
    # redundancy analysis finds on real data.
    return ConvictionEngine(
        config=config,
        block_lookup={
            "ema_stack": 0,
            "supertrend": 0,
            "price_slope": 0,  # one trend block
            "rsi": 1,
            "stoch_rsi": 1,  # one momentum block
            "macd": 2,
            "adx_di": 3,
            "donchian_pos": 4,
            "obv_slope": 5,
            "mfi": 6,
            "vwap_distance": 7,
            "funding": 8,
            "oi_divergence": 9,
        },
    )


def _ctx(**overrides) -> ScoringContext:
    base = {
        "symbol": "BTC/USDT",
        "bar_ts": 1_700_000_000_000,
        "regime": Regime.TREND_UP,
        "trigger_signals": {},
        "setup_signals": {},
        "permission_signals": {},
        "spread_bps": 2.0,
        "volume_24h": 400_000_000,
        "atr": 1000.0,
        "price": 60_000.0,
    }
    base.update(overrides)
    return ScoringContext(**base)


# --------------------------------------------------------------------------
# Threshold behaviour
# --------------------------------------------------------------------------


def test_threshold_rises_with_drawdown_and_loss_streak(engine, config):
    assert engine.current_threshold() == config.base_threshold
    assert engine.current_threshold(drawdown_bump=10.0) == config.base_threshold + 10.0
    assert (
        engine.current_threshold(drawdown_bump=10.0, loss_streak_bump=2.0)
        == config.base_threshold + 12.0
    )


def test_threshold_never_falls_below_the_floor_however_quiet_the_market(engine, config):
    # "Bored, so traded" must be unreachable: scarcity relief is capped and
    # floored, so no amount of waiting opens the gate.
    engine.bars_since_last_entry = 100_000
    assert engine.current_threshold() >= config.absolute_floor


def test_scarcity_relief_is_bounded(engine, config):
    engine.bars_since_last_entry = config.scarcity_bars * 50
    relaxed = engine.current_threshold()
    assert relaxed >= config.base_threshold - config.scarcity_max_relief
    assert relaxed >= config.absolute_floor


def test_scarcity_counter_resets_on_entry(engine):
    engine.note_bar(entered=False)
    engine.note_bar(entered=False)
    assert engine.bars_since_last_entry == 2
    engine.note_bar(entered=True)
    assert engine.bars_since_last_entry == 0


# --------------------------------------------------------------------------
# Blocking conditions — every rejection carries a reason
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"stale": True}, NoTradeReason.STALE_DATA),
        ({"already_open": True}, NoTradeReason.ALREADY_IN_POSITION),
        ({"portfolio_full": True}, NoTradeReason.MAX_POSITIONS),
        ({"llm_veto": True}, NoTradeReason.LLM_EVENT_VETO),
        ({"regime": Regime.TREND_DOWN}, NoTradeReason.REGIME_VETO),
        ({"regime": Regime.HIGH_VOL}, NoTradeReason.REGIME_VETO),
        ({"correlation_conflict": True}, NoTradeReason.CORRELATION_FULL),
    ],
)
def test_blocking_conditions_are_reported_not_silent(engine, overrides, expected):
    conv = engine.score(_ctx(**overrides), Side.BUY)
    assert conv.no_trade_reason is expected
    assert not conv.passed
    assert conv.total == 0.0


def test_regime_veto_blocks_shorts_on_spot(engine):
    # Spot-only: in a downtrend the correct position is cash, not a short.
    assert engine.score(_ctx(regime=Regime.TREND_UP), Side.SELL).no_trade_reason is (
        NoTradeReason.REGIME_VETO
    )


# --------------------------------------------------------------------------
# The core mechanic: blocks vote, indicators do not
# --------------------------------------------------------------------------


def test_correlated_indicators_count_as_one_vote(engine):
    """Three indicators in one block must not become three confirmations."""
    one_block = _ctx(setup_signals={"ema_stack": 0.9, "supertrend": 0.9, "price_slope": 0.9})
    three_blocks = _ctx(setup_signals={"ema_stack": 0.9, "rsi": 0.9, "macd": 0.9})

    assert engine._count_blocks(one_block, Side.BUY, set(engine.block_lookup)) == 1
    assert engine._count_blocks(three_blocks, Side.BUY, set(engine.block_lookup)) == 3


def test_a_single_loud_block_cannot_carry_a_trade(engine, config):
    """Score alone is not enough — independent evidence is required too."""
    ctx = _ctx(
        setup_signals={"ema_stack": 1.0, "supertrend": 1.0, "price_slope": 1.0},
        permission_signals={"ema_stack": 1.0},
        trigger_signals={"ema_stack": 1.0},
    )
    conv = engine.score(ctx, Side.BUY)
    assert conv.blocks_voting == 1
    assert conv.blocks_voting < config.min_voting_blocks
    assert conv.no_trade_reason is NoTradeReason.BELOW_THRESHOLD


def test_broad_agreement_across_blocks_scores_higher_than_one_cluster(engine):
    narrow = engine.score(
        _ctx(setup_signals={"ema_stack": 0.9, "supertrend": 0.9, "price_slope": 0.9}), Side.BUY
    )
    broad = engine.score(
        _ctx(
            setup_signals={
                "ema_stack": 0.9,
                "rsi": 0.9,
                "macd": 0.9,
                "adx_di": 0.9,
                "donchian_pos": 0.9,
                "obv_slope": 0.9,
            }
        ),
        Side.BUY,
    )
    assert broad.total > narrow.total
    assert broad.blocks_voting > narrow.blocks_voting


def test_opposing_blocks_reduce_confluence(engine):
    agreeing = engine.score(
        _ctx(setup_signals={"ema_stack": 0.9, "rsi": 0.9, "macd": 0.9, "adx_di": 0.9}), Side.BUY
    )
    conflicted = engine.score(
        _ctx(setup_signals={"ema_stack": 0.9, "rsi": -0.9, "macd": 0.9, "adx_di": -0.9}), Side.BUY
    )
    assert conflicted.total < agreeing.total


def test_weak_signals_do_not_vote(engine):
    ctx = _ctx(setup_signals={"ema_stack": 0.05, "rsi": 0.08, "macd": 0.1})
    assert engine._count_blocks(ctx, Side.BUY, set(engine.block_lookup)) == 0


def test_nan_signals_are_ignored_not_treated_as_neutral(engine):
    """A warming-up indicator has no opinion; it must not be read as zero."""
    ctx = _ctx(setup_signals={"ema_stack": np.nan, "rsi": 0.9, "macd": 0.9})
    assert engine._count_blocks(ctx, Side.BUY, set(engine.block_lookup)) == 2


# --------------------------------------------------------------------------
# Components
# --------------------------------------------------------------------------


def test_regime_fit_favours_a_trend_over_a_range(engine):
    trend = engine.score(_ctx(regime=Regime.TREND_UP, setup_signals={"rsi": 0.8}), Side.BUY)
    ranging = engine.score(_ctx(regime=Regime.RANGE, setup_signals={"rsi": 0.8}), Side.BUY)
    trend_fit = next(c for c in trend.components if c.name == "regime_fit")
    range_fit = next(c for c in ranging.components if c.name == "regime_fit")
    assert trend_fit.score > range_fit.score


def test_higher_timeframes_carry_more_weight_in_alignment(engine):
    slow_agrees = engine.score(
        _ctx(
            setup_signals={"rsi": 0.5, "macd": 0.5, "adx_di": 0.5},
            permission_signals={"rsi": 0.9},
            trigger_signals={"rsi": -0.9},
        ),
        Side.BUY,
    )
    fast_agrees = engine.score(
        _ctx(
            setup_signals={"rsi": 0.5, "macd": 0.5, "adx_di": 0.5},
            permission_signals={"rsi": -0.9},
            trigger_signals={"rsi": 0.9},
        ),
        Side.BUY,
    )
    slow_mtf = next(c for c in slow_agrees.components if c.name == "mtf_alignment")
    fast_mtf = next(c for c in fast_agrees.components if c.name == "mtf_alignment")
    assert slow_mtf.score > fast_mtf.score


def test_wide_spread_is_penalised(engine):
    tight = engine.score(_ctx(spread_bps=2.0, setup_signals={"rsi": 0.9, "macd": 0.9}), Side.BUY)
    wide = engine.score(_ctx(spread_bps=40.0, setup_signals={"rsi": 0.9, "macd": 0.9}), Side.BUY)
    assert wide.total < tight.total
    assert any(c.name == "penalties" and c.score < 0 for c in wide.components)


def test_expected_r_is_neutral_without_history(engine):
    conv = engine.score(_ctx(setup_signals={"rsi": 0.9}), Side.BUY)
    component = next(c for c in conv.components if c.name == "expected_r")
    assert component.score == pytest.approx(component.max_score * 0.5)


def test_expected_r_rewards_better_historical_reward_to_risk(engine):
    poor = engine.score(_ctx(expected_r=0.6, setup_signals={"rsi": 0.9}), Side.BUY)
    good = engine.score(_ctx(expected_r=3.0, setup_signals={"rsi": 0.9}), Side.BUY)
    poor_r = next(c for c in poor.components if c.name == "expected_r")
    good_r = next(c for c in good.components if c.name == "expected_r")
    assert good_r.score > poor_r.score


def test_score_stays_within_bounds_under_extreme_input(engine):
    conv = engine.score(
        _ctx(
            setup_signals=dict.fromkeys(engine.block_lookup, 1.0),
            permission_signals=dict.fromkeys(engine.block_lookup, 1.0),
            trigger_signals=dict.fromkeys(engine.block_lookup, 1.0),
            expected_r=10.0,
        ),
        Side.BUY,
    )
    assert 0.0 <= conv.total <= 100.0


def test_ic_selection_excludes_indicators_from_scoring(config):
    """Only indicators that cleared the IC bar for this regime may vote."""
    engine = ConvictionEngine(
        config=config,
        block_lookup={"ema_stack": 0, "rsi": 1, "macd": 2},
        selected_by_regime={"trend_up": ["ema_stack"]},  # rsi and macd failed IC
    )
    ctx = _ctx(setup_signals={"ema_stack": 0.9, "rsi": 0.9, "macd": 0.9})
    assert engine._count_blocks(ctx, Side.BUY, engine._allowed_indicators(Regime.TREND_UP)) == 1


def test_components_retain_their_signals_for_later_explanation(engine):
    conv = engine.score(_ctx(setup_signals={"ema_stack": 0.9, "rsi": 0.8}), Side.BUY)
    confluence = next(c for c in conv.components if c.name == "indicator_confluence")
    assert {s.name for s in confluence.signals} == {"ema_stack", "rsi"}


# --------------------------------------------------------------------------
# Audit #78: the last unassigned rejection reasons, and the last dead setting
# --------------------------------------------------------------------------


def test_a_book_too_wide_to_trade_blocks_before_scoring(engine):
    """``SPREAD_TOO_WIDE`` existed as an enum member and nothing ever set it.

    It survived from the era when no microstructure data was collected at all
    (audit #44). So did ``universe.max_spread_bps``, which no code read — the
    exact shape invariant 50 exists to prevent, except one level up: not a
    setting silently reverting to a default, a setting silently deciding
    nothing.
    """
    blocked = engine.score(
        _ctx(spread_bps=30.0, max_spread_bps=8.0), side=Side.BUY
    )
    assert blocked.no_trade_reason is NoTradeReason.SPREAD_TOO_WIDE
    assert blocked.total == 0.0  # nothing was computed, invariant 48

    allowed = engine.score(
        _ctx(spread_bps=2.0, max_spread_bps=8.0), side=Side.BUY
    )
    assert allowed.no_trade_reason is not NoTradeReason.SPREAD_TOO_WIDE


def test_a_book_too_thin_to_trade_blocks_before_scoring(engine):
    blocked = engine.score(
        _ctx(volume_24h=1_000_000, min_volume_24h=5_000_000), side=Side.BUY
    )
    assert blocked.no_trade_reason is NoTradeReason.LIQUIDITY_TOO_LOW

    allowed = engine.score(
        _ctx(volume_24h=40_000_000, min_volume_24h=5_000_000), side=Side.BUY
    )
    assert allowed.no_trade_reason is not NoTradeReason.LIQUIDITY_TOO_LOW


def test_a_zero_floor_checks_nothing(engine):
    """Which is what keeps every existing caller behaving exactly as before.

    The engine only supplies the floors when ``microstructure_penalties`` is
    on. Every backtest, and every context built without them, passes zero — and
    a zero floor must not reject the whole universe.
    """
    result = engine.score(
        _ctx(spread_bps=500.0, volume_24h=1.0), side=Side.BUY
    )
    assert result.no_trade_reason not in (
        NoTradeReason.SPREAD_TOO_WIDE,
        NoTradeReason.LIQUIDITY_TOO_LOW,
    )


def test_a_thin_book_is_no_opinion_not_a_dead_thesis():
    """Both reasons short-circuit before scoring, so they belong in the set.

    The conviction-collapse exit reads a position's current score to ask "has
    this thesis died?". A ``total`` of 0.0 that was never computed would read
    as the loudest possible yes. That is invariant 10, and audit #47 is what it
    costs to get the membership of this set wrong.
    """
    from tbot.strategy.conviction import UNSCORED_REASONS

    assert NoTradeReason.SPREAD_TOO_WIDE in UNSCORED_REASONS
    assert NoTradeReason.LIQUIDITY_TOO_LOW in UNSCORED_REASONS
    assert NoTradeReason.BELOW_THRESHOLD not in UNSCORED_REASONS
