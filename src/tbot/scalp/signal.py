"""The scalper's one decision: closed one-minute bars in, an intent or nothing out.

This is the whole of H18, and it is deliberately a pure function of closed bars.
The live loop and the backtester both call it and nothing else decides, so the
two cannot drift apart (invariant 59) — and because it only ever sees bars the
stream has already closed, the forming minute cannot reach it (invariant 1).

**Trigger.** Over the last ``trigger_bars`` closed bars price moved more than
``trigger_sigma`` standard deviations of the recent one-bar returns, scaled to
that horizon: ``|c[t]/c[t-k] - 1| > z * sigma * sqrt(k)``.

**Arms.** Each trigger is assigned to exactly one arm by a hash of (symbol, bar).
Momentum enters with the move, reversion against it. The assignment cannot see
the outcome, so the two arms are measured on the same population of events —
the cleanest comparison a forward test can make. It also makes a conflict
between them impossible: on the same event they want opposite sides, and one-way
mode would net them.

Measured before any of this ran (Bybit perp, 6 symbols x 7 days): conditional on
this trigger, the better arm's hit rate at +/-0.3% was 57.8% against a break-even
of 61.7%, n=218. The pre-registered expectation is negative.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from dataclasses import dataclass

from tbot.core.config import ScalpConfig
from tbot.core.models import Side


@dataclass(frozen=True, slots=True)
class Intent:
    """What the signal wants on this bar. Prices are set later, from the book."""

    symbol: str
    bar_ts: int  # open time of the closed bar that fired
    arm: str  # "momentum" | "reversion"
    side: Side
    move: float  # signed trigger move, as a fraction
    threshold: float  # the move it had to exceed, as a fraction

    @property
    def setup(self) -> str:
        """The tag every order, position and trade of this arm carries."""
        return f"scalp_{self.arm}"


def arm_for(symbol: str, bar_ts: int, arms: Sequence[str]) -> str:
    """Deterministic arm assignment that the outcome cannot influence.

    ``hash()`` would do in one process and not across two: Python salts string
    hashes per process, so the live engine and the backtester would disagree
    about which arm an event belongs to. blake2b does not.
    """
    digest = hashlib.blake2b(f"{symbol}|{bar_ts}".encode(), digest_size=8).digest()
    return arms[int.from_bytes(digest, "big") % len(arms)]


def required_bars(config: ScalpConfig) -> int:
    """Closed bars needed before the first decision is possible."""
    return max(config.sigma_window, config.trigger_bars) + 1


def evaluate(
    symbol: str, closes: Sequence[float], bar_ts: int, config: ScalpConfig
) -> Intent | None:
    """``closes`` are closed one-minute closes, oldest first; the last is bar ``bar_ts``."""
    if len(closes) < required_bars(config):
        return None  # warming up is "no opinion", not a neutral vote (invariant 10)

    window = closes[-(config.sigma_window + 1) :]
    returns = [b / a - 1.0 for a, b in zip(window[:-1], window[1:], strict=True) if a > 0]
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    sigma = math.sqrt(sum((r - mean) ** 2 for r in returns) / (len(returns) - 1))
    if not sigma > 0:
        return None

    anchor = closes[-1 - config.trigger_bars]
    if anchor <= 0:
        return None
    move = closes[-1] / anchor - 1.0
    threshold = config.trigger_sigma * sigma * math.sqrt(config.trigger_bars)
    if abs(move) <= threshold:
        return None

    arm = arm_for(symbol, bar_ts, config.arms)
    with_move = Side.BUY if move > 0 else Side.SELL
    side = with_move if arm == "momentum" else with_move.opposite
    return Intent(symbol, bar_ts, arm, side, move, threshold)


def exit_levels(side: Side, entry: float, config: ScalpConfig) -> tuple[float, float]:
    """(take_profit, stop_loss) around the actual entry price."""
    if side is Side.BUY:
        return entry * (1 + config.take_profit_pct), entry * (1 - config.stop_loss_pct)
    return entry * (1 - config.take_profit_pct), entry * (1 + config.stop_loss_pct)
