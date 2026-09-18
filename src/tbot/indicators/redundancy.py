"""Redundancy analysis: stopping the same opinion from voting twice.

The most common failure in multi-indicator systems is counting correlated
indicators as independent confirmations. RSI, Stochastic RSI and ROC agreeing is
not three confirmations — it is one momentum reading expressed three ways, and
scoring it as three is how a system convinces itself it is certain.

The fix is measurement rather than intuition. Every indicator is normalised onto
the same scale, correlated against every other, and any pair above the threshold
is collapsed into one voting block. A block casts exactly one vote, from its
highest-IC member.

Correlation is computed **per regime**, because independence is not a fixed
property: indicators that diverge usefully in a trend often collapse onto each
other in a range, which is precisely when a system most needs to know that its
"confirmation" has evaporated.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from tbot.core.logging import get_logger
from tbot.indicators.registry import REGISTRY, SignalRole, compute_signals

log = get_logger("indicators.redundancy")


@dataclass(slots=True)
class VotingBlock:
    """A set of indicators that measure substantially the same thing."""

    block_id: int
    members: list[str]
    representative: str
    max_internal_corr: float
    categories: set[str] = field(default_factory=set)

    @property
    def size(self) -> int:
        return len(self.members)

    @property
    def is_collapsed(self) -> bool:
        """True when this block absorbed indicators that looked independent."""
        return self.size > 1


def signal_correlation(
    signals: pd.DataFrame, method: str = "spearman", min_overlap: int = 100
) -> pd.DataFrame:
    """Pairwise rank correlation between normalised signal series.

    Rank correlation rather than Pearson: two indicators can be monotonically
    equivalent while living on different scales, and that is still redundancy.
    """
    usable = [c for c in signals.columns if signals[c].notna().sum() >= min_overlap]
    if len(usable) < 2:
        return pd.DataFrame()
    return signals[usable].corr(method=method, min_periods=min_overlap)


def build_blocks(
    corr: pd.DataFrame,
    threshold: float = 0.8,
    ic_scores: dict[str, float] | None = None,
) -> list[VotingBlock]:
    """Collapse correlated indicators into voting blocks.

    Uses transitive grouping (union-find): if A correlates with B and B with C,
    all three share a block even when A and C look unrelated in isolation. The
    alternative — pairwise-only grouping — lets a chain of near-duplicates vote
    several times over.
    """
    if corr.empty:
        return []

    names = list(corr.columns)
    parent = {name: name for name in names}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            rho = corr.loc[a, b]
            if pd.notna(rho) and abs(float(rho)) >= threshold:
                union(a, b)

    groups: dict[str, list[str]] = {}
    for name in names:
        groups.setdefault(find(name), []).append(name)

    ic_scores = ic_scores or {}
    blocks: list[VotingBlock] = []

    for block_id, members in enumerate(sorted(groups.values(), key=lambda m: -len(m))):
        # The representative is the member with the strongest measured IC, so a
        # block speaks with its best evidence rather than an arbitrary member.
        representative = max(members, key=lambda n: abs(ic_scores.get(n, 0.0)))
        internal = 0.0
        for i, a in enumerate(members):
            for b in members[i + 1 :]:
                rho = corr.loc[a, b]
                if pd.notna(rho):
                    internal = max(internal, abs(float(rho)))
        blocks.append(
            VotingBlock(
                block_id=block_id,
                members=sorted(members),
                representative=representative,
                max_internal_corr=round(internal, 3),
                categories={REGISTRY[m].category.value for m in members if m in REGISTRY},
            )
        )

    return blocks


def analyse_universe(
    store,
    symbols: list[str],
    timeframe: str,
    threshold: float = 0.8,
    enabled: list[str] | None = None,
    ic_scores: dict[str, float] | None = None,
    directional_only: bool = True,
) -> tuple[pd.DataFrame, list[VotingBlock]]:
    """Pool signals across the universe, then measure and collapse.

    Pooling matters: a correlation measured on one symbol can be an accident of
    that symbol's regime history. Across twenty symbols it is a property of the
    indicators.
    """
    frames: list[pd.DataFrame] = []

    for symbol in symbols:
        df = store.read_with_derivatives(symbol, timeframe)
        if len(df) < 300:
            continue
        signals = compute_signals(df, enabled)
        if directional_only:
            keep = [c for c in signals.columns if REGISTRY[c].role is SignalRole.DIRECTIONAL]
            signals = signals[keep]
        if not signals.empty:
            frames.append(signals)

    if not frames:
        return pd.DataFrame(), []

    pooled = pd.concat(frames, ignore_index=True)
    corr = signal_correlation(pooled)
    blocks = build_blocks(corr, threshold, ic_scores)

    log.info(
        "redundancy_analysed",
        indicators=len(corr.columns) if not corr.empty else 0,
        blocks=len(blocks),
        collapsed=sum(1 for b in blocks if b.is_collapsed),
        threshold=threshold,
    )
    return corr, blocks


def analyse_by_regime(
    store,
    symbols: list[str],
    timeframe: str,
    regime_labels_by_symbol: dict[str, pd.Series],
    threshold: float = 0.8,
    enabled: list[str] | None = None,
    ic_scores: dict[str, float] | None = None,
) -> dict[str, list[VotingBlock]]:
    """Voting blocks computed separately within each regime.

    Independence is regime-dependent. Two indicators that diverge usefully in a
    trend can move as one in a range — and a system that keeps counting them as
    two will be most overconfident exactly when it is least informed.
    """
    per_regime: dict[str, list[pd.DataFrame]] = {}

    for symbol in symbols:
        df = store.read_with_derivatives(symbol, timeframe)
        labels = regime_labels_by_symbol.get(symbol)
        if len(df) < 300 or labels is None or len(labels) != len(df):
            continue
        signals = compute_signals(df, enabled)
        keep = [c for c in signals.columns if REGISTRY[c].role is SignalRole.DIRECTIONAL]
        signals = signals[keep]
        for regime in labels.unique():
            mask = (labels == regime).to_numpy()
            if mask.sum() < 150:
                continue
            per_regime.setdefault(str(regime), []).append(signals[mask])

    out: dict[str, list[VotingBlock]] = {}
    for regime, frames in per_regime.items():
        pooled = pd.concat(frames, ignore_index=True)
        corr = signal_correlation(pooled)
        out[regime] = build_blocks(corr, threshold, ic_scores)

    return out


def block_lookup(blocks: list[VotingBlock]) -> dict[str, int]:
    """Map each indicator to its block id, for fast scoring-time lookup."""
    return {member: block.block_id for block in blocks for member in block.members}


def independent_vote_count(
    active_signals: dict[str, float], lookup: dict[str, int], min_strength: float = 0.2
) -> int:
    """How many genuinely independent opinions are behind a setup.

    Counts blocks, not indicators. Twelve correlated indicators all leaning long
    is one vote; that is the number the conviction score is allowed to use.
    """
    seen: set[int] = set()
    for name, value in active_signals.items():
        if abs(value) >= min_strength:
            seen.add(lookup.get(name, -1))
    return len(seen)


def summarise(blocks: list[VotingBlock]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "block": b.block_id,
                "size": b.size,
                "representative": b.representative,
                "members": ", ".join(b.members),
                "max_corr": b.max_internal_corr,
                "categories": ", ".join(sorted(b.categories)),
            }
            for b in blocks
        ]
    )


def cross_category_leakage(blocks: list[VotingBlock]) -> pd.DataFrame:
    """Blocks whose members span categories.

    Category was the prior; correlation is the measurement. Where a block spans
    two categories, the prior was wrong — a momentum and a flow indicator that
    move together are one signal, whatever their labels say. Worth seeing
    explicitly rather than trusting the taxonomy.
    """
    rows = [
        {
            "block": b.block_id,
            "categories": ", ".join(sorted(b.categories)),
            "members": ", ".join(b.members),
            "max_corr": b.max_internal_corr,
        }
        for b in blocks
        if len(b.categories) > 1
    ]
    return pd.DataFrame(rows)
