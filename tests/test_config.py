"""Config must refuse bad input at load time, not surface it as a strange trade."""

import pytest
from pydantic import ValidationError

from tbot.core.config import (
    Config,
    ConvictionConfig,
    DrawdownTier,
    RiskConfig,
    Secrets,
    TimeframeConfig,
    assert_runnable,
    load_config,
)
from tbot.core.models import Mode, Venue


def test_all_shipped_profiles_load():
    for name in ("base", "paper", "demo", "live"):
        cfg = load_config(f"config/{name}.yaml")
        assert cfg.starting_balance > 0


def test_profile_inherits_base_and_overrides_only_its_own_keys():
    base = load_config("config/base.yaml")
    live = load_config("config/live.yaml")
    assert live.venue is Venue.BYBIT_LIVE
    assert live.risk.risk_per_trade < base.risk.risk_per_trade  # halved for first month
    assert live.conviction.base_threshold == base.conviction.base_threshold  # inherited


def test_timeframe_hierarchy_must_be_slowest_to_fastest():
    TimeframeConfig(permission="1d", setup="4h", trigger="1h")
    with pytest.raises(ValidationError, match="slowest-to-fastest"):
        TimeframeConfig(permission="1h", setup="4h", trigger="1d")
    with pytest.raises(ValidationError, match="slowest-to-fastest"):
        TimeframeConfig(permission="4h", setup="4h", trigger="1h")  # no duplicates


def test_drawdown_ladder_must_shrink_monotonically():
    with pytest.raises(ValidationError, match="strictly increasing"):
        RiskConfig(
            drawdown_tiers=[
                DrawdownTier(max_drawdown=0.10, risk_multiplier=1.0, threshold_bump=0.0),
                DrawdownTier(max_drawdown=0.05, risk_multiplier=0.5, threshold_bump=5.0),
            ]
        )
    with pytest.raises(ValidationError, match="must not increase"):
        RiskConfig(
            drawdown_tiers=[
                DrawdownTier(max_drawdown=0.05, risk_multiplier=0.5, threshold_bump=0.0),
                DrawdownTier(max_drawdown=1.00, risk_multiplier=0.9, threshold_bump=5.0),
            ]
        )


def test_drawdown_ladder_must_cover_total_loss():
    with pytest.raises(ValidationError, match="cover up to 1.0"):
        RiskConfig(
            drawdown_tiers=[
                DrawdownTier(max_drawdown=0.15, risk_multiplier=0.5, threshold_bump=0.0)
            ]
        )


def test_risk_multiplier_may_never_reach_zero():
    # The design guarantee: the bot shrinks and gets pickier, it never halts on
    # losses. A zero multiplier would be a silent binary lockout.
    with pytest.raises(ValidationError):
        DrawdownTier(max_drawdown=1.0, risk_multiplier=0.0, threshold_bump=20.0)


def test_shipped_ladder_keeps_trading_at_maximum_drawdown():
    cfg = load_config("config/base.yaml")
    assert cfg.risk.drawdown_tiers[-1].risk_multiplier > 0.0


def test_conviction_weights_must_sum_to_100():
    with pytest.raises(ValidationError, match="sum to 100"):
        ConvictionConfig(component_weights={"regime_fit": 50.0, "mtf_alignment": 30.0})


def test_conviction_floor_cannot_exceed_base_threshold():
    with pytest.raises(ValidationError, match="absolute_floor"):
        ConvictionConfig(base_threshold=60.0, absolute_floor=70.0)


def test_leverage_ceiling_is_capped_at_user_limit():
    RiskConfig(max_leverage=3.0)  # the operator's authorised ceiling
    with pytest.raises(ValidationError):
        RiskConfig(max_leverage=3.5)  # above it, and no flag raises it
    with pytest.raises(ValidationError):
        RiskConfig(max_leverage=0.5)  # sub-1x is not a thing


def test_leverage_defaults_to_one_even_though_the_ceiling_is_higher():
    """Raising the ceiling must not raise the usage. Invariant 8.

    The ceiling moved from 2.0 to 3.0 because the operator authorised it. That
    is a permission, not an instruction, and the two have to stay separable:
    leverage multiplies expectancy without changing its sign, so applying it to
    an unproven strategy just reaches the same answer faster.
    """
    assert RiskConfig().max_leverage == 1.0


def test_the_volatility_window_has_exactly_one_definition():
    """``vol_lookback_bars`` was declared twice in ``RiskConfig``.

    The later declaration wins in a Python class body, so the first one — sat
    among its neighbours with a ``ge=5`` bound and a stale default — was dead:
    an edit to it changed nothing in the running engine. That is the same
    "setting that looks read and is not" class the surrounding ``extra=forbid``
    work exists to end, arriving through a different door. The bound is what
    pins it, because only the dead declaration accepted 5.
    """
    assert RiskConfig().vol_lookback_bars == 240
    with pytest.raises(ValidationError):
        RiskConfig(vol_lookback_bars=5)
    with pytest.raises(ValidationError):
        RiskConfig(vol_lookback_bars=5000)


def test_zero_slippage_backtest_is_rejected():
    with pytest.raises(ValidationError, match="fiction"):
        Config(mode=Mode.BACKTEST, execution={"slippage_bps": 0.0})


def test_collapse_threshold_must_sit_below_entry_floor():
    with pytest.raises(ValidationError, match="collapse_threshold"):
        Config(
            conviction={"base_threshold": 68.0, "absolute_floor": 60.0, "collapse_threshold": 65.0}
        )


def test_live_money_requires_explicit_environment_confirmation(monkeypatch):
    cfg = load_config("config/live.yaml")
    secrets = Secrets(bybit_live_api_key="k", bybit_live_api_secret="s")
    monkeypatch.delenv("TBOT_CONFIRM_LIVE", raising=False)
    with pytest.raises(RuntimeError, match="Refusing to start with REAL money"):
        assert_runnable(cfg, secrets)
    monkeypatch.setenv("TBOT_CONFIRM_LIVE", "yes")
    assert_runnable(cfg, secrets)  # explicit opt-in clears it


def test_live_without_credentials_is_blocked():
    cfg = load_config("config/live.yaml")
    with pytest.raises(RuntimeError, match="requires API credentials"):
        assert_runnable(cfg, Secrets())


def test_paper_mode_needs_no_credentials_and_is_not_live_money():
    cfg = load_config("config/paper.yaml")
    assert_runnable(cfg, Secrets())
    assert cfg.is_live_money() is False


def test_demo_venue_is_not_live_money():
    cfg = load_config("config/demo.yaml")
    assert cfg.mode is Mode.LIVE
    assert cfg.is_live_money() is False  # live mode, virtual funds


def test_llm_enabled_without_key_is_blocked():
    cfg = load_config("config/base.yaml", overrides={"llm": {"enabled": True}})
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        assert_runnable(cfg, Secrets())
