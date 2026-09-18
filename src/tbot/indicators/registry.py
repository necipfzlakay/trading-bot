"""Indicator registry: raw calculations turned into comparable signals.

Every indicator here answers one of two different questions, and conflating them
is a common way to build a scoring system that quietly makes no sense:

* **DIRECTIONAL** — "which way?" Produces a signal in [-1, +1] where the sign is
  a directional opinion and the magnitude is conviction. These vote.
* **GATE** — "is this a tradeable condition at all?" Bollinger bandwidth, a
  squeeze or a volatility percentile say nothing about direction. They scale or
  block a setup; they never vote on side.

Normalising everything onto [-1, +1] is what makes the redundancy analysis
possible: RSI, ADX and a funding rate cannot be correlated against each other in
their native units, but their normalised signals can.

Contrarian indicators (funding, long/short crowding) carry an explicit sign
flip, declared in the spec rather than buried in the calculation, so the
direction of the assumption stays visible and testable.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

import numpy as np
import pandas as pd

from tbot.core.models import IndicatorCategory
from tbot.indicators import ta


class SignalRole(StrEnum):
    DIRECTIONAL = "directional"
    GATE = "gate"


@dataclass(slots=True, frozen=True)
class IndicatorSpec:
    """One indicator: how to compute it, how to read it, and what it needs."""

    name: str
    category: IndicatorCategory
    role: SignalRole
    compute: Callable[[pd.DataFrame], pd.Series]
    description: str
    params: dict[str, float] = field(default_factory=dict)
    # Columns beyond OHLCV. A spec asking for a column the archive lacks is
    # skipped rather than silently producing zeros.
    requires: tuple[str, ...] = ()
    # Declared here, not hidden inside compute(), so the assumption is visible
    # and the IC test can confirm or refute it.
    contrarian: bool = False
    min_bars: int = 100


# --------------------------------------------------------------------------
# Trend
# --------------------------------------------------------------------------


def _ema_stack(df: pd.DataFrame) -> pd.Series:
    """Alignment of the 20/50/200 EMA fan, plus where price sits in it.

    Full marks require the fan ordered *and* price leading it — a stack that
    price has already fallen back through is a trend in the process of failing.
    """
    close = df["close"]
    e20, e50, e200 = ta.ema(close, 20), ta.ema(close, 50), ta.ema(close, 200)
    bull = (e20 > e50).astype(float) + (e50 > e200).astype(float) + (close > e20).astype(float)
    bear = (e20 < e50).astype(float) + (e50 < e200).astype(float) + (close < e20).astype(float)
    out = (bull - bear) / 3.0
    return out.where(e200.notna())


def _adx_directional(df: pd.DataFrame) -> pd.Series:
    """ADX gives strength, DI gives direction; only together are they a signal.

    ADX alone rises in strong downtrends too, so using it unsigned would score a
    collapse as a buy.
    """
    adx_val, plus_di, minus_di = ta.adx(df["high"], df["low"], df["close"], 14)
    strength = (adx_val / 50.0).clip(0.0, 1.0)
    direction = np.sign(plus_di - minus_di)
    return (strength * direction).where(adx_val.notna())


def _supertrend_dir(df: pd.DataFrame) -> pd.Series:
    _, direction = ta.supertrend(df["high"], df["low"], df["close"], 10, 3.0)
    return direction


def _donchian_position(df: pd.DataFrame) -> pd.Series:
    """Where price sits in the prior channel; >1 means a genuine breakout."""
    upper, middle, lower = ta.donchian(df["high"], df["low"], 20)
    span = (upper - lower).replace(0, np.nan)
    return (2.0 * (df["close"] - middle) / span).clip(-1.5, 1.5).clip(-1.0, 1.0)


def _ichimoku_signal(df: pd.DataFrame) -> pd.Series:
    """Price against the cloud, plus the tenkan/kijun relationship."""
    ich = ta.ichimoku(df["high"], df["low"], df["close"])
    cloud_top = pd.concat([ich["senkou_a"], ich["senkou_b"]], axis=1).max(axis=1)
    cloud_bottom = pd.concat([ich["senkou_a"], ich["senkou_b"]], axis=1).min(axis=1)
    close = df["close"]

    above = (close > cloud_top).astype(float)
    below = (close < cloud_bottom).astype(float)
    tk = np.sign(ich["tenkan"] - ich["kijun"])
    return ((above - below) * 0.6 + tk * 0.4).where(cloud_top.notna())


def _price_slope(df: pd.DataFrame) -> pd.Series:
    """Trailing regression slope, scaled by the symbol's own volatility."""
    slope = ta.slope(df["close"], 20)
    scale = slope.rolling(120, min_periods=60).std(ddof=0)
    return (slope / scale.where(scale > 0)).clip(-2.0, 2.0) / 2.0


# --------------------------------------------------------------------------
# Momentum
# --------------------------------------------------------------------------


def _rsi_signal(df: pd.DataFrame) -> pd.Series:
    """RSI read as momentum: above 50 bullish.

    Deliberately *not* read as mean reversion here. Which reading is right
    depends on regime, and that is measured by the per-regime IC test rather
    than assumed — if it flips in range conditions, the data will say so.
    """
    return ta.normalize_bounded(ta.rsi(df["close"], 14), 0.0, 100.0)


def _macd_signal(df: pd.DataFrame) -> pd.Series:
    """MACD histogram scaled by ATR so it is comparable across symbols."""
    _, _, hist = ta.macd(df["close"])
    atr_ = ta.atr(df["high"], df["low"], df["close"], 14)
    return (hist / atr_.where(atr_ > 0)).clip(-2.0, 2.0) / 2.0


def _stoch_rsi_signal(df: pd.DataFrame) -> pd.Series:
    k, _ = ta.stoch_rsi(df["close"])
    return ta.normalize_bounded(k, 0.0, 100.0)


def _roc_signal(df: pd.DataFrame) -> pd.Series:
    return ta.normalize_zscore(ta.roc(df["close"], 12), 90)


# --------------------------------------------------------------------------
# Volatility — gates, not votes
# --------------------------------------------------------------------------


def _bb_percent_b(df: pd.DataFrame) -> pd.Series:
    """Position within the Bollinger band: +1 at the upper, -1 at the lower."""
    upper, middle, lower, _ = ta.bollinger(df["close"], 20, 2.0)
    span = (upper - lower).replace(0, np.nan)
    return (2.0 * (df["close"] - middle) / span).clip(-1.0, 1.0)


def _squeeze_gate(df: pd.DataFrame) -> pd.Series:
    """1.0 while compressed. Direction-free by construction."""
    return ta.squeeze(df["high"], df["low"], df["close"]).astype(float)


def _vol_regime_gate(df: pd.DataFrame) -> pd.Series:
    """Volatility percentile mapped to [-1, 1]: -1 dead calm, +1 extreme.

    High readings are a reason to shrink, not a reason to pick a side.
    """
    pct = ta.vol_percentile(df["close"], 20, 252)
    return (pct * 2.0 - 1.0).clip(-1.0, 1.0)


def _bb_width_gate(df: pd.DataFrame) -> pd.Series:
    _, _, _, width = ta.bollinger(df["close"], 20, 2.0)
    return ta.normalize_zscore(width, 90)


# --------------------------------------------------------------------------
# Flow
# --------------------------------------------------------------------------


def _obv_slope(df: pd.DataFrame) -> pd.Series:
    """OBV trend. The level is arbitrary; only its direction carries meaning."""
    return ta.normalize_zscore(ta.slope(ta.obv(df["close"], df["volume"]), 20), 90)


def _mfi_signal(df: pd.DataFrame) -> pd.Series:
    return ta.normalize_bounded(
        ta.mfi(df["high"], df["low"], df["close"], df["volume"], 14), 0.0, 100.0
    )


def _vwap_distance(df: pd.DataFrame) -> pd.Series:
    """Distance from rolling VWAP in ATR units — who is winning, buyers or sellers."""
    vw = ta.vwap(df["high"], df["low"], df["close"], df["volume"], 20)
    atr_ = ta.atr(df["high"], df["low"], df["close"], 14)
    return ((df["close"] - vw) / atr_.where(atr_ > 0)).clip(-2.0, 2.0) / 2.0


def _relative_volume_gate(df: pd.DataFrame) -> pd.Series:
    """Participation. A breakout on thin volume is a different animal entirely."""
    return ta.normalize_ratio(ta.relative_volume(df["volume"], 20), neutral=1.0, scale=2.0)


# --------------------------------------------------------------------------
# Derivatives — not derived from price, so not redundant with any of the above
# --------------------------------------------------------------------------


def _funding_signal(df: pd.DataFrame) -> pd.Series:
    """Funding z-score, read as momentum — measured, not assumed.

    The standard story is contrarian: persistently high funding means longs are
    crowded and paying to stay, which precedes squeezes. That story is wrong at
    these horizons on this data, and consistently so.

    Measured over 21 Bybit symbols (``scripts/polarity.py``), the top funding
    decile beat the base rate at every horizon tested, with bootstrap intervals
    clear of zero — on 1d/20-bar, +5.31% against a +2.28% base rate, CI
    [+2.49%, +7.70%]. High funding marks a trend people are paying to stay in,
    and at multi-week horizons that trend more often continued than snapped.

    Caveat that belongs with the number: the sample is 2023-2026, a broadly
    rising market. A sustained bear phase could restore the contrarian reading,
    which is why ``scripts/polarity.py`` is re-runnable rather than a one-off.
    """
    return ta.normalize_zscore(df["funding_rate"], 90)


def _oi_change_signal(df: pd.DataFrame) -> pd.Series:
    return ta.normalize_zscore(ta.oi_change(df["open_interest"], 24), 90)


def _oi_divergence_signal(df: pd.DataFrame) -> pd.Series:
    """Positions building with a move, or unwinding into it.

    Rising price on rising OI is new money. Rising price on falling OI is short
    covering. Identical candles, opposite meaning — and nothing in the price
    series distinguishes them.
    """
    return ta.normalize_zscore(ta.oi_price_divergence(df["close"], df["open_interest"], 24), 90)


def _long_short_signal(df: pd.DataFrame) -> pd.Series:
    """Crowding in retail positioning, read contrarian."""
    return ta.normalize_zscore(df["long_short_ratio"], 90)


# --------------------------------------------------------------------------
# The registry
# --------------------------------------------------------------------------

REGISTRY: dict[str, IndicatorSpec] = {
    spec.name: spec
    for spec in [
        # -- trend
        IndicatorSpec(
            "ema_stack",
            IndicatorCategory.TREND,
            SignalRole.DIRECTIONAL,
            _ema_stack,
            "20/50/200 EMA fan alignment with price confirmation",
            {"fast": 20, "mid": 50, "slow": 200},
            min_bars=220,
        ),
        IndicatorSpec(
            "adx_di",
            IndicatorCategory.TREND,
            SignalRole.DIRECTIONAL,
            _adx_directional,
            "ADX trend strength signed by the DI spread",
            {"period": 14},
        ),
        IndicatorSpec(
            "supertrend",
            IndicatorCategory.TREND,
            SignalRole.DIRECTIONAL,
            _supertrend_dir,
            "Supertrend regime flag",
            {"period": 10, "multiplier": 3.0},
        ),
        IndicatorSpec(
            "donchian_pos",
            IndicatorCategory.TREND,
            SignalRole.DIRECTIONAL,
            _donchian_position,
            "Position within the prior 20-bar channel",
            {"period": 20},
        ),
        IndicatorSpec(
            "ichimoku",
            IndicatorCategory.TREND,
            SignalRole.DIRECTIONAL,
            _ichimoku_signal,
            "Price against the cloud plus tenkan/kijun",
            {"tenkan": 9, "kijun": 26},
            min_bars=130,
        ),
        IndicatorSpec(
            "price_slope",
            IndicatorCategory.TREND,
            SignalRole.DIRECTIONAL,
            _price_slope,
            "Volatility-scaled regression slope",
            {"period": 20},
            min_bars=180,
        ),
        # -- momentum
        IndicatorSpec(
            "rsi",
            IndicatorCategory.MOMENTUM,
            SignalRole.DIRECTIONAL,
            _rsi_signal,
            "RSI(14) read as momentum",
            {"period": 14},
        ),
        IndicatorSpec(
            "macd",
            IndicatorCategory.MOMENTUM,
            SignalRole.DIRECTIONAL,
            _macd_signal,
            "ATR-scaled MACD histogram",
            {"fast": 12, "slow": 26, "signal": 9},
        ),
        IndicatorSpec(
            "stoch_rsi",
            IndicatorCategory.MOMENTUM,
            SignalRole.DIRECTIONAL,
            _stoch_rsi_signal,
            "Stochastic RSI %K",
            {"rsi": 14, "stoch": 14},
        ),
        IndicatorSpec(
            "roc",
            IndicatorCategory.MOMENTUM,
            SignalRole.DIRECTIONAL,
            _roc_signal,
            "12-bar rate of change, z-scored",
            {"period": 12},
        ),
        # -- volatility (gates)
        IndicatorSpec(
            "bb_percent_b",
            IndicatorCategory.VOLATILITY,
            SignalRole.DIRECTIONAL,
            _bb_percent_b,
            "Position within the Bollinger band",
            {"period": 20, "std": 2.0},
        ),
        IndicatorSpec(
            "squeeze",
            IndicatorCategory.VOLATILITY,
            SignalRole.GATE,
            _squeeze_gate,
            "Bollinger inside Keltner: compression before expansion",
            {"period": 20},
        ),
        IndicatorSpec(
            "vol_regime",
            IndicatorCategory.VOLATILITY,
            SignalRole.GATE,
            _vol_regime_gate,
            "Realised volatility percentile",
            {"period": 20, "lookback": 252},
            min_bars=280,
        ),
        IndicatorSpec(
            "bb_width",
            IndicatorCategory.VOLATILITY,
            SignalRole.GATE,
            _bb_width_gate,
            "Bollinger bandwidth z-score",
            {"period": 20},
        ),
        # -- flow
        IndicatorSpec(
            "obv_slope",
            IndicatorCategory.FLOW,
            SignalRole.DIRECTIONAL,
            _obv_slope,
            "On-balance volume trend",
            {"period": 20},
            min_bars=150,
        ),
        IndicatorSpec(
            "mfi",
            IndicatorCategory.FLOW,
            SignalRole.DIRECTIONAL,
            _mfi_signal,
            "Money Flow Index",
            {"period": 14},
        ),
        IndicatorSpec(
            "vwap_distance",
            IndicatorCategory.FLOW,
            SignalRole.DIRECTIONAL,
            _vwap_distance,
            "Distance from rolling VWAP in ATR units",
            {"period": 20},
        ),
        IndicatorSpec(
            "rel_volume",
            IndicatorCategory.FLOW,
            SignalRole.GATE,
            _relative_volume_gate,
            "Volume against its own trailing median",
            {"period": 20},
        ),
        # -- derivatives
        IndicatorSpec(
            "funding",
            IndicatorCategory.DERIVATIVES,
            SignalRole.DIRECTIONAL,
            _funding_signal,
            "Funding rate z-score (measured as momentum — see _funding_signal)",
            {"period": 90},
            requires=("funding_rate",),
            contrarian=False,
        ),
        IndicatorSpec(
            "oi_change",
            IndicatorCategory.DERIVATIVES,
            SignalRole.DIRECTIONAL,
            _oi_change_signal,
            "Open interest change z-score",
            {"period": 24},
            requires=("open_interest",),
        ),
        IndicatorSpec(
            "oi_divergence",
            IndicatorCategory.DERIVATIVES,
            SignalRole.DIRECTIONAL,
            _oi_divergence_signal,
            "Open interest against price direction",
            {"period": 24},
            requires=("open_interest",),
        ),
        IndicatorSpec(
            "long_short",
            IndicatorCategory.DERIVATIVES,
            SignalRole.DIRECTIONAL,
            _long_short_signal,
            "Account long/short crowding (read contrarian)",
            {"period": 90},
            requires=("long_short_ratio",),
            contrarian=True,
        ),
    ]
}


def available_specs(df: pd.DataFrame, enabled: list[str] | None = None) -> list[IndicatorSpec]:
    """Specs computable from the columns present, optionally filtered by name.

    A spec whose required column is absent is dropped, never defaulted to zero:
    a missing funding series must read as "no opinion", not as "neutral
    funding", or absence starts voting.
    """
    names = enabled or list(REGISTRY)
    out = []
    for name in names:
        spec = REGISTRY.get(name)
        if spec is None:
            raise KeyError(f"unknown indicator {name!r}; known: {sorted(REGISTRY)}")
        if all(col in df.columns for col in spec.requires):
            out.append(spec)
    return out


def compute_signals(
    df: pd.DataFrame, enabled: list[str] | None = None, apply_contrarian: bool = True
) -> pd.DataFrame:
    """Compute every available indicator as a normalised signal series.

    Returns a frame indexed like ``df``, one column per indicator, values in
    [-1, +1] with NaN during warm-up. Contrarian specs are sign-flipped here so
    that a positive value always means "bullish" regardless of the underlying
    indicator's natural polarity.
    """
    out: dict[str, pd.Series] = {}
    for spec in available_specs(df, enabled):
        if len(df) < spec.min_bars:
            continue
        series = spec.compute(df)
        if apply_contrarian and spec.contrarian:
            series = -series
        out[spec.name] = series.clip(-1.0, 1.0).astype("float64")

    return pd.DataFrame(out, index=df.index)


def directional_names(enabled: list[str] | None = None) -> list[str]:
    """Names of indicators that vote on direction (gates excluded)."""
    names = enabled or list(REGISTRY)
    return [n for n in names if REGISTRY[n].role is SignalRole.DIRECTIONAL]


def by_category(enabled: list[str] | None = None) -> dict[IndicatorCategory, list[str]]:
    names = enabled or list(REGISTRY)
    out: dict[IndicatorCategory, list[str]] = {}
    for name in names:
        out.setdefault(REGISTRY[name].category, []).append(name)
    return out
