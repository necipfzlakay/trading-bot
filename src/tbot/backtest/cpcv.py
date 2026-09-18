"""Combinatorial Purged Cross-Validation.

Walk-forward has three weaknesses that matter here: it produces exactly one
out-of-sample path, it wastes the earliest data on warm-up, and it lets labels
that span the train/test boundary leak information across it.

CPCV addresses all three. The sample is cut into N blocks, every combination of
k test blocks is used, and the rest trains — producing many out-of-sample paths
instead of one. Two mechanisms keep the folds honest:

* **Purging** removes training observations whose label window overlaps the test
  set. In a trading context the label is the forward return, so a bar whose
  forward window reaches into the test period must be dropped.
* **Embargo** additionally drops training observations immediately *after* the
  test block, because serial correlation makes them near-duplicates of test
  observations.

The output is a distribution of out-of-sample results rather than a single
number, and the spread of that distribution is usually more informative than its
mean.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

import numpy as np


@dataclass(slots=True)
class CPCVSplit:
    train_indices: np.ndarray
    test_indices: np.ndarray
    test_blocks: tuple[int, ...]
    purged: int
    embargoed: int


@dataclass(slots=True)
class CPCVResult:
    paths: list[float] = field(default_factory=list)
    split_details: list[dict] = field(default_factory=list)
    n_splits: int = 0

    @property
    def mean(self) -> float:
        return float(np.mean(self.paths)) if self.paths else 0.0

    @property
    def median(self) -> float:
        return float(np.median(self.paths)) if self.paths else 0.0

    @property
    def std(self) -> float:
        return float(np.std(self.paths, ddof=1)) if len(self.paths) > 1 else 0.0

    @property
    def win_share(self) -> float:
        """Share of out-of-sample paths that were profitable.

        More useful than the mean: a strategy carried by one spectacular path is
        a different proposition from one that works most of the time.
        """
        return float(np.mean([p > 0 for p in self.paths])) if self.paths else 0.0

    def percentile(self, q: float) -> float:
        return float(np.percentile(self.paths, q)) if self.paths else 0.0

    def summary(self) -> dict:
        return {
            "splits": self.n_splits,
            "mean": round(self.mean, 5),
            "median": round(self.median, 5),
            "std": round(self.std, 5),
            "win_share": round(self.win_share, 4),
            "p05": round(self.percentile(5), 5),
            "p95": round(self.percentile(95), 5),
        }


def make_splits(
    n_samples: int,
    n_blocks: int = 10,
    n_test_blocks: int = 2,
    embargo_pct: float = 0.01,
    label_horizon: int = 0,
    max_splits: int | None = 120,
) -> Iterator[CPCVSplit]:
    """Yield purged, embargoed train/test splits.

    ``label_horizon`` is how far forward a label looks, in bars. Training rows
    within that distance of a test block are purged, because their outcome is
    partly determined by data the test set is supposed to be hiding.
    """
    if n_blocks < 3 or n_test_blocks >= n_blocks:
        raise ValueError("need at least 3 blocks and fewer test blocks than total")

    indices = np.arange(n_samples)
    blocks = np.array_split(indices, n_blocks)
    embargo = int(n_samples * embargo_pct)

    combos = list(itertools.combinations(range(n_blocks), n_test_blocks))
    if max_splits and len(combos) > max_splits:
        rng = np.random.default_rng(17)
        chosen = rng.choice(len(combos), max_splits, replace=False)
        combos = [combos[i] for i in sorted(chosen)]

    for combo in combos:
        test_idx = np.concatenate([blocks[i] for i in combo])
        test_start, test_end = int(test_idx.min()), int(test_idx.max())

        train_mask = np.ones(n_samples, dtype=bool)
        train_mask[test_idx] = False

        purge_lo = max(0, test_start - label_horizon)
        purged = int(train_mask[purge_lo:test_start].sum())
        train_mask[purge_lo:test_start] = False

        embargo_hi = min(n_samples, test_end + 1 + embargo)
        embargoed = int(train_mask[test_end + 1 : embargo_hi].sum())
        train_mask[test_end + 1 : embargo_hi] = False

        yield CPCVSplit(
            train_indices=indices[train_mask],
            test_indices=test_idx,
            test_blocks=combo,
            purged=purged,
            embargoed=embargoed,
        )


def run_cpcv(
    n_samples: int,
    evaluate: Callable[[np.ndarray, np.ndarray], float],
    n_blocks: int = 10,
    n_test_blocks: int = 2,
    embargo_pct: float = 0.01,
    label_horizon: int = 0,
    max_splits: int | None = 120,
) -> CPCVResult:
    """Run ``evaluate(train_idx, test_idx)`` over every split.

    The callable returns whatever metric matters — the framework does not care,
    as long as higher is better.
    """
    result = CPCVResult()

    for split in make_splits(
        n_samples, n_blocks, n_test_blocks, embargo_pct, label_horizon, max_splits
    ):
        score = evaluate(split.train_indices, split.test_indices)
        if np.isfinite(score):
            result.paths.append(float(score))
            result.split_details.append(
                {
                    "blocks": split.test_blocks,
                    "train": len(split.train_indices),
                    "test": len(split.test_indices),
                    "purged": split.purged,
                    "embargoed": split.embargoed,
                    "score": round(float(score), 5),
                }
            )

    result.n_splits = len(result.paths)
    return result


def walk_forward_splits(
    n_samples: int, train_size: int, test_size: int, step: int | None = None
) -> Iterator[tuple[np.ndarray, np.ndarray]]:
    """Classic rolling walk-forward. Kept for comparison against CPCV.

    Running both is informative: where walk-forward looks good and CPCV does
    not, the difference is usually a single lucky ordering of the data.
    """
    step = step or test_size
    start = 0
    while start + train_size + test_size <= n_samples:
        train = np.arange(start, start + train_size)
        test = np.arange(start + train_size, start + train_size + test_size)
        yield train, test
        start += step
