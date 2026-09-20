"""Market regime classification.

The permission layer of the hierarchy. Regime is computed on the slowest
timeframe and gates everything below it: a setup that only works in a trend does
not get scored in a range, and nothing gets scored in a volatility panic.

Why regime comes first rather than being one more indicator: the same RSI
reading means opposite things in a trend and in a range. Treating regime as a
vote among votes averages those meanings together into mush. Treating it as a
gate keeps them separate, and lets the IC test measure each indicator *within*
each regime, where its behaviour is actually stable.

Classification is deliberately simple and hysteretic. A regime detector that
flips every other bar is worse than useless — it makes the strategy trade its
own indecision.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from tbot.core.models import Regime
from tbot.indicators import ta

# Thresholds are starting points measured against history, not tuned constants.
# The IC report shows whether the boundaries separate anything real.
ADX_TREND_MIN = 22.0
VOL_PANIC_PERCENTILE = 0.90
SLOPE_FLAT_BAND = 0.0004  # fractional change per bar that still counts as flat


# Regime parameters are expressed in bars, so the same numbers mean very
# different spans across timeframes. 200 bars is ten months of daily data and
# nearly four years of weekly — longer than the archive, so the classifier would
# never warm up and every bar would come back UNKNOWN. These are the equivalent
# spans, not the same numbers.
REGIME_PERIODS = {
    "1w": {"ema_fast": 13, "ema_slow": 52, "vol_lookback": 52},  # quarter vs year
    "1d": {"ema_fast": 50, "ema_slow": 200, "vol_lookback": 252},
    "4h": {"ema_fast": 50, "ema_slow": 200, "vol_lookback": 252},
    "1h": {"ema_fast": 50, "ema_slow": 200, "vol_lookback": 252},
}


def periods_for(timeframe: str) -> dict[str, int]:
    """Regime parameters scaled to the timeframe's bar length."""
    return REGIME_PERIODS.get(timeframe, REGIME_PERIODS["1d"])


def classify_series(
    df: pd.DataFrame,
    adx_period: int = 14,
    ema_fast: int = 50,
    ema_slow: int = 200,
    vol_lookback: int = 252,
    hysteresis_bars: int = 2,
    timeframe: str | None = None,
) -> pd.Series:
    """Label every bar with a regime. Causal: bar *i* uses only data up to *i*.

    ``hysteresis_bars`` requires a new label to persist before it is accepted,
    which removes the single-bar flip-flopping that would otherwise whipsaw
    every downstream decision.
    """
    if timeframe is not None:
        scaled = periods_for(timeframe)
        ema_fast = scaled["ema_fast"]
        ema_slow = scaled["ema_slow"]
        vol_lookback = scaled["vol_lookback"]

    close = df["close"]
    adx_val, _, _ = ta.adx(df["high"], df["low"], df["close"], adx_period)
    fast = ta.ema(close, ema_fast)
    slow = ta.ema(close, ema_slow)
    slope = ta.slope(close, 20)
    vol_pct = ta.vol_percentile(close, 20, vol_lookback)

    raw = pd.Series(Regime.UNKNOWN.value, index=df.index, dtype="object")

    # Every input must be warm before a bar can be labelled at all. Anything
    # else labels bars from a half-formed EMA, which is worse than not labelling.
    ready = adx_val.notna() & slow.notna() & slope.notna()

    trending = adx_val > ADX_TREND_MIN
    rising = (fast > slow) & (slope > SLOPE_FLAT_BAND)
    falling = (fast < slow) & (slope < -SLOPE_FLAT_BAND)

    # RANGE is the default for any warmed-up bar, so the classification has no
    # holes. Strong ADX with a flat slope, or a fan and a slope that disagree,
    # is a transition — genuinely directionless, not unclassifiable. Leaving
    # those bars UNKNOWN would silently exclude a third of history from the
    # per-regime IC measurement.
    raw[ready] = Regime.RANGE.value
    raw[ready & trending & rising] = Regime.TREND_UP.value
    raw[ready & trending & falling] = Regime.TREND_DOWN.value
    # A volatility panic overrides any directional read: in that state the
    # question is position size, not which way to lean.
    raw[ready & (vol_pct > VOL_PANIC_PERCENTILE)] = Regime.HIGH_VOL.value

    return _apply_hysteresis(raw, hysteresis_bars)


def _apply_hysteresis(labels: pd.Series, bars: int) -> pd.Series:
    """Accept a new label only once it has held for ``bars`` consecutive bars."""
    if bars <= 1:
        return labels

    values = labels.to_numpy(dtype=object)
    out = np.empty(len(values), dtype=object)
    current = Regime.UNKNOWN.value
    candidate = None
    streak = 0

    for i, value in enumerate(values):
        if value == current:
            candidate, streak = None, 0
        elif value == candidate:
            streak += 1
            if streak >= bars:
                current, candidate, streak = value, None, 0
        else:
            candidate, streak = value, 1
        out[i] = current

    return pd.Series(out, index=labels.index, dtype="object")


def classify_latest(df: pd.DataFrame, **kwargs) -> Regime:
    """Regime of the most recent closed bar."""
    series = classify_series(df, **kwargs)
    if series.empty:
        return Regime.UNKNOWN
    return Regime(series.iloc[-1])


def regime_allows(regime: Regime, side: str, allow_short: bool = False) -> bool:
    """Whether the permission layer lets a side be considered at all.

    Long-only by default, and that default is not conservatism — it is what
    spot can express. Without a derivative there is nothing to sell, so in a
    downtrend the correct spot position is cash.

    ``allow_short`` opens the mirror image, and only a perpetual engine passes
    it. The asymmetry that stays is HIGH_VOL and UNKNOWN: a volatility panic is
    not a direction, and neither is an unclassified bar, so no new position is
    opened in either regardless of side.

    Opening TREND_DOWN to shorts is worth naming as a change of scope rather
    than a setting: it is 16.5% of bars that the engine has never traded, and
    the short side is the only measured asymmetry in this universe (base hit
    rate 33.9% against 31.8% long, measured across three years in which the
    market rose). It is pre-registered as H17.
    """
    if regime is Regime.TREND_UP:
        return side == "buy"
    if regime is Regime.TREND_DOWN:
        return allow_short and side == "sell"
    if regime is Regime.RANGE:
        # Both edges of a range are tradeable once shorting is possible.
        return side == "buy" or (allow_short and side == "sell")
    return False  # HIGH_VOL, UNKNOWN


def regime_stats(labels: pd.Series) -> pd.DataFrame:
    """Distribution of regimes — a sanity check on the classifier itself.

    A classifier that labels 95% of history one way has not found structure, it
    has found a constant.
    """
    counts = labels.value_counts()
    total = int(counts.sum())
    rows = []
    for name, count in counts.items():
        rows.append(
            {"regime": name, "bars": int(count), "share": round(count / total, 4) if total else 0.0}
        )
    return pd.DataFrame(rows).sort_values("bars", ascending=False).reset_index(drop=True)
