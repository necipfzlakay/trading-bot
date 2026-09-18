"""Exit management: layered, with no fixed holding time.

The operator's instruction was explicit — no minimum or maximum trade duration,
but exit timing still matters, so the levels should come out of the data.

That is what the excursion archive is for. Every closed trade records its MAE
and MFE, and ``backtest/excursion.py`` turns that history into stop and trail
multiples. Nothing here is a number someone liked the look of.

The layers, whichever fires first:

1. **Stop / trailing stop** — protects capital, then protects profit
2. **Profit target** — optional, off unless a level has been pre-registered
3. **Regime flip** — the condition that justified the entry has ended
4. **Conviction collapse** — the reasons for the entry have evaporated
5. **Volatility shock** — risk repriced faster than the stop can follow

Layers 1 and 2 are *price-level* checks: they ask only whether price reached a
number that was known at the last close. That makes them safe to run between
bars, which is what ``evaluate_intrabar`` does. Layers 3-5 read indicators, and
indicators may only be computed on closed bars (invariant 1), so they run once
per bar in ``evaluate``.

Both entry points call the same private check per layer. Writing a second copy
of "did price go through the stop" for the intra-bar path is the single most
likely way for live and backtest to quietly disagree.

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
    """Evaluates the exit layers for one position."""

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
        breach = self._stop_breach(position, high, low)
        if breach is not None:
            return breach

        # The target is checked *after* the stop, never before. A bar's OHLC
        # cannot say which came first when both were touched, and assuming the
        # good one makes live results look better than the backtest that
        # justified them — a pleasant surprise nobody goes back and audits.
        hit = self._target_hit(position, high, low)
        if hit is not None:
            return hit

        # A volatility shock reprices risk faster than a trailing stop can
        # follow. Waiting for the stop here means taking the gap.
        if entry_atr > 0 and atr / entry_atr >= self.config.vol_shock_atr_ratio:
            return ExitDecision(
                True,
                ExitReason.VOLATILITY_SHOCK,
                close,
                f"ATR {atr / entry_atr:.1f}x its level at entry",
            )

        # The regime that justified the entry has ended.
        if regime in (Regime.TREND_DOWN, Regime.HIGH_VOL) and position.side is Side.BUY:
            return ExitDecision(
                True,
                ExitReason.REGIME_FLIP,
                close,
                f"regime now {regime.value}",
            )

        # The entry reasons are gone even though price has not yet moved
        # against us. Exiting here is what stops a thesis-less position from
        # drifting into a full stop-out.
        # ``collapse_threshold <= 0`` disables the layer outright. Relying on
        # "no score can be below zero" would work by accident; saying so is
        # what lets the config comment be trusted.
        if (
            collapse_threshold > 0
            and conviction_now is not None
            and conviction_now < collapse_threshold
        ):
            return ExitDecision(
                True,
                ExitReason.CONVICTION_COLLAPSE,
                close,
                f"conviction {conviction_now:.0f} below {collapse_threshold:.0f}",
            )

        return ExitDecision(False, new_stop=self.update_stop(position, close, atr))

    def evaluate_intrabar(self, position: Position, high: float, low: float) -> ExitDecision:
        """The price-level layers only, for use between bar closes.

        ``high`` and ``low`` are the extremes seen *since the last bar close*,
        which is exactly what the closed-bar path passes from the forming bar's
        predecessor. Same question, same code, earlier answer.

        Nothing here reads an indicator, so invariant 1 holds: the forming bar
        never reaches a calculation. All this asks is whether price touched a
        number that was already decided.
        """
        breach = self._stop_breach(position, high, low)
        if breach is not None:
            return breach
        hit = self._target_hit(position, high, low)
        if hit is not None:
            return hit
        return ExitDecision(False)

    def _stop_breach(self, position: Position, high: float, low: float) -> ExitDecision | None:
        """The stop is a resting instruction: it executes where it triggers.

        Not at the close. Filling it at the close understates losses on exactly
        the bars that produce them.
        """
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
        return None

    def _target_hit(self, position: Position, high: float, low: float) -> ExitDecision | None:
        target = self.target_price(position)
        if target is None:
            return None
        if position.side is Side.BUY and high >= target:
            return ExitDecision(
                True, ExitReason.TAKE_PROFIT, target, f"high {high:.6f} reached target {target:.6f}"
            )
        if position.side is Side.SELL and low <= target:
            return ExitDecision(
                True, ExitReason.TAKE_PROFIT, target, f"low {low:.6f} reached target {target:.6f}"
            )
        return None

    def target_price(self, position: Position) -> float | None:
        """Fixed profit target, or None when no level has been configured.

        Measured against the ATR *at entry*, not the current one. A target that
        drifts with volatility is not a target — it is a second opinion arriving
        after the position is already open.

        This is deliberately not a venue order. On Bybit spot a resting stop
        locks the base asset, so a reduce-only take-profit alongside it is
        refused outright (retCode 170131, measured on the demo account). The
        engine therefore owns the target and the venue owns the stop.
        """
        multiple = self.config.target_atr_mult
        if multiple <= 0:
            return None
        entry_atr = float(position.meta.get("entry_atr", 0.0) or 0.0)
        if entry_atr <= 0 or np.isnan(entry_atr):
            return None
        distance = multiple * entry_atr
        return (
            position.entry_price + distance
            if position.side is Side.BUY
            else position.entry_price - distance
        )

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
