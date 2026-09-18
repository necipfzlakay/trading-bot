"""Exit management: four layers, no fixed holding time.

The operator's instruction was explicit — no minimum or maximum trade duration,
but exit timing still matters, so the levels should come out of the data.

That is what the excursion archive is for. Every closed trade records its MAE
and MFE, and ``backtest/excursion.py`` turns that history into stop and trail
multiples. Nothing here is a number someone liked the look of.

The four layers, whichever fires first:

1. **Stop / trailing stop** — protects capital, then protects profit
2. **Regime flip** — the condition that justified the entry has ended
3. **Conviction collapse** — the reasons for the entry have evaporated
4. **Volatility shock** — risk repriced faster than the stop can follow

Edge decay replaces a time limit. Rather than "close after N bars", the measured
survival profile of a setup tightens the trail once its edge has historically
stopped compounding — a limit derived from the setup rather than imposed on it.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from tbot.core.config import ExitConfig
from tbot.core.models import ExitReason, Position, Regime, Side


@dataclass(slots=True)
class ExitDecision:
    should_exit: bool
    reason: ExitReason | None = None
    exit_price: float | None = None
    detail: str = ""
    new_stop: float | None = None


@dataclass(slots=True)
class ExitManager:
    """Evaluates every exit layer for one position on one bar."""

    config: ExitConfig
    # Bars after which a setup's edge has historically flattened, measured by
    # the excursion study. Absent entry means no decay adjustment.
    edge_decay_bars: dict[str, int] | None = None

    def evaluate(
        self,
        position: Position,
        high: float,
        low: float,
        close: float,
        atr: float,
        entry_atr: float,
        regime: Regime,
        conviction_now: float | None,
        collapse_threshold: float,
    ) -> ExitDecision:
        """Check all layers in priority order and return the first that fires."""
        # 1. The stop is a resting instruction: if the bar traded through it,
        #    the exit happened at the stop, not at the close.
        if position.side is Side.BUY and low <= position.current_stop:
            reason = (
                ExitReason.INITIAL_STOP
                if position.current_stop <= position.initial_stop
                else ExitReason.TRAILING_STOP
            )
            return ExitDecision(
                True,
                reason,
                position.current_stop,
                f"low {low:.6f} through stop {position.current_stop:.6f}",
            )
        if position.side is Side.SELL and high >= position.current_stop:
            reason = (
                ExitReason.INITIAL_STOP
                if position.current_stop >= position.initial_stop
                else ExitReason.TRAILING_STOP
            )
            return ExitDecision(
                True,
                reason,
                position.current_stop,
                f"high {high:.6f} through stop {position.current_stop:.6f}",
            )

        # 2. A volatility shock reprices risk faster than a trailing stop can
        #    follow. Waiting for the stop here means taking the gap.
        if entry_atr > 0 and atr / entry_atr >= self.config.vol_shock_atr_ratio:
            return ExitDecision(
                True,
                ExitReason.VOLATILITY_SHOCK,
                close,
                f"ATR {atr / entry_atr:.1f}x its level at entry",
            )

        # 3. The regime that justified the entry has ended.
        if regime in (Regime.TREND_DOWN, Regime.HIGH_VOL) and position.side is Side.BUY:
            return ExitDecision(
                True,
                ExitReason.REGIME_FLIP,
                close,
                f"regime now {regime.value}",
            )

        # 4. The entry reasons are gone even though price has not yet moved
        #    against us. Exiting here is what stops a thesis-less position from
        #    drifting into a full stop-out.
        if conviction_now is not None and conviction_now < collapse_threshold:
            return ExitDecision(
                True,
                ExitReason.CONVICTION_COLLAPSE,
                close,
                f"conviction {conviction_now:.0f} below {collapse_threshold:.0f}",
            )

        return ExitDecision(False, new_stop=self.update_stop(position, close, atr))

    def update_stop(self, position: Position, close: float, atr: float) -> float | None:
        """Ratchet the trailing stop. Never loosens it.

        A stop that can move away from price is not a stop; it is a way of
        rationalising a losing position one bar at a time.
        """
        if atr <= 0 or np.isnan(atr):
            return None

        multiplier = self.config.trailing_atr_mult

        # Edge decay stands in for a time limit: once a setup's measured edge
        # has flattened, hold the position on a tighter leash rather than
        # closing it on a schedule that ignores what price is doing.
        if self.config.edge_decay_enabled and self.edge_decay_bars:
            decay_at = self.edge_decay_bars.get(position.setup)
            if decay_at and position.bars_held >= decay_at:
                multiplier *= self.config.edge_decay_tighten_factor

        if position.side is Side.BUY:
            candidate = close - multiplier * atr
            # Move to breakeven once the trade has paid for itself.
            if self.config.breakeven_at_r > 0:
                r_now = position.unrealized_r(close)
                if r_now >= self.config.breakeven_at_r:
                    candidate = max(candidate, position.entry_price)
            new_stop = max(position.current_stop, candidate)
        else:
            candidate = close + multiplier * atr
            if self.config.breakeven_at_r > 0:
                r_now = position.unrealized_r(close)
                if r_now >= self.config.breakeven_at_r:
                    candidate = min(candidate, position.entry_price)
            new_stop = min(position.current_stop, candidate)

        return new_stop if new_stop != position.current_stop else None

    def initial_stop(self, entry_price: float, atr: float, side: Side) -> float:
        """Stop for a new position, in ATR units.

        The multiple is calibrated by ``scripts/exit_optimizer.py`` from the MAE
        distribution of past winners: the tightest stop that would not have cut
        them short.
        """
        distance = self.config.initial_stop_atr_mult * atr
        return entry_price - distance if side is Side.BUY else entry_price + distance
