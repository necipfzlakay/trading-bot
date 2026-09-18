"""Technical indicators, implemented here rather than imported.

Three reasons this is hand-written instead of pulling in a library:

1. ``pandas-ta`` is publicly flagged as having unsustainable maintenance. A
   silent behaviour change across versions in a trading system does not raise an
   exception — it changes which trades get taken, which is far worse.
2. Every function must be verifiably causal. A single centered window or a
   ``shift(-1)`` anywhere is lookahead that a backtest will happily monetise.
3. Each one is tested against known reference values, so "the indicator is
   wrong" stops being a candidate explanation when results look odd.

**Causality contract:** every function returns a series where index *i* depends
only on input values at indices <= *i*. Warm-up periods return NaN rather than a
partially-formed value; the scoring layer treats NaN as "no opinion", never as
zero.

Wilder's original smoothing (RSI, ATR, ADX) is ``ewm(alpha=1/period)``, not a
simple moving average — the two give visibly different values and most tutorial
implementations get this wrong.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------
# Moving averages
# --------------------------------------------------------------------------


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period, min_periods=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential MA. ``adjust=False`` gives the recursive form traders expect."""
    out = series.ewm(span=period, adjust=False, min_periods=period).mean()
    return out


def wilder_ma(series: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothing: alpha = 1/period, seeded with a simple average.

    Two details that most implementations get wrong, both of which change the
    numbers a strategy trades on:

    * alpha is ``1/period``, not the ``2/(period+1)`` of a standard EMA
    * the recursion is seeded with the simple average of the first ``period``
      values, not with the first value alone

    Skipping the seed converges eventually but differs for a long time at the
    start of every series — and every backtest starts at the start. Charting
    platforms use the seeded form, so without it our RSI would disagree with
    every chart the numbers get compared against.
    """
    values = series.to_numpy(dtype="float64")
    out = np.full(values.shape, np.nan)

    valid = np.flatnonzero(~np.isnan(values))
    if valid.size < period:
        return pd.Series(out, index=series.index)

    seed_pos = int(valid[period - 1])
    seeded = np.full(values.shape, np.nan)
    seeded[seed_pos] = float(np.mean(values[valid[:period]]))
    seeded[seed_pos + 1 :] = values[seed_pos + 1 :]

    # ewm(adjust=False) starts from the first non-NaN observation, which is now
    # the simple average we just placed — exactly Wilder's initialisation.
    return pd.Series(seeded, index=series.index).ewm(alpha=1.0 / period, adjust=False).mean()


def slope(series: pd.Series, period: int) -> pd.Series:
    """Least-squares slope over a trailing window, normalised by price level.

    Expressed as fractional change per bar so it is comparable across symbols
    trading at $0.50 and $70,000.
    """
    x = np.arange(period, dtype="float64")
    x_centered = x - x.mean()
    denom = float((x_centered**2).sum())

    def _fit(window: np.ndarray) -> float:
        if np.isnan(window).any():
            return np.nan
        beta = float((x_centered * (window - window.mean())).sum() / denom)
        level = float(window.mean())
        return beta / level if level else np.nan

    return series.rolling(period, min_periods=period).apply(_fit, raw=True)


# --------------------------------------------------------------------------
# Trend
# --------------------------------------------------------------------------


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    return pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(
        axis=1
    )


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Average True Range — the volatility unit stops and sizing are quoted in."""
    return wilder_ma(true_range(high, low, close), period)


def adx(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Returns (adx, +DI, -DI). ADX measures trend *strength*, not direction.

    Directional movement counts only the dominant side of each bar: an inside
    bar contributes nothing, which is what keeps ADX from rising in chop.
    """
    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=high.index
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=high.index
    )

    atr_ = wilder_ma(true_range(high, low, close), period)
    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di = 100.0 * wilder_ma(plus_dm, period) / atr_
        minus_di = 100.0 * wilder_ma(minus_dm, period) / atr_
        dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di)

    return wilder_ma(dx.replace([np.inf, -np.inf], np.nan), period), plus_di, minus_di


def supertrend(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 10, multiplier: float = 3.0
) -> tuple[pd.Series, pd.Series]:
    """Returns (supertrend line, direction) where direction is +1 long, -1 short.

    The band ratchet is inherently sequential — each bar's band depends on the
    previous bar's band and close — so this one loop is unavoidable.
    """
    atr_ = atr(high, low, close, period)
    hl2 = (high + low) / 2.0
    upper = hl2 + multiplier * atr_
    lower = hl2 - multiplier * atr_

    n = len(close)
    line = np.full(n, np.nan)
    direction = np.full(n, np.nan)

    close_v = close.to_numpy(dtype="float64")
    upper_v = upper.to_numpy(dtype="float64")
    lower_v = lower.to_numpy(dtype="float64")

    final_upper = np.nan
    final_lower = np.nan
    trend = 1

    for i in range(n):
        if np.isnan(upper_v[i]) or np.isnan(lower_v[i]):
            continue
        if np.isnan(final_upper):
            final_upper, final_lower = upper_v[i], lower_v[i]
            trend = 1
        else:
            # Bands only tighten toward price; they widen only on a flip.
            final_upper = (
                min(upper_v[i], final_upper) if close_v[i - 1] <= final_upper else upper_v[i]
            )
            final_lower = (
                max(lower_v[i], final_lower) if close_v[i - 1] >= final_lower else lower_v[i]
            )
            if close_v[i] > final_upper:
                trend = 1
            elif close_v[i] < final_lower:
                trend = -1

        line[i] = final_lower if trend == 1 else final_upper
        direction[i] = trend

    return pd.Series(line, index=close.index), pd.Series(direction, index=close.index)


def donchian(
    high: pd.Series, low: pd.Series, period: int = 20
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Returns (upper, middle, lower), excluding the current bar.

    The shift matters: a channel that includes the current bar can never be
    broken by it, so an unshifted Donchian breakout never fires in a backtest and
    always fires live. Same formula, opposite bug.
    """
    upper = high.rolling(period, min_periods=period).max().shift(1)
    lower = low.rolling(period, min_periods=period).min().shift(1)
    return upper, (upper + lower) / 2.0, lower


def ichimoku(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    tenkan: int = 9,
    kijun: int = 26,
    senkou_b: int = 52,
) -> dict[str, pd.Series]:
    """Ichimoku components, with the cloud shifted *forward* as designed.

    Senkou spans are plotted ``kijun`` bars ahead, so at bar *i* the visible
    cloud was computed from data at *i - kijun*. Shifting forward is therefore
    causal; it is the Chikou span (close shifted backward) that is not usable as
    a signal at the current bar, which is why it is deliberately absent here.
    """

    def _mid(period: int) -> pd.Series:
        return (
            high.rolling(period, min_periods=period).max()
            + low.rolling(period, min_periods=period).min()
        ) / 2.0

    tenkan_sen = _mid(tenkan)
    kijun_sen = _mid(kijun)
    return {
        "tenkan": tenkan_sen,
        "kijun": kijun_sen,
        "senkou_a": ((tenkan_sen + kijun_sen) / 2.0).shift(kijun),
        "senkou_b": _mid(senkou_b).shift(kijun),
    }


# --------------------------------------------------------------------------
# Momentum
# --------------------------------------------------------------------------


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI. Uses ``wilder_ma``, not a simple average of gains/losses."""
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)

    avg_gain = wilder_ma(gain, period)
    avg_loss = wilder_ma(loss, period)

    with np.errstate(divide="ignore", invalid="ignore"):
        rs = avg_gain / avg_loss
    out = 100.0 - (100.0 / (1.0 + rs))
    # All-gain windows give avg_loss == 0 -> rs == inf -> RSI 100, which is
    # correct but arrives as NaN through the division.
    return out.where(avg_loss != 0, 100.0).where(avg_gain.notna(), np.nan)


def macd(
    series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Returns (macd line, signal line, histogram)."""
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return macd_line, signal_line, macd_line - signal_line


def stoch_rsi(
    series: pd.Series, rsi_period: int = 14, stoch_period: int = 14, smooth: int = 3
) -> tuple[pd.Series, pd.Series]:
    """Stochastic of RSI — faster and noisier than RSI, kept for the IC test."""
    r = rsi(series, rsi_period)
    lowest = r.rolling(stoch_period, min_periods=stoch_period).min()
    highest = r.rolling(stoch_period, min_periods=stoch_period).max()
    span = highest - lowest
    raw = ((r - lowest) / span.where(span != 0)).clip(0, 1) * 100.0
    k = raw.rolling(smooth, min_periods=smooth).mean()
    return k, k.rolling(smooth, min_periods=smooth).mean()


def roc(series: pd.Series, period: int = 12) -> pd.Series:
    """Rate of change as a fraction, not a percentage."""
    return series.pct_change(period)


# --------------------------------------------------------------------------
# Volatility
# --------------------------------------------------------------------------


def bollinger(
    series: pd.Series, period: int = 20, std_mult: float = 2.0
) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    """Returns (upper, middle, lower, bandwidth). Bandwidth is the squeeze gauge."""
    middle = sma(series, period)
    # ddof=0: the population form, matching every charting platform.
    sd = series.rolling(period, min_periods=period).std(ddof=0)
    upper = middle + std_mult * sd
    lower = middle - std_mult * sd
    return upper, middle, lower, (upper - lower) / middle.where(middle != 0)


def keltner(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 20,
    atr_period: int = 10,
    multiplier: float = 1.5,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    middle = ema(close, period)
    band = multiplier * atr(high, low, close, atr_period)
    return middle + band, middle, middle - band


def squeeze(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    bb_period: int = 20,
    bb_std: float = 2.0,
    kc_period: int = 20,
    kc_mult: float = 1.5,
) -> pd.Series:
    """True while Bollinger Bands sit inside Keltner Channels.

    Compression, not direction: a squeeze says a move is being stored up, and
    says nothing about which way it will resolve. Used as a setup-quality gate,
    never as an entry signal on its own.
    """
    bb_upper, _, bb_lower, _ = bollinger(close, bb_period, bb_std)
    kc_upper, _, kc_lower = keltner(high, low, close, kc_period, kc_period // 2, kc_mult)
    return (bb_upper < kc_upper) & (bb_lower > kc_lower)


def realized_vol(close: pd.Series, period: int = 20, bars_per_year: int = 8760) -> pd.Series:
    """Annualised realised volatility from log returns.

    ``bars_per_year`` defaults to hourly crypto (24 x 365) because the market
    never closes — using 252 trading days here would understate it badly.
    """
    returns = np.log(close / close.shift(1))
    return returns.rolling(period, min_periods=period).std(ddof=0) * np.sqrt(bars_per_year)


def vol_percentile(close: pd.Series, period: int = 20, lookback: int = 252) -> pd.Series:
    """Where current volatility sits within its own recent history, 0..1.

    A raw volatility number means nothing across symbols; its percentile does.
    """
    rv = realized_vol(close, period)
    return rv.rolling(lookback, min_periods=lookback // 2).rank(pct=True)


# --------------------------------------------------------------------------
# Volume and flow
# --------------------------------------------------------------------------


def obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    """On-Balance Volume: cumulative volume signed by the close-to-close move."""
    direction = np.sign(close.diff().fillna(0.0))
    return (direction * volume).cumsum()


def mfi(
    high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series, period: int = 14
) -> pd.Series:
    """Money Flow Index — RSI weighted by traded value rather than price alone."""
    typical = (high + low + close) / 3.0
    raw_flow = typical * volume
    delta = typical.diff()

    positive = raw_flow.where(delta > 0, 0.0).rolling(period, min_periods=period).sum()
    negative = raw_flow.where(delta < 0, 0.0).rolling(period, min_periods=period).sum()

    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = positive / negative
    out = 100.0 - (100.0 / (1.0 + ratio))
    return out.where(negative != 0, 100.0).where(positive.notna(), np.nan)


def vwap(
    high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series, period: int | None = None
) -> pd.Series:
    """Volume-weighted average price, rolling when ``period`` is given.

    ``period=None`` anchors to the start of the series — use that for an
    anchored VWAP from a specific event, not for a live rolling reference.
    """
    typical = (high + low + close) / 3.0
    if period is None:
        return (typical * volume).cumsum() / volume.cumsum()
    pv = (typical * volume).rolling(period, min_periods=period).sum()
    vol = volume.rolling(period, min_periods=period).sum()
    return pv / vol.where(vol != 0)


def relative_volume(volume: pd.Series, period: int = 20) -> pd.Series:
    """Current volume against its own trailing median. 1.0 = typical bar.

    Median rather than mean: one liquidation cascade should not redefine
    "normal" for the next twenty bars.
    """
    baseline = volume.rolling(period, min_periods=period).median()
    return volume / baseline.where(baseline > 0)


def cvd(taker_buy: pd.Series, taker_sell: pd.Series) -> pd.Series:
    """Cumulative Volume Delta: who is crossing the spread, buyers or sellers.

    Distinct from OBV, which infers aggression from the close. This measures it
    directly from taker flow, so the two are not redundant — the IC and
    redundancy tests decide whether both earn a vote.
    """
    return (taker_buy - taker_sell).cumsum()


# --------------------------------------------------------------------------
# Derivatives — structurally independent of price
# --------------------------------------------------------------------------


def zscore(series: pd.Series, period: int = 90) -> pd.Series:
    """Trailing z-score. The generic way to make any raw series comparable."""
    mean = series.rolling(period, min_periods=period // 2).mean()
    sd = series.rolling(period, min_periods=period // 2).std(ddof=0)
    return (series - mean) / sd.where(sd > 0)


def funding_zscore(funding_rate: pd.Series, period: int = 90) -> pd.Series:
    """Funding rate relative to its own history.

    Persistently high positive funding means longs are crowded and paying to
    stay — a condition that precedes long squeezes. The absolute rate is
    meaningless across symbols and regimes; the z-score is not.
    """
    return zscore(funding_rate, period)


def oi_change(open_interest: pd.Series, period: int = 24) -> pd.Series:
    """Fractional change in open interest over ``period`` bars."""
    return open_interest.pct_change(period)


def oi_price_divergence(close: pd.Series, open_interest: pd.Series, period: int = 24) -> pd.Series:
    """Positions building against price, or unwinding into it.

    Rising price with rising OI is new money joining a move. Rising price with
    falling OI is short covering — the same candle, a different meaning, and
    nothing in the price series can tell them apart.
    """
    price_change = close.pct_change(period)
    oi_delta = open_interest.pct_change(period)
    return np.sign(price_change) * oi_delta


def long_short_extreme(ratio: pd.Series, period: int = 90) -> pd.Series:
    """Crowding z-score from the account long/short ratio."""
    return zscore(ratio, period)


# --------------------------------------------------------------------------
# Normalisation helpers
# --------------------------------------------------------------------------


def normalize_bounded(series: pd.Series, low: float, high: float) -> pd.Series:
    """Map a known-range oscillator (RSI 0..100) onto [-1, 1]."""
    mid = (high + low) / 2.0
    half = (high - low) / 2.0
    return ((series - mid) / half).clip(-1.0, 1.0)


def normalize_zscore(series: pd.Series, period: int = 90, clip: float = 3.0) -> pd.Series:
    """Map an unbounded series onto [-1, 1] via its own trailing distribution."""
    return (zscore(series, period) / clip).clip(-1.0, 1.0)


def normalize_sign(series: pd.Series) -> pd.Series:
    """Map a directional series onto exactly -1, 0 or +1."""
    return pd.Series(np.sign(series.to_numpy(dtype="float64")), index=series.index)


def normalize_ratio(series: pd.Series, neutral: float = 1.0, scale: float = 1.0) -> pd.Series:
    """Map a ratio around a neutral point onto [-1, 1] (relative volume, etc.)."""
    return ((series - neutral) / scale).clip(-1.0, 1.0)
