"""Continuous risk budgeting — the bot shrinks, it never locks itself out.

A binary cooldown ("four losses, stop for 24 hours") costs opportunity in a
market that trades around the clock, and it measures the wrong thing: what
matters is the expectancy of the last fifty trades, not whether today was red.

So exposure is a continuous function of drawdown instead. Deeper drawdown means
a smaller ``risk_multiplier`` and a higher conviction bar — the bot becomes
smaller and pickier, and keeps trading. The multiplier can never reach zero;
config validation rejects a ladder that would allow it.

Recovery uses hysteresis: climbing back to exactly a tier edge does not restore
size, because equity oscillating on a boundary would otherwise change position
sizing every bar.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from tbot.core.config import RiskConfig
from tbot.core.logging import get_logger
from tbot.core.models import RiskState, Trade

log = get_logger("risk.budget")


@dataclass(slots=True)
class RollingPerformance:
    """Expectancy and profit factor over the last N trades.

    The evaluation unit is trades, not days. "Today was bad" says nothing; "the
    last fifty trades have negative expectancy" says the edge has gone.
    """

    window: int
    r_multiples: list[float] = field(default_factory=list)

    def add(self, r_multiple: float) -> None:
        self.r_multiples.append(r_multiple)
        if len(self.r_multiples) > self.window:
            self.r_multiples.pop(0)

    @property
    def count(self) -> int:
        return len(self.r_multiples)

    @property
    def expectancy(self) -> float:
        """Average R per trade. The single most informative number here."""
        return float(np.mean(self.r_multiples)) if self.r_multiples else 0.0

    @property
    def profit_factor(self) -> float:
        wins = sum(r for r in self.r_multiples if r > 0)
        losses = -sum(r for r in self.r_multiples if r < 0)
        if losses <= 0:
            return float("inf") if wins > 0 else 0.0
        return wins / losses

    @property
    def win_rate(self) -> float:
        if not self.r_multiples:
            return 0.0
        return sum(1 for r in self.r_multiples if r > 0) / len(self.r_multiples)


@dataclass(slots=True)
class RiskBudget:
    """Owns equity, drawdown, the exposure ladder and rolling performance."""

    config: RiskConfig
    state: RiskState = field(default_factory=RiskState)
    rolling: dict[int, RollingPerformance] = field(default_factory=dict)
    _tier_index: int = 0

    def __post_init__(self) -> None:
        if not self.rolling:
            self.rolling = {w: RollingPerformance(w) for w in self.config.rolling_windows}

    # -- equity ------------------------------------------------------------

    def update_equity(self, equity: float, ts: int = 0) -> None:
        """Record equity and recompute the exposure tier."""
        self.state.equity = equity
        self.state.peak_equity = max(self.state.peak_equity, equity)
        self.state.updated_ts = ts
        self._recompute_tier()

    def _recompute_tier(self) -> None:
        drawdown = self.state.drawdown
        target = self._tier_for(drawdown)

        if target > self._tier_index:
            # Deepening drawdown takes effect immediately — protection should
            # never wait for confirmation.
            self._apply_tier(target, drawdown, "deepened")
        elif target < self._tier_index:
            # Recovery must clear the boundary by the buffer, otherwise equity
            # sitting on an edge would flip position size every bar.
            edge = self.config.drawdown_tiers[target].max_drawdown
            if drawdown <= edge - self.config.tier_recovery_buffer:
                self._apply_tier(target, drawdown, "recovered")

    def _tier_for(self, drawdown: float) -> int:
        for i, tier in enumerate(self.config.drawdown_tiers):
            if drawdown <= tier.max_drawdown:
                return i
        return len(self.config.drawdown_tiers) - 1

    def _apply_tier(self, index: int, drawdown: float, direction: str) -> None:
        tier = self.config.drawdown_tiers[index]
        previous = self._tier_index
        self._tier_index = index
        self.state.risk_multiplier = tier.risk_multiplier
        self.state.threshold_bump = tier.threshold_bump
        log.info(
            "risk_tier_changed",
            direction=direction,
            tier=index,
            previous_tier=previous,
            drawdown=round(drawdown, 4),
            risk_multiplier=tier.risk_multiplier,
            threshold_bump=tier.threshold_bump,
        )

    # -- trades ------------------------------------------------------------

    def record_trade(self, trade: Trade) -> None:
        """Fold a closed trade into streaks and rolling performance."""
        for window in self.rolling.values():
            window.add(trade.r_multiple)

        if trade.pnl < 0:
            self.state.consecutive_losses += 1
        else:
            self.state.consecutive_losses = 0

        primary = self.rolling.get(max(self.rolling), None)
        if primary:
            self.state.rolling_expectancy_r = primary.expectancy
            self.state.rolling_profit_factor = primary.profit_factor
            self.state.rolling_window_trades = primary.count

    # -- outputs -----------------------------------------------------------

    @property
    def risk_multiplier(self) -> float:
        return self.state.risk_multiplier

    @property
    def frozen(self) -> bool:
        """True past the absolute floor: manage what is open, open nothing new.

        Distinct from ``degraded``, which is about infrastructure. This is about
        capital: below this level the problem is not one that smaller position
        sizes will solve.
        """
        return self.state.drawdown >= self.config.freeze_drawdown

    def threshold_bump(self) -> float:
        """Total conviction bump: drawdown tier plus the loss-streak surcharge.

        A losing streak raises the bar rather than banning entries. The bot gets
        fussier; if a genuinely good setup appears it still takes it.
        """
        bump = self.state.threshold_bump
        if self.state.consecutive_losses >= self.config.loss_streak_start:
            extra = self.state.consecutive_losses - self.config.loss_streak_start + 1
            bump += extra * self.config.loss_streak_threshold_bump
        return bump

    def risk_amount(self, equity: float) -> float:
        """Quote-currency risk for the next trade — this is 1R."""
        return equity * self.config.risk_per_trade * self.risk_multiplier

    def degrade(self, reason: str) -> None:
        """Enter degraded mode: hold what is open, stop opening new positions.

        Reserved for data or venue failures, never for losses. It resolves on
        its own when the underlying condition clears; nothing waits for a human.
        """
        if not self.state.degraded:
            log.warning("degraded_mode_entered", reason=reason)
        self.state.degraded = True
        self.state.degraded_reason = reason

    def recover(self) -> None:
        if self.state.degraded:
            log.info("degraded_mode_cleared", previous_reason=self.state.degraded_reason)
        self.state.degraded = False
        self.state.degraded_reason = None

    def snapshot(self) -> dict:
        return {
            "equity": round(self.state.equity, 4),
            "peak_equity": round(self.state.peak_equity, 4),
            "drawdown": round(self.state.drawdown, 4),
            "risk_multiplier": self.state.risk_multiplier,
            "threshold_bump": round(self.threshold_bump(), 2),
            "consecutive_losses": self.state.consecutive_losses,
            "degraded": self.state.degraded,
            "degraded_reason": self.state.degraded_reason,
            "rolling": {
                str(w): {
                    "trades": p.count,
                    "expectancy_r": round(p.expectancy, 4),
                    "profit_factor": round(p.profit_factor, 3)
                    if np.isfinite(p.profit_factor)
                    else None,
                    "win_rate": round(p.win_rate, 3),
                }
                for w, p in self.rolling.items()
            },
        }
