"""Deflated Sharpe Ratio and Probability of Backtest Overfitting.

Trying many configurations and reporting the best one is not research, it is
selection bias with extra steps. Given enough attempts, a strategy with no edge
will produce an impressive Sharpe by chance — and the more carefully it is
tuned, the more impressive that chance result looks.

Two corrections, both from López de Prado and Bailey:

**Deflated Sharpe Ratio.** Adjusts an observed Sharpe for the number of trials,
the length of the sample, and the skew and kurtosis of the returns. It answers
"how likely is a Sharpe this high from a strategy with no edge, given that I
tried N times?" Ten trials producing a best Sharpe of 1.8 is a very different
claim from one trial producing 1.8, and DSR is where that difference becomes a
number.

**Probability of Backtest Overfitting.** Splits the sample into combinations of
in-sample and out-of-sample blocks and asks how often the configuration that
looked best in-sample ranked below median out-of-sample. PBO above 0.5 means the
selection process is worse than picking at random.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass

import numpy as np
from scipy import stats


@dataclass(slots=True)
class DSRResult:
    sharpe: float
    deflated_sharpe: float
    p_value: float
    trials: int
    expected_max_sharpe: float
    skew: float
    kurtosis: float
    samples: int

    @property
    def is_significant(self) -> bool:
        """Survives the correction at the 5% level."""
        return self.p_value < 0.05


def expected_max_sharpe(trials: int, variance: float = 1.0) -> float:
    """Expected maximum Sharpe from ``trials`` strategies that all have zero edge.

    This is the bar a real result has to clear. It grows with the number of
    trials, which is precisely why "I tested 200 parameter sets and this one is
    great" is not evidence on its own.
    """
    if trials <= 1:
        return 0.0
    euler = 0.5772156649015329
    sqrt_var = np.sqrt(variance)
    a = (1 - euler) * stats.norm.ppf(1 - 1.0 / trials)
    b = euler * stats.norm.ppf(1 - 1.0 / (trials * np.e))
    return float(sqrt_var * (a + b))


def deflated_sharpe(
    returns: np.ndarray,
    trials: int = 1,
    benchmark_sharpe: float | None = None,
    periods_per_year: int = 365,
) -> DSRResult:
    """Sharpe ratio corrected for selection bias and non-normal returns."""
    returns = np.asarray(returns, dtype="float64")
    returns = returns[np.isfinite(returns)]
    n = len(returns)

    if n < 20:
        return DSRResult(0.0, 0.0, 1.0, trials, 0.0, 0.0, 0.0, n)

    mean, sd = float(np.mean(returns)), float(np.std(returns, ddof=1))
    if sd <= 0:
        return DSRResult(0.0, 0.0, 1.0, trials, 0.0, 0.0, 0.0, n)

    sharpe = mean / sd
    annualised = sharpe * np.sqrt(periods_per_year)
    skew = float(stats.skew(returns))
    kurt = float(stats.kurtosis(returns, fisher=False))

    threshold = (
        benchmark_sharpe
        if benchmark_sharpe is not None
        else expected_max_sharpe(trials, sd**2) / sd
    )

    # Variance of the Sharpe estimator, adjusted for skew and fat tails. Crypto
    # returns are strongly non-normal, and ignoring that overstates confidence.
    denom = 1.0 - skew * sharpe + (kurt - 1.0) / 4.0 * sharpe**2
    if denom <= 0 or n <= 1:
        return DSRResult(annualised, 0.0, 1.0, trials, threshold, skew, kurt, n)

    statistic = (sharpe - threshold) * np.sqrt(n - 1) / np.sqrt(denom)
    probability = float(stats.norm.cdf(statistic))

    return DSRResult(
        sharpe=float(annualised),
        deflated_sharpe=float(probability),
        p_value=float(1.0 - probability),
        trials=trials,
        expected_max_sharpe=float(threshold),
        skew=skew,
        kurtosis=kurt,
        samples=n,
    )


@dataclass(slots=True)
class PBOResult:
    pbo: float
    splits: int
    median_oos_rank: float
    logit_slope: float
    in_sample_best_oos_ranks: list[float]

    @property
    def verdict(self) -> str:
        if self.pbo > 0.5:
            return "OVERFIT — in-sample selection performs worse than random out of sample"
        if self.pbo > 0.3:
            return "FRAGILE — selection carries meaningful overfitting risk"
        return "ACCEPTABLE — in-sample selection tends to hold up out of sample"


def probability_of_backtest_overfitting(performance: np.ndarray, splits: int = 16) -> PBOResult:
    """PBO from a matrix of per-configuration, per-period performance.

    ``performance`` is (periods, configurations). The sample is cut into blocks,
    every balanced in-sample/out-of-sample combination is formed, and for each
    one the configuration that won in sample is ranked out of sample. How often
    it lands below the median is the probability of backtest overfitting.
    """
    performance = np.asarray(performance, dtype="float64")
    if performance.ndim != 2 or performance.shape[1] < 2:
        return PBOResult(1.0, 0, 0.5, 0.0, [])

    periods, configs = performance.shape
    blocks = min(splits, max(4, periods // 20))
    if blocks % 2:
        blocks -= 1
    if blocks < 4:
        return PBOResult(1.0, 0, 0.5, 0.0, [])

    chunks = np.array_split(np.arange(periods), blocks)
    half = blocks // 2
    ranks: list[float] = []

    combos = list(itertools.combinations(range(blocks), half))
    # The full combination set explodes; a stable subset gives the same answer.
    if len(combos) > 200:
        rng = np.random.default_rng(11)
        combos = [combos[i] for i in rng.choice(len(combos), 200, replace=False)]

    for combo in combos:
        is_idx = np.concatenate([chunks[i] for i in combo])
        oos_idx = np.concatenate([chunks[i] for i in range(blocks) if i not in combo])

        is_perf = performance[is_idx].mean(axis=0)
        oos_perf = performance[oos_idx].mean(axis=0)

        best = int(np.argmax(is_perf))
        # Relative rank of the in-sample winner among out-of-sample results.
        rank = float((oos_perf < oos_perf[best]).sum()) / (configs - 1)
        ranks.append(rank)

    ranks_array = np.array(ranks)
    pbo = float(np.mean(ranks_array < 0.5))

    # Logit slope: how strongly in-sample rank carries over. Near zero means it
    # carries no information at all.
    clipped = np.clip(ranks_array, 1e-6, 1 - 1e-6)
    logits = np.log(clipped / (1 - clipped))

    return PBOResult(
        pbo=pbo,
        splits=len(combos),
        median_oos_rank=float(np.median(ranks_array)),
        logit_slope=float(np.mean(logits)),
        in_sample_best_oos_ranks=[round(r, 4) for r in ranks[:50]],
    )
