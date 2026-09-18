"""Cross-sectional selection: rank symbols against each other, not against a number.

This exists because of a mismatch between what was measured and what was built.

The tail analysis that justified the whole momentum thesis was **cross-sectional**:
at each bar it pooled every symbol, cut the signal distribution into deciles, and
measured what the top decile did next. The conviction engine, meanwhile, scores
each symbol independently against a fixed threshold. Those are different
strategies. One asks "is this symbol strong in absolute terms?", the other asks
"is this symbol among the strongest available right now?"

The difference is not cosmetic. An absolute threshold produces feast and famine:
in a broad rally everything clears the bar at once, and in a quiet quarter
nothing does for months — which is exactly the pattern the backtests showed. A
relative rank always identifies the top of whatever distribution exists, so
opportunity count stays roughly constant and the sample becomes measurable.

It is also more honest about what a signal means. A momentum reading of 0.7 is
not informative on its own; whether it is the best or the worst of twenty
symbols is.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass(slots=True)
class RankedCandidate:
    symbol: str
    score: float
    rank: int
    percentile: float
    eligible: bool
    reason: str = ""


@dataclass(slots=True)
class CrossSectionalRanker:
    """Ranks one bar's scored symbols and picks the top slice.

    ``top_percentile`` mirrors the tail analysis: 0.9 means the top decile, the
    same cut the measurement used. ``min_universe`` guards against ranking in a
    near-empty cross-section, where "top decile of three symbols" is just "the
    best of three" and carries no information.
    """

    top_percentile: float = 0.9
    min_universe: int = 8
    min_absolute_score: float = 0.0
    history: list[float] = field(default_factory=list)

    def rank(self, scores: dict[str, float]) -> list[RankedCandidate]:
        """Order symbols by score and mark which fall in the top slice."""
        usable = {s: v for s, v in scores.items() if v is not None and np.isfinite(v)}
        if not usable:
            return []

        ordered = sorted(usable.items(), key=lambda kv: -kv[1])
        values = np.array([v for _, v in ordered], dtype="float64")

        if len(ordered) < self.min_universe:
            # Too few symbols for a percentile to mean anything.
            return [
                RankedCandidate(
                    symbol=symbol,
                    score=score,
                    rank=i + 1,
                    percentile=1.0 - i / max(1, len(ordered) - 1),
                    eligible=False,
                    reason=f"cross-section too small ({len(ordered)} < {self.min_universe})",
                )
                for i, (symbol, score) in enumerate(ordered)
            ]

        cutoff = float(np.quantile(values, self.top_percentile))
        candidates = []

        for i, (symbol, score) in enumerate(ordered):
            percentile = 1.0 - i / (len(ordered) - 1)
            in_top = score >= cutoff
            # A relative rank still needs an absolute floor. Being the best of a
            # uniformly bad cross-section is not a reason to buy anything.
            above_floor = score >= self.min_absolute_score

            if in_top and above_floor:
                reason = ""
            elif not in_top:
                reason = f"rank {i + 1}/{len(ordered)}, below the top {1 - self.top_percentile:.0%}"
            else:
                reason = f"score {score:.1f} below the absolute floor {self.min_absolute_score:.1f}"

            candidates.append(
                RankedCandidate(
                    symbol=symbol,
                    score=score,
                    rank=i + 1,
                    percentile=percentile,
                    eligible=in_top and above_floor,
                    reason=reason,
                )
            )

        self.history.append(cutoff)
        if len(self.history) > 500:
            self.history.pop(0)

        return candidates

    @property
    def median_cutoff(self) -> float:
        """Where the bar has typically sat. Diagnostic, not used in decisions."""
        return float(np.median(self.history)) if self.history else 0.0

    def eligible_symbols(self, scores: dict[str, float]) -> list[str]:
        return [c.symbol for c in self.rank(scores) if c.eligible]


def dispersion(scores: dict[str, float]) -> float:
    """Spread of the cross-section, as a fraction of its own range.

    Near zero means every symbol looks the same and a rank carries no
    information — worth knowing before acting on "the best" of them.
    """
    values = np.array([v for v in scores.values() if v is not None and np.isfinite(v)])
    if values.size < 3:
        return 0.0
    spread = float(np.percentile(values, 90) - np.percentile(values, 10))
    scale = float(np.abs(values).max())
    return spread / scale if scale > 0 else 0.0
