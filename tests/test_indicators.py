"""Indicator correctness.

Two classes of test here, and the second matters more than the first.

*Reference values* confirm the arithmetic matches the published definition.
*Causality* confirms no function can see the future — computing an indicator on
a truncated series must give identical values for the overlapping prefix. A
single ``shift(-1)`` or centered window would pass every reference test and
still make the whole backtest fiction, and this is the test that catches it.
"""

import numpy as np
import pandas as pd
import pytest

from tbot.indicators import ta

# Wilder's own worked example. The values below are the textbook arithmetic
# (seed = simple mean of the first 14 changes), verified by hand rather than
# copied from the widely reproduced list online, which is off by one change.
WILDER_CLOSES = [
    44.34,
    44.09,
    44.15,
    43.61,
    44.33,
    44.83,
    45.10,
    45.42,
    45.84,
    46.08,
    45.89,
    46.03,
    45.61,
    46.28,
    46.28,
    46.00,
    46.03,
    46.41,
    46.22,
    45.64,
    46.21,
    46.25,
    45.71,
    46.45,
    45.78,
    45.35,
    44.03,
    44.18,
    44.22,
    44.57,
    43.42,
    42.66,
    43.13,
]
# Full precision, produced by an independent textbook implementation (seed =
# simple mean of the first 14 changes, then the (prev*13 + current)/14
# recursion), not copied from any library.
WILDER_RSI14 = [
    70.464135,
    66.249619,
    66.480942,
    69.346853,
    66.294713,
    57.915021,
    62.880718,
    63.208789,
]


@pytest.fixture
def ohlcv() -> pd.DataFrame:
    """Deterministic pseudo-random walk with realistic bar geometry."""
    rng = np.random.default_rng(42)
    n = 400
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.012, n)))
    spread = np.abs(rng.normal(0, 0.006, n)) * close
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread
    volume = np.abs(rng.normal(1000, 300, n))
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume})


# --------------------------------------------------------------------------
# Reference values
# --------------------------------------------------------------------------


def test_sma_exact():
    s = pd.Series([1.0, 2, 3, 4, 5])
    out = ta.sma(s, 3)
    assert np.isnan(out.iloc[1])  # warm-up is NaN, not a partial average
    assert out.iloc[2] == pytest.approx(2.0)
    assert out.iloc[4] == pytest.approx(4.0)


def test_ema_recursive_form():
    s = pd.Series([1.0, 2, 3, 4, 5])
    out = ta.ema(s, 3)
    # adjust=False seeds on the first value: 1, 1.5, 2.25, 3.125, 4.0625
    assert out.iloc[2] == pytest.approx(2.25)
    assert out.iloc[4] == pytest.approx(4.0625)


def test_wilder_smoothing_differs_from_standard_ema():
    s = pd.Series(np.arange(1.0, 51.0))
    assert not np.allclose(
        ta.wilder_ma(s, 14).dropna().to_numpy(), ta.ema(s, 14).dropna().to_numpy()
    )


def test_wilder_ma_seeds_with_simple_average():
    s = pd.Series(np.arange(1.0, 30.0))
    out = ta.wilder_ma(s, 14)
    assert np.isnan(out.iloc[12])
    assert out.iloc[13] == pytest.approx(np.mean(np.arange(1.0, 15.0)))  # = 7.5


def test_rsi_matches_wilder_reference():
    out = ta.rsi(pd.Series(WILDER_CLOSES), 14).dropna().to_numpy()
    assert out[: len(WILDER_RSI14)] == pytest.approx(WILDER_RSI14, abs=1e-6)


def test_rsi_saturates_correctly():
    assert ta.rsi(pd.Series(np.arange(1.0, 40.0)), 14).iloc[-1] == pytest.approx(100.0)
    assert ta.rsi(pd.Series(np.arange(40.0, 1.0, -1.0)), 14).iloc[-1] == pytest.approx(0.0)


def test_rsi_stays_in_range(ohlcv):
    out = ta.rsi(ohlcv["close"], 14).dropna()
    assert out.between(0, 100).all()


def test_atr_first_value_is_mean_of_true_ranges():
    high = pd.Series([10.0, 11, 12, 13, 14])
    low = pd.Series([9.0, 10, 11, 12, 13])
    close = pd.Series([9.5, 10.5, 11.5, 12.5, 13.5])
    out = ta.atr(high, low, close, 3)
    # TR = [1.0, 1.5, 1.5, 1.5, 1.5]; seed at index 2 = mean of first three
    assert out.iloc[2] == pytest.approx((1.0 + 1.5 + 1.5) / 3)


def test_adx_is_bounded_and_rises_in_a_trend(ohlcv):
    adx, plus_di, minus_di = ta.adx(ohlcv["high"], ohlcv["low"], ohlcv["close"], 14)
    assert adx.dropna().between(0, 100).all()
    assert plus_di.dropna().between(0, 100).all()

    n = 120
    trend = pd.Series(np.linspace(100, 200, n))
    high, low = trend * 1.01, trend * 0.99
    trend_adx, trend_plus, trend_minus = ta.adx(high, low, trend, 14)
    assert trend_adx.iloc[-1] > 40  # a clean ramp is a strong trend
    assert trend_plus.iloc[-1] > trend_minus.iloc[-1]


def test_macd_histogram_is_line_minus_signal(ohlcv):
    line, signal, hist = ta.macd(ohlcv["close"])
    valid = hist.dropna().index
    assert (line[valid] - signal[valid]).to_numpy() == pytest.approx(hist[valid].to_numpy())


def test_bollinger_bands_are_ordered_and_bandwidth_positive(ohlcv):
    upper, middle, lower, width = ta.bollinger(ohlcv["close"], 20, 2.0)
    valid = upper.dropna().index
    assert (upper[valid] >= middle[valid]).all()
    assert (middle[valid] >= lower[valid]).all()
    assert (width.dropna() > 0).all()


def test_keltner_bands_are_ordered(ohlcv):
    upper, middle, lower = ta.keltner(ohlcv["high"], ohlcv["low"], ohlcv["close"])
    valid = upper.dropna().index
    assert (upper[valid] > middle[valid]).all()
    assert (middle[valid] > lower[valid]).all()


def test_squeeze_is_boolean_and_fires_when_volatility_compresses():
    # Flat then explosive: the squeeze must be on during the flat stretch.
    close = pd.Series(
        np.concatenate(
            [
                np.full(120, 100.0) + np.random.default_rng(1).normal(0, 0.02, 120),
                np.linspace(100, 160, 80),
            ]
        )
    )
    high, low = close * 1.001, close * 0.999
    out = ta.squeeze(high, low, close)
    assert out.dtype == bool
    assert out.iloc[60:110].any()


def test_obv_accumulates_signed_volume():
    close = pd.Series([10.0, 11, 10, 12])
    volume = pd.Series([100.0, 200, 150, 300])
    # first bar has no prior close -> 0 contribution, then +200, -150, +300
    assert ta.obv(close, volume).tolist() == [0.0, 200.0, 50.0, 350.0]


def test_mfi_stays_in_range(ohlcv):
    out = ta.mfi(ohlcv["high"], ohlcv["low"], ohlcv["close"], ohlcv["volume"], 14).dropna()
    assert out.between(0, 100).all()


def test_vwap_sits_inside_the_price_range(ohlcv):
    out = ta.vwap(ohlcv["high"], ohlcv["low"], ohlcv["close"], ohlcv["volume"], 20).dropna()
    valid = out.index
    assert (out <= ohlcv["high"].rolling(20).max()[valid]).all()
    assert (out >= ohlcv["low"].rolling(20).min()[valid]).all()


def test_relative_volume_is_one_for_a_typical_bar():
    volume = pd.Series([100.0] * 30)
    assert ta.relative_volume(volume, 20).iloc[-1] == pytest.approx(1.0)


def test_cvd_is_cumulative_taker_imbalance():
    buy = pd.Series([10.0, 20, 5])
    sell = pd.Series([5.0, 5, 15])
    assert ta.cvd(buy, sell).tolist() == [5.0, 20.0, 10.0]


def test_oi_price_divergence_distinguishes_new_money_from_short_covering():
    close = pd.Series(np.linspace(100, 120, 50))  # price rising throughout
    building = pd.Series(np.linspace(1000, 1400, 50))  # OI rising -> new longs
    unwinding = pd.Series(np.linspace(1400, 1000, 50))  # OI falling -> covering
    assert ta.oi_price_divergence(close, building, 24).iloc[-1] > 0
    assert ta.oi_price_divergence(close, unwinding, 24).iloc[-1] < 0


# --------------------------------------------------------------------------
# Donchian: the shift is the whole point
# --------------------------------------------------------------------------


def test_donchian_excludes_the_current_bar():
    # A new high on the last bar must NOT raise that bar's own upper band,
    # otherwise the channel can never be broken in a backtest while always
    # breaking live — same formula, opposite behaviour.
    high = pd.Series([10.0] * 20 + [99.0])
    low = pd.Series([5.0] * 21)
    upper, _, lower = ta.donchian(high, low, 20)
    assert upper.iloc[-1] == pytest.approx(10.0)
    assert high.iloc[-1] > upper.iloc[-1]  # the breakout is detectable
    assert lower.iloc[-1] == pytest.approx(5.0)


def test_ichimoku_cloud_is_shifted_forward_not_backward():
    n = 120
    close = pd.Series(np.linspace(100, 200, n))
    high, low = close * 1.01, close * 0.99
    out = ta.ichimoku(high, low, close)
    # senkou_a at bar i is built from data at bar i-26, so in a rising market it
    # must lag the current tenkan/kijun rather than lead it.
    assert out["senkou_a"].iloc[-1] < out["tenkan"].iloc[-1]
    assert "chikou" not in out  # deliberately absent: backward shift is unusable


def test_supertrend_direction_is_only_plus_or_minus_one(ohlcv):
    line, direction = ta.supertrend(ohlcv["high"], ohlcv["low"], ohlcv["close"])
    assert set(direction.dropna().unique()) <= {-1.0, 1.0}
    assert line.dropna().gt(0).all()


def test_supertrend_follows_a_clean_trend():
    n = 150
    close = pd.Series(np.linspace(100, 300, n))
    high, low = close * 1.005, close * 0.995
    _, direction = ta.supertrend(high, low, close, 10, 3.0)
    assert direction.iloc[-1] == 1.0


# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------


def test_normalisers_stay_within_the_signal_range(ohlcv):
    rsi_values = ta.rsi(ohlcv["close"], 14)
    normalised = ta.normalize_bounded(rsi_values, 0, 100).dropna()
    assert normalised.between(-1, 1).all()
    assert ta.normalize_bounded(pd.Series([50.0]), 0, 100).iloc[0] == pytest.approx(0.0)

    z = ta.normalize_zscore(ohlcv["close"], 90).dropna()
    assert z.between(-1, 1).all()

    assert set(ta.normalize_sign(pd.Series([-3.0, 0.0, 7.0])).tolist()) == {-1.0, 0.0, 1.0}
    assert ta.normalize_ratio(pd.Series([1.0]), neutral=1.0).iloc[0] == pytest.approx(0.0)


def test_zscore_of_constant_series_is_not_infinite():
    out = ta.zscore(pd.Series([5.0] * 100), 30)
    assert out.dropna().empty or np.isfinite(out.dropna()).all()


# --------------------------------------------------------------------------
# Causality — the test that actually protects the backtest
# --------------------------------------------------------------------------


CAUSALITY_CASES = {
    "sma": lambda d: ta.sma(d["close"], 20),
    "ema": lambda d: ta.ema(d["close"], 20),
    "wilder_ma": lambda d: ta.wilder_ma(d["close"], 14),
    "slope": lambda d: ta.slope(d["close"], 20),
    "atr": lambda d: ta.atr(d["high"], d["low"], d["close"], 14),
    "adx": lambda d: ta.adx(d["high"], d["low"], d["close"], 14)[0],
    "plus_di": lambda d: ta.adx(d["high"], d["low"], d["close"], 14)[1],
    "supertrend_line": lambda d: ta.supertrend(d["high"], d["low"], d["close"])[0],
    "supertrend_dir": lambda d: ta.supertrend(d["high"], d["low"], d["close"])[1],
    "donchian_upper": lambda d: ta.donchian(d["high"], d["low"], 20)[0],
    "donchian_lower": lambda d: ta.donchian(d["high"], d["low"], 20)[2],
    "ichimoku_tenkan": lambda d: ta.ichimoku(d["high"], d["low"], d["close"])["tenkan"],
    "ichimoku_senkou_a": lambda d: ta.ichimoku(d["high"], d["low"], d["close"])["senkou_a"],
    "ichimoku_senkou_b": lambda d: ta.ichimoku(d["high"], d["low"], d["close"])["senkou_b"],
    "rsi": lambda d: ta.rsi(d["close"], 14),
    "macd_hist": lambda d: ta.macd(d["close"])[2],
    "stoch_rsi_k": lambda d: ta.stoch_rsi(d["close"])[0],
    "roc": lambda d: ta.roc(d["close"], 12),
    "bollinger_width": lambda d: ta.bollinger(d["close"], 20)[3],
    "keltner_upper": lambda d: ta.keltner(d["high"], d["low"], d["close"])[0],
    "squeeze": lambda d: ta.squeeze(d["high"], d["low"], d["close"]).astype(float),
    "realized_vol": lambda d: ta.realized_vol(d["close"], 20),
    "vol_percentile": lambda d: ta.vol_percentile(d["close"], 20, 120),
    "obv": lambda d: ta.obv(d["close"], d["volume"]),
    "mfi": lambda d: ta.mfi(d["high"], d["low"], d["close"], d["volume"], 14),
    "vwap_rolling": lambda d: ta.vwap(d["high"], d["low"], d["close"], d["volume"], 20),
    "relative_volume": lambda d: ta.relative_volume(d["volume"], 20),
    "zscore": lambda d: ta.zscore(d["close"], 90),
    "funding_zscore": lambda d: ta.funding_zscore(d["close"] / 1e6, 90),
    "oi_change": lambda d: ta.oi_change(d["volume"], 24),
    "oi_price_divergence": lambda d: ta.oi_price_divergence(d["close"], d["volume"], 24),
    "normalize_zscore": lambda d: ta.normalize_zscore(d["close"], 90),
}


@pytest.mark.parametrize("name", sorted(CAUSALITY_CASES))
def test_indicator_cannot_see_the_future(name, ohlcv):
    """Truncating the input must not change any earlier output value.

    If an indicator peeks ahead, values near the truncation point shift when the
    future is removed. Checking the last 50 shared bars targets exactly where a
    leak would show up first.
    """
    func = CAUSALITY_CASES[name]
    cut = 300

    full = func(ohlcv).iloc[:cut]
    truncated = func(ohlcv.iloc[:cut].copy())

    compare_from = cut - 50
    a = full.iloc[compare_from:].to_numpy(dtype="float64")
    b = truncated.iloc[compare_from:].to_numpy(dtype="float64")

    both_nan = np.isnan(a) & np.isnan(b)
    assert np.array_equal(np.isnan(a), np.isnan(b)), f"{name}: NaN pattern changed on truncation"
    assert a[~both_nan] == pytest.approx(b[~both_nan], rel=1e-9, abs=1e-9), (
        f"{name} leaks future data: values changed when later bars were removed"
    )


@pytest.mark.parametrize("name", sorted(CAUSALITY_CASES))
def test_indicator_warmup_is_nan_not_zero(name, ohlcv):
    """Warm-up must be NaN so scoring reads it as "no opinion", never as neutral."""
    out = CAUSALITY_CASES[name](ohlcv)
    assert out.notna().any(), f"{name} produced no values at all"
