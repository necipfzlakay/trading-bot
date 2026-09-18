"""Entry setups: where a trade is triggered and where its stop goes.

A setup does two things and nothing else — it decides that *now* is a moment
worth considering, and it places the stop. It does not decide whether the trade
is good; that is the conviction engine's job, scoring the candidate against
everything else available. Splitting it this way means adding a setup cannot
accidentally weaken selectivity.

The setups below follow what the tail analysis actually found in three years of
Bybit data: in the top decile of momentum and trend readings, forward returns
over the following weeks beat the base rate by a wide margin, and the effect is
strongest inside an established uptrend. So these are continuation setups with
volatility-based stops, not reversal hunts.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from tbot.core.models import Regime, Side
from tbot.indicators import ta


@dataclass(slots=True, frozen=True)
class SetupCandidate:
    """A triggered setup awaiting scoring."""

    name: str
    side: Side
    entry_price: float
    stop_price: float
    trigger_detail: str
    atr: float

    @property
    def stop_distance(self) -> float:
        return abs(self.entry_price - self.stop_price)


class Setup:
    """Base class. ``detect`` runs on closed bars only, by contract."""

    name = "base"
    allowed_regimes: tuple[Regime, ...] = ()
    # Enough bars for the slowest indicator the setup uses. Kept as an attribute
    # so a weekly or daily setup is not gated on a count chosen for 4h data.
    min_bars = 220

    def detect(self, df: pd.DataFrame, regime: Regime, atr_mult: float) -> SetupCandidate | None:
        raise NotImplementedError

    def _allowed(self, regime: Regime) -> bool:
        return regime in self.allowed_regimes


class DonchianBreakout(Setup):
    """Price closes above the prior N-bar high.

    The channel deliberately excludes the current bar (see ``ta.donchian``). A
    channel that includes the bar breaking it can never be broken in a backtest
    while always breaking live — the same formula producing opposite behaviour
    in test and production.
    """

    name = "donchian_breakout"
    allowed_regimes = (Regime.TREND_UP,)

    def __init__(self, period: int = 20):
        self.period = period

    def detect(self, df: pd.DataFrame, regime: Regime, atr_mult: float) -> SetupCandidate | None:
        if not self._allowed(regime) or len(df) < self.period + 20:
            return None

        upper, _, lower = ta.donchian(df["high"], df["low"], self.period)
        atr_series = ta.atr(df["high"], df["low"], df["close"], 14)

        close = float(df["close"].iloc[-1])
        band = upper.iloc[-1]
        atr_value = atr_series.iloc[-1]
        if np.isnan(band) or np.isnan(atr_value) or atr_value <= 0:
            return None

        prev_close = float(df["close"].iloc[-2])
        # Require the breakout to happen on this bar. Without the prior-bar
        # check the setup re-triggers on every bar that price stays extended.
        if not (close > band and prev_close <= upper.iloc[-2]):
            return None

        stop = close - atr_mult * float(atr_value)
        # A structural stop below the channel floor is often tighter than the
        # ATR stop; take whichever is closer to protect more capital.
        channel_stop = float(lower.iloc[-1])
        stop = max(stop, channel_stop) if channel_stop < close else stop

        return SetupCandidate(
            name=self.name,
            side=Side.BUY,
            entry_price=close,
            stop_price=stop,
            trigger_detail=f"close {close:.4f} > {self.period}-bar high {float(band):.4f}",
            atr=float(atr_value),
        )


class MomentumContinuation(Setup):
    """Sustained strength while the trend is intact.

    This is the setup the tail analysis points at directly: the top decile of
    trend/momentum readings, taken inside an uptrend, is where the measured
    excess return lives. It intentionally does not wait for a pullback — the
    data says extension persists rather than reverting at these horizons.
    """

    name = "momentum_continuation"
    allowed_regimes = (Regime.TREND_UP,)

    def __init__(self, adx_min: float = 25.0, rsi_min: float = 58.0):
        self.adx_min = adx_min
        self.rsi_min = rsi_min

    def detect(self, df: pd.DataFrame, regime: Regime, atr_mult: float) -> SetupCandidate | None:
        if not self._allowed(regime) or len(df) < self.min_bars:
            return None

        adx_value, plus_di, minus_di = ta.adx(df["high"], df["low"], df["close"], 14)
        rsi_value = ta.rsi(df["close"], 14)
        ema50 = ta.ema(df["close"], 50)
        atr_series = ta.atr(df["high"], df["low"], df["close"], 14)

        close = float(df["close"].iloc[-1])
        checks = {
            "adx": adx_value.iloc[-1] >= self.adx_min,
            "di": plus_di.iloc[-1] > minus_di.iloc[-1],
            "rsi": rsi_value.iloc[-1] >= self.rsi_min,
            "above_ema50": close > ema50.iloc[-1],
        }
        if not all(bool(v) for v in checks.values()):
            return None

        atr_value = float(atr_series.iloc[-1])
        if np.isnan(atr_value) or atr_value <= 0:
            return None

        return SetupCandidate(
            name=self.name,
            side=Side.BUY,
            entry_price=close,
            stop_price=close - atr_mult * atr_value,
            trigger_detail=(
                f"ADX {float(adx_value.iloc[-1]):.0f} RSI {float(rsi_value.iloc[-1]):.0f} "
                f"above EMA50"
            ),
            atr=atr_value,
        )


class RangeDipBuy(Setup):
    """Deep short-term oversold inside a range, bought at the lower band.

    Small and tightly stopped by design. Range trading has thinner edge than
    trend continuation in the measured data, so it exists to keep the bot
    participating during the ~55% of bars that are directionless — not to carry
    the strategy.
    """

    name = "range_dip"
    allowed_regimes = (Regime.RANGE,)

    def __init__(self, rsi_period: int = 2, rsi_max: float = 8.0):
        self.rsi_period = rsi_period
        self.rsi_max = rsi_max

    def detect(self, df: pd.DataFrame, regime: Regime, atr_mult: float) -> SetupCandidate | None:
        if not self._allowed(regime) or len(df) < self.min_bars:
            return None

        fast_rsi = ta.rsi(df["close"], self.rsi_period)
        _, _, lower, _ = ta.bollinger(df["close"], 20, 2.0)
        ema200 = ta.ema(df["close"], 200)
        atr_series = ta.atr(df["high"], df["low"], df["close"], 14)

        close = float(df["close"].iloc[-1])
        # Never buy dips below the 200 EMA: in a structural downtrend every dip
        # looks like this one and the range assumption has already failed.
        if close < ema200.iloc[-1]:
            return None
        if not (fast_rsi.iloc[-1] <= self.rsi_max and close <= lower.iloc[-1] * 1.01):
            return None

        atr_value = float(atr_series.iloc[-1])
        if np.isnan(atr_value) or atr_value <= 0:
            return None

        return SetupCandidate(
            name=self.name,
            side=Side.BUY,
            entry_price=close,
            stop_price=close - atr_mult * 0.75 * atr_value,  # tighter: lower conviction
            trigger_detail=f"RSI({self.rsi_period}) {float(fast_rsi.iloc[-1]):.1f} at lower band",
            atr=atr_value,
        )


DEFAULT_SETUPS: tuple[Setup, ...] = (
    MomentumContinuation(),
    DonchianBreakout(),
    RangeDipBuy(),
)


def detect_all(
    df: pd.DataFrame, regime: Regime, atr_mult: float, setups=DEFAULT_SETUPS
) -> list[SetupCandidate]:
    """Every setup triggering on this bar, in registration order."""
    found = []
    for setup in setups:
        candidate = setup.detect(df, regime, atr_mult)
        if candidate is not None and candidate.stop_distance > 0:
            found.append(candidate)
    return found
