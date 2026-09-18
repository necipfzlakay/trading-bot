"""Conviction scoring — the selectivity engine.

The design goal, stated by the user, is that the bot does not have to trade: it
watches many symbols continuously and acts only on setups it is close to certain
about. That inverts the usual structure. Instead of "find a signal, then filter
it", every symbol is scored on every bar and only a score clearing a high bar
becomes a position. Sitting in cash is a normal outcome, and every rejection
records why.

Six components, weighted to 100:

============================  ====  =====================================
Component                     Max   What it measures
============================  ====  =====================================
regime_fit                      25  Does the permission layer allow this
indicator_confluence            25  Independent block votes, not indicators
mtf_alignment                   15  Do 1d, 4h and 1h agree
flow_confirmation               15  Is there volume behind the move
derivatives                     10  Positioning: funding, OI, crowding
expected_r                      10  Historical reward/risk of this setup
============================  ====  =====================================

Penalties subtract on top: wide spread, thin liquidity, portfolio correlation.

The single most important mechanic is in ``_score_confluence``: votes are
counted per *redundancy block*, not per indicator. Twelve correlated indicators
all leaning long is one opinion held loudly, and scoring it as twelve is how a
system talks itself into a bad trade.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from tbot.core.config import ConvictionConfig
from tbot.core.models import (
    Conviction,
    ConvictionComponent,
    IndicatorCategory,
    IndicatorSignal,
    NoTradeReason,
    Regime,
    Side,
)
from tbot.indicators.registry import REGISTRY, SignalRole
from tbot.strategy.regime import regime_allows

# Below this magnitude an indicator is treated as having no opinion rather than
# a weak one — a signal of 0.05 is noise wearing a direction.
MIN_SIGNAL_STRENGTH = 0.2


@dataclass(slots=True)
class ScoringContext:
    """Everything needed to score one symbol on one bar.

    Signals arrive pre-computed per timeframe so the engine does no indicator
    maths itself — it only weighs evidence it is handed.
    """

    symbol: str
    bar_ts: int
    regime: Regime
    trigger_signals: dict[str, float]
    setup_signals: dict[str, float]
    permission_signals: dict[str, float]
    spread_bps: float = 0.0
    volume_24h: float = 0.0
    atr: float = 0.0
    price: float = 0.0
    expected_r: float | None = None
    correlation_conflict: bool = False
    portfolio_full: bool = False
    already_open: bool = False
    stale: bool = False
    llm_veto: bool = False
    setup: str = "unknown"


@dataclass(slots=True)
class ConvictionEngine:
    """Scores setups and owns the dynamic threshold.

    ``block_lookup`` maps indicator -> voting block id, produced by the
    redundancy analysis. Without it every indicator would vote separately, which
    is the failure mode this whole layer exists to prevent.
    """

    config: ConvictionConfig
    block_lookup: dict[str, int] = field(default_factory=dict)
    selected_by_regime: dict[str, list[str]] = field(default_factory=dict)
    bars_since_last_entry: int = 0

    # -- threshold ---------------------------------------------------------

    def current_threshold(self, drawdown_bump: float = 0.0, loss_streak_bump: float = 0.0) -> float:
        """The bar a setup must clear right now.

        Rises with drawdown and with consecutive losses — the bot becomes
        pickier rather than being locked out, which is the whole point of
        replacing a binary cooldown. Falls only when opportunities have been
        genuinely scarce, and never below ``absolute_floor``: "bored, so traded"
        is not reachable from here.
        """
        threshold = self.config.base_threshold + drawdown_bump + loss_streak_bump

        if self.bars_since_last_entry > self.config.scarcity_bars:
            excess = self.bars_since_last_entry - self.config.scarcity_bars
            relief = min(
                self.config.scarcity_max_relief,
                self.config.scarcity_max_relief * (excess / self.config.scarcity_bars),
            )
            threshold -= relief

        return max(self.config.absolute_floor, threshold)

    # -- scoring -----------------------------------------------------------

    def score(
        self,
        ctx: ScoringContext,
        side: Side,
        drawdown_bump: float = 0.0,
        loss_streak_bump: float = 0.0,
    ) -> Conviction:
        """Score one (symbol, side) on one bar and explain the result."""
        threshold = self.current_threshold(drawdown_bump, loss_streak_bump)

        blocking = self._blocking_reason(ctx, side)
        if blocking is not None:
            return Conviction(
                symbol=ctx.symbol,
                bar_ts=ctx.bar_ts,
                side=side,
                total=0.0,
                threshold=threshold,
                regime=ctx.regime,
                no_trade_reason=blocking,
            )

        weights = self.config.component_weights
        allowed = self._allowed_indicators(ctx.regime)

        components = [
            self._score_regime_fit(ctx, side, weights["regime_fit"]),
            self._score_confluence(ctx, side, weights["indicator_confluence"], allowed),
            self._score_mtf(ctx, side, weights["mtf_alignment"], allowed),
            self._score_flow(ctx, side, weights["flow_confirmation"], allowed),
            self._score_derivatives(ctx, side, weights["derivatives"], allowed),
            self._score_expected_r(ctx, weights["expected_r"]),
        ]
        penalty = self._score_penalties(ctx)
        if penalty.score < 0:
            components.append(penalty)

        total = float(np.clip(sum(c.score for c in components), 0.0, 100.0))
        blocks_voting = self._count_blocks(ctx, side, allowed)

        conviction = Conviction(
            symbol=ctx.symbol,
            bar_ts=ctx.bar_ts,
            side=side,
            total=total,
            threshold=threshold,
            components=components,
            regime=ctx.regime,
            blocks_voting=blocks_voting,
        )

        if blocks_voting < self.config.min_voting_blocks:
            # Enough score, not enough independent evidence. This is the guard
            # against one loud correlated cluster carrying a trade on its own.
            conviction.no_trade_reason = NoTradeReason.BELOW_THRESHOLD
        elif total < threshold:
            conviction.no_trade_reason = NoTradeReason.BELOW_THRESHOLD

        return conviction

    # -- blocking conditions ----------------------------------------------

    def _blocking_reason(self, ctx: ScoringContext, side: Side) -> NoTradeReason | None:
        """Conditions that make scoring pointless. Checked before any maths."""
        if ctx.stale:
            return NoTradeReason.STALE_DATA
        if ctx.already_open:
            return NoTradeReason.ALREADY_IN_POSITION
        if ctx.portfolio_full:
            return NoTradeReason.MAX_POSITIONS
        if ctx.llm_veto:
            return NoTradeReason.LLM_EVENT_VETO
        if not regime_allows(ctx.regime, side.value):
            return NoTradeReason.REGIME_VETO
        if ctx.correlation_conflict:
            return NoTradeReason.CORRELATION_FULL
        return None

    def _allowed_indicators(self, regime: Regime) -> set[str]:
        """Indicators that cleared the IC bar for this regime.

        Falls back to the full registry when no selection has been run yet, so
        the engine works before the IC study exists — but the selection, once
        produced, is authoritative.
        """
        names = self.selected_by_regime.get(regime.value)
        if not names:
            names = self.selected_by_regime.get("all")
        return set(names) if names else set(REGISTRY)

    # -- components --------------------------------------------------------

    def _score_regime_fit(
        self, ctx: ScoringContext, side: Side, max_score: float
    ) -> ConvictionComponent:
        """How well the current regime supports this direction."""
        fit = {
            Regime.TREND_UP: 1.0 if side is Side.BUY else 0.0,
            Regime.RANGE: 0.55 if side is Side.BUY else 0.0,
            Regime.TREND_DOWN: 0.0,
            Regime.HIGH_VOL: 0.0,
            Regime.UNKNOWN: 0.0,
        }[ctx.regime]

        return ConvictionComponent(
            name="regime_fit",
            score=fit * max_score,
            max_score=max_score,
            detail=f"{ctx.regime.value} supports {side.value} at {fit:.0%}",
        )

    def _score_confluence(
        self, ctx: ScoringContext, side: Side, max_score: float, allowed: set[str]
    ) -> ConvictionComponent:
        """Independent voting blocks agreeing with this side.

        Each block contributes once, weighted by its representative's signal
        strength. Blocks voting *against* subtract — a setup that half the
        evidence disagrees with is not a high-conviction setup.
        """
        want = side.sign
        per_block: dict[int, float] = {}
        signals: list[IndicatorSignal] = []

        for name, value in ctx.setup_signals.items():
            if name not in allowed or np.isnan(value):
                continue
            spec = REGISTRY.get(name)
            if spec is None or spec.role is not SignalRole.DIRECTIONAL:
                continue
            if abs(value) < MIN_SIGNAL_STRENGTH:
                continue

            block = self.block_lookup.get(name, hash(name))
            # Within a block keep the strongest opinion rather than averaging:
            # averaging duplicates dilutes the very signal that defined the block.
            if abs(value) > abs(per_block.get(block, 0.0)):
                per_block[block] = value
            signals.append(
                IndicatorSignal(
                    name=name,
                    category=spec.category,
                    timeframe="setup",
                    value=float(np.clip(value, -1, 1)),
                )
            )

        if not per_block:
            return ConvictionComponent(
                name="indicator_confluence",
                score=0.0,
                max_score=max_score,
                detail="no block cleared the strength floor",
                signals=signals,
            )

        agreeing = [v * want for v in per_block.values() if v * want > 0]
        opposing = [abs(v * want) for v in per_block.values() if v * want < 0]

        net = (sum(agreeing) - sum(opposing)) / len(per_block)
        score = float(np.clip(net, 0.0, 1.0)) * max_score

        return ConvictionComponent(
            name="indicator_confluence",
            score=score,
            max_score=max_score,
            detail=f"{len(agreeing)}/{len(per_block)} blocks agree, {len(opposing)} oppose",
            signals=signals,
        )

    def _score_mtf(
        self, ctx: ScoringContext, side: Side, max_score: float, allowed: set[str]
    ) -> ConvictionComponent:
        """Agreement across the three timeframes.

        Weighted toward the slower charts: the permission layer's opinion counts
        for more than the trigger layer's, matching the rule that a higher
        timeframe vetoes a lower one and never the reverse.
        """
        want = side.sign
        layers = [
            ("permission", ctx.permission_signals, 0.45),
            ("setup", ctx.setup_signals, 0.35),
            ("trigger", ctx.trigger_signals, 0.20),
        ]

        total = 0.0
        details = []
        for label, signals, weight in layers:
            values = [
                v
                for n, v in signals.items()
                if n in allowed
                and not np.isnan(v)
                and REGISTRY.get(n)
                and REGISTRY[n].role is SignalRole.DIRECTIONAL
            ]
            if not values:
                details.append(f"{label}: n/a")
                continue
            lean = float(np.mean(values)) * want
            total += weight * float(np.clip(lean, -1.0, 1.0))
            details.append(f"{label}: {lean:+.2f}")

        return ConvictionComponent(
            name="mtf_alignment",
            score=float(np.clip(total, 0.0, 1.0)) * max_score,
            max_score=max_score,
            detail=", ".join(details),
        )

    def _score_flow(
        self, ctx: ScoringContext, side: Side, max_score: float, allowed: set[str]
    ) -> ConvictionComponent:
        """Is there participation behind the move.

        Volume is a gate rather than a vote: strong flow scales the directional
        evidence up, thin flow scales it down, and neither picks a side.
        """
        return self._score_category(
            ctx, side, max_score, allowed, IndicatorCategory.FLOW, "flow_confirmation"
        )

    def _score_derivatives(
        self, ctx: ScoringContext, side: Side, max_score: float, allowed: set[str]
    ) -> ConvictionComponent:
        """Positioning data: funding, open interest, crowding.

        Scored separately from price indicators precisely because it is not
        derived from price — it is the one component that can disagree with the
        chart for a reason the chart cannot express.
        """
        return self._score_category(
            ctx, side, max_score, allowed, IndicatorCategory.DERIVATIVES, "derivatives"
        )

    def _score_category(
        self,
        ctx: ScoringContext,
        side: Side,
        max_score: float,
        allowed: set[str],
        category: IndicatorCategory,
        component_name: str,
    ) -> ConvictionComponent:
        want = side.sign
        directional: list[float] = []
        gates: list[float] = []
        signals: list[IndicatorSignal] = []

        for name, value in ctx.setup_signals.items():
            spec = REGISTRY.get(name)
            if spec is None or spec.category is not category or np.isnan(value):
                continue
            if name not in allowed and spec.role is SignalRole.DIRECTIONAL:
                continue
            if spec.role is SignalRole.DIRECTIONAL:
                directional.append(value * want)
            else:
                gates.append(value)
            signals.append(
                IndicatorSignal(
                    name=name,
                    category=category,
                    timeframe="setup",
                    value=float(np.clip(value, -1, 1)),
                )
            )

        if not directional and not gates:
            return ConvictionComponent(
                name=component_name,
                score=0.0,
                max_score=max_score,
                detail="no data",
            )

        base = float(np.mean(directional)) if directional else 0.0
        # A gate can amplify to 1.25x or damp to 0.75x, never flip the sign.
        gate_factor = 1.0 + 0.25 * float(np.mean(gates)) if gates else 1.0
        score = float(np.clip(base * gate_factor, 0.0, 1.0)) * max_score

        return ConvictionComponent(
            name=component_name,
            score=score,
            max_score=max_score,
            detail=f"{len(directional)} directional, {len(gates)} gates, base={base:+.2f}",
            signals=signals,
        )

    def _score_expected_r(self, ctx: ScoringContext, max_score: float) -> ConvictionComponent:
        """Reward/risk implied by how similar past setups actually behaved.

        Fed by the MFE/MAE archive rather than a target pulled from the air. With
        no history yet it scores neutral, so a fresh system is not penalised for
        not yet knowing itself.
        """
        if ctx.expected_r is None:
            return ConvictionComponent(
                name="expected_r",
                score=max_score * 0.5,
                max_score=max_score,
                detail="no excursion history yet — neutral",
            )

        # 1R expected is par; 3R or better is full marks.
        normalised = float(np.clip((ctx.expected_r - 0.5) / 2.5, 0.0, 1.0))
        return ConvictionComponent(
            name="expected_r",
            score=normalised * max_score,
            max_score=max_score,
            detail=f"expected {ctx.expected_r:.2f}R from similar setups",
        )

    def _score_penalties(self, ctx: ScoringContext) -> ConvictionComponent:
        """Execution-cost realities that a chart cannot see."""
        penalty = 0.0
        reasons = []

        if ctx.spread_bps > 10.0:
            cost = min(15.0, (ctx.spread_bps - 10.0) * 0.8)
            penalty -= cost
            reasons.append(f"spread {ctx.spread_bps:.1f}bps (-{cost:.1f})")

        if 0 < ctx.volume_24h < 10_000_000:
            cost = 5.0 * (1.0 - ctx.volume_24h / 10_000_000)
            penalty -= cost
            reasons.append(f"thin 24h volume (-{cost:.1f})")

        if ctx.atr > 0 and ctx.price > 0:
            atr_pct = ctx.atr / ctx.price
            if atr_pct > 0.08:
                cost = min(10.0, (atr_pct - 0.08) * 100)
                penalty -= cost
                reasons.append(f"ATR {atr_pct:.1%} of price (-{cost:.1f})")

        return ConvictionComponent(
            name="penalties",
            score=penalty,
            max_score=0.0,
            detail="; ".join(reasons) if reasons else "none",
        )

    def _count_blocks(self, ctx: ScoringContext, side: Side, allowed: set[str]) -> int:
        """Independent blocks leaning the same way as the proposed side."""
        want = side.sign
        blocks: set[int] = set()
        for name, value in ctx.setup_signals.items():
            if name not in allowed or np.isnan(value) or abs(value) < MIN_SIGNAL_STRENGTH:
                continue
            spec = REGISTRY.get(name)
            if spec is None or spec.role is not SignalRole.DIRECTIONAL:
                continue
            if value * want > 0:
                blocks.add(self.block_lookup.get(name, hash(name)))
        return len(blocks)

    # -- bookkeeping -------------------------------------------------------

    def note_bar(self, entered: bool) -> None:
        """Advance the scarcity counter that relaxes the threshold over time."""
        self.bars_since_last_entry = 0 if entered else self.bars_since_last_entry + 1


def signals_at(signal_frame: pd.DataFrame, index: int) -> dict[str, float]:
    """Extract one bar's signals as a plain dict, NaNs preserved."""
    if signal_frame.empty or index < 0 or index >= len(signal_frame):
        return {}
    row = signal_frame.iloc[index]
    return {str(k): float(v) for k, v in row.items()}
