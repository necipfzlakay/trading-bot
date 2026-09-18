"""Monte Carlo stress testing.

A backtest reports one ordering of trades out of the many that could plausibly
have occurred. Reshuffling them answers a question the single path cannot: how
bad could the drawdown have been if the same trades had arrived in a different
order? A strategy whose equity curve looks smooth only because the losses
happened to be spread out is fragile, and this is where that shows.

Three stresses:

* **Trade shuffling** — same trades, different order. Gives a drawdown
  distribution rather than a single number.
* **Cost shock** — adds slippage beyond the assumed level. An edge that
  disappears when costs rise by a few basis points was never an edge.
* **Sequential bootstrap** — resamples with replacement to estimate how much of
  the observed result is attributable to a handful of trades.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from tbot.core.models import Trade


@dataclass(slots=True)
class MonteCarloResult:
    simulations: int
    median_return: float
    p05_return: float
    p95_return: float
    median_max_drawdown: float
    p95_max_drawdown: float
    worst_max_drawdown: float
    ruin_probability: float
    positive_share: float

    def summary(self) -> dict:
        return {
            "simulations": self.simulations,
            "median_return": round(self.median_return, 4),
            "return_p05": round(self.p05_return, 4),
            "return_p95": round(self.p95_return, 4),
            "median_max_dd": round(self.median_max_drawdown, 4),
            "max_dd_p95": round(self.p95_max_drawdown, 4),
            "worst_max_dd": round(self.worst_max_drawdown, 4),
            "ruin_probability": round(self.ruin_probability, 4),
            "positive_share": round(self.positive_share, 4),
        }


def shuffle_trades(
    trades: list[Trade],
    starting_balance: float,
    simulations: int = 2000,
    ruin_threshold: float = 0.5,
    seed: int = 23,
) -> MonteCarloResult:
    """Reorder the same trades many times and measure the spread of outcomes."""
    if not trades:
        return MonteCarloResult(0, 0, 0, 0, 0, 0, 0, 0, 0)

    pnl = np.array([t.pnl for t in trades], dtype="float64")
    rng = np.random.default_rng(seed)

    returns = np.empty(simulations)
    drawdowns = np.empty(simulations)
    ruined = 0

    for i in range(simulations):
        equity = starting_balance + np.cumsum(rng.permutation(pnl))
        equity = np.concatenate([[starting_balance], equity])
        peaks = np.maximum.accumulate(equity)
        dd = np.max(1.0 - equity / peaks)

        returns[i] = equity[-1] / starting_balance - 1.0
        drawdowns[i] = dd
        if dd >= ruin_threshold:
            ruined += 1

    return MonteCarloResult(
        simulations=simulations,
        median_return=float(np.median(returns)),
        p05_return=float(np.percentile(returns, 5)),
        p95_return=float(np.percentile(returns, 95)),
        median_max_drawdown=float(np.median(drawdowns)),
        p95_max_drawdown=float(np.percentile(drawdowns, 95)),
        worst_max_drawdown=float(np.max(drawdowns)),
        ruin_probability=ruined / simulations,
        positive_share=float(np.mean(returns > 0)),
    )


def cost_shock(trades: list[Trade], starting_balance: float, extra_bps: float = 10.0) -> dict:
    """Re-run the same trades with worse execution.

    The most common reason a live result trails its backtest is that real
    slippage exceeded the assumption. This puts a number on how much slippage
    the edge can absorb before it is gone.
    """
    if not trades:
        return {"extra_bps": extra_bps, "original_pnl": 0.0, "shocked_pnl": 0.0, "survives": False}

    extra = extra_bps / 10_000.0
    original = sum(t.pnl for t in trades)
    # Each round trip pays the extra cost twice — once in, once out.
    penalty = sum(t.entry_price * t.quantity * extra * 2 for t in trades)
    shocked = original - penalty

    return {
        "extra_bps": extra_bps,
        "original_pnl": round(original, 4),
        "shocked_pnl": round(shocked, 4),
        "cost_of_shock": round(penalty, 4),
        "return_after_shock": round(shocked / starting_balance, 4),
        "survives": shocked > 0,
    }


def bootstrap_expectancy(trades: list[Trade], simulations: int = 2000, seed: int = 29) -> dict:
    """Confidence interval for expectancy, by resampling trades.

    A wide interval straddling zero means the result rests on a few trades —
    common when the sample is small, and the honest reading is "not yet known".
    """
    if len(trades) < 5:
        return {"expectancy_r": 0.0, "ci_low": 0.0, "ci_high": 0.0, "samples": len(trades)}

    r = np.array([t.r_multiple for t in trades], dtype="float64")
    rng = np.random.default_rng(seed)
    means = np.array([rng.choice(r, size=len(r), replace=True).mean() for _ in range(simulations)])

    return {
        "expectancy_r": round(float(r.mean()), 4),
        "ci_low": round(float(np.percentile(means, 2.5)), 4),
        "ci_high": round(float(np.percentile(means, 97.5)), 4),
        "positive_probability": round(float(np.mean(means > 0)), 4),
        "samples": len(trades),
    }
