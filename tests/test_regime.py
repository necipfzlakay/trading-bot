"""Regime classification: no holes, no whipsaw, no lookahead."""

import numpy as np
import pandas as pd
import pytest

from tbot.core.models import Regime
from tbot.strategy.regime import (
    classify_latest,
    classify_series,
    regime_allows,
    regime_stats,
)


def _frame(close: np.ndarray, spread: float = 0.01) -> pd.DataFrame:
    close = pd.Series(close)
    return pd.DataFrame(
        {
            "open": close.shift(1).fillna(close.iloc[0]),
            "high": close * (1 + spread),
            "low": close * (1 - spread),
            "close": close,
            "volume": pd.Series(np.full(len(close), 1000.0)),
        }
    )


def test_steady_uptrend_is_labelled_trend_up():
    # Constant percentage growth. A linear ramp would have *decreasing* percent
    # moves, which is not what a steady trend looks like in a log-return world.
    df = _frame(100 * np.exp(np.linspace(0, 1.4, 400)))
    assert classify_latest(df) is Regime.TREND_UP


def test_steady_downtrend_is_labelled_trend_down():
    # Constant percentage decline. A linear 400 -> 100 fall accelerates in
    # percentage terms, so realised volatility rises and the classifier
    # correctly calls the tail HIGH_VOL rather than a steady trend.
    df = _frame(400 * np.exp(np.linspace(0, -1.4, 400)))
    assert classify_latest(df) is Regime.TREND_DOWN


def test_sideways_chop_is_labelled_range():
    rng = np.random.default_rng(7)
    df = _frame(100 + rng.normal(0, 0.5, 400))
    assert classify_latest(df) in (Regime.RANGE, Regime.HIGH_VOL)


def test_classification_has_no_holes_after_warmup():
    """Every warmed-up bar gets a label.

    Strong ADX with a flat slope used to fall through every branch and stay
    UNKNOWN, silently excluding a third of history from per-regime measurement.
    """
    rng = np.random.default_rng(3)
    close = 100 * np.exp(np.cumsum(rng.normal(0.0005, 0.02, 600)))
    labels = classify_series(_frame(close))
    after_warmup = labels.iloc[250:]
    assert (after_warmup != Regime.UNKNOWN.value).all()


def test_hysteresis_suppresses_single_bar_flips():
    rng = np.random.default_rng(11)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, 600)))
    df = _frame(close)
    jumpy = classify_series(df, hysteresis_bars=1)
    smooth = classify_series(df, hysteresis_bars=3)
    assert (smooth != smooth.shift()).sum() < (jumpy != jumpy.shift()).sum()


def test_classification_is_causal():
    """Truncating the series must not change earlier labels."""
    rng = np.random.default_rng(5)
    close = 100 * np.exp(np.cumsum(rng.normal(0.0003, 0.018, 700)))
    df = _frame(close)
    cut = 500
    full = classify_series(df).iloc[:cut]
    truncated = classify_series(df.iloc[:cut].copy())
    assert full.iloc[-50:].tolist() == truncated.iloc[-50:].tolist()


def test_high_volatility_overrides_a_directional_read():
    calm = np.full(300, 100.0) + np.random.default_rng(2).normal(0, 0.1, 300)
    panic = 100 * np.exp(np.cumsum(np.random.default_rng(2).normal(0.01, 0.12, 60)))
    labels = classify_series(_frame(np.concatenate([calm, panic])))
    assert Regime.HIGH_VOL.value in labels.iloc[-40:].tolist()


def test_permission_layer_is_long_only_on_spot():
    assert regime_allows(Regime.TREND_UP, "buy") is True
    assert regime_allows(Regime.TREND_UP, "sell") is False
    assert regime_allows(Regime.RANGE, "buy") is True
    assert regime_allows(Regime.TREND_DOWN, "buy") is False
    assert regime_allows(Regime.HIGH_VOL, "buy") is False
    assert regime_allows(Regime.UNKNOWN, "buy") is False


def test_regime_stats_shares_sum_to_one():
    rng = np.random.default_rng(13)
    labels = classify_series(_frame(100 * np.exp(np.cumsum(rng.normal(0, 0.02, 500)))))
    stats = regime_stats(labels)
    assert stats["share"].sum() == pytest.approx(1.0, abs=1e-6)
    assert stats["bars"].sum() == len(labels)
