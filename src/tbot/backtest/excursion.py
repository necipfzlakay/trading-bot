"""MAE/MFE analysis — deriving exits from what trades actually did.

The operator's instruction was to avoid fixed holding times but still get the
exit timing right. Excursion analysis is the standard way to do that: instead of
choosing a stop and a target and hoping, read them off the distribution of how
past trades behaved.

**Maximum Adverse Excursion** is the worst point a trade reached before closing.
Looking only at the *winners*, the MAE distribution answers "how much room did
profitable trades actually need?" If winners rarely went beyond -0.5R, a 1R stop
is twice as wide as necessary — every loss costs double for no benefit.

**Maximum Favorable Excursion** is the best point reached. Comparing average MFE
to the average realised result shows how much of each move is being left behind.
Capturing 45% of a 2.5R average MFE means the exits, not the entries, are the
binding constraint.

**Edge decay** replaces a time limit. Bucketing trades by how long they were held
shows where returns stop compounding — which is a property of the setup, not a
number imposed on it.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from tbot.core.models import Trade


@dataclass(slots=True)
class ExcursionStats:
    setup: str
    trades: int
    win_rate: float
    avg_mae_winners: float
    p90_mae_winners: float
    avg_mae_losers: float
    avg_mfe: float
    avg_mfe_winners: float
    avg_r: float
    capture_ratio: float
    suggested_stop_r: float
    suggested_target_r: float
    stop_too_wide_by: float

    def to_dict(self) -> dict:
        return {
            "setup": self.setup,
            "trades": self.trades,
            "win_rate": round(self.win_rate, 3),
            "avg_mae_win": round(self.avg_mae_winners, 3),
            "p90_mae_win": round(self.p90_mae_winners, 3),
            "avg_mae_loss": round(self.avg_mae_losers, 3),
            "avg_mfe": round(self.avg_mfe, 3),
            "avg_mfe_win": round(self.avg_mfe_winners, 3),
            "avg_r": round(self.avg_r, 3),
            "capture": round(self.capture_ratio, 3),
            "suggested_stop_r": round(self.suggested_stop_r, 3),
            "suggested_target_r": round(self.suggested_target_r, 3),
            "stop_wider_than_needed_x": round(self.stop_too_wide_by, 2),
        }


def analyse(trades: list[Trade], setup: str = "all") -> ExcursionStats | None:
    """Excursion statistics for one setup (or all trades pooled)."""
    if len(trades) < 10:
        return None

    winners = [t for t in trades if t.is_win]
    losers = [t for t in trades if not t.is_win]

    mae_winners = np.array([abs(t.mae_r) for t in winners]) if winners else np.array([0.0])
    mae_losers = np.array([abs(t.mae_r) for t in losers]) if losers else np.array([0.0])
    mfe_all = np.array([t.mfe_r for t in trades])
    mfe_winners = np.array([t.mfe_r for t in winners]) if winners else np.array([0.0])
    realised = np.array([t.r_multiple for t in trades])

    # The stop that would have kept ~90% of winners alive. Tighter than that
    # starts cutting trades that were going to work; wider is paying for room
    # the winners never used.
    p90_mae = float(np.percentile(mae_winners, 90))
    suggested_stop = max(0.3, p90_mae * 1.1)  # small buffer for noise

    # Target from the median MFE of winners: aiming at the mean is aiming at
    # the outliers, which most trades will never reach.
    suggested_target = float(np.median(mfe_winners)) if winners else 1.0

    avg_r = float(np.mean(realised))
    avg_mfe = float(np.mean(mfe_all))
    capture = avg_r / avg_mfe if avg_mfe > 0 else 0.0

    return ExcursionStats(
        setup=setup,
        trades=len(trades),
        win_rate=len(winners) / len(trades),
        avg_mae_winners=float(np.mean(mae_winners)),
        p90_mae_winners=p90_mae,
        avg_mae_losers=float(np.mean(mae_losers)),
        avg_mfe=avg_mfe,
        avg_mfe_winners=float(np.mean(mfe_winners)),
        avg_r=avg_r,
        capture_ratio=capture,
        suggested_stop_r=suggested_stop,
        suggested_target_r=suggested_target,
        # 1.0 means the current stop is exactly as wide as winners needed.
        stop_too_wide_by=1.0 / suggested_stop if suggested_stop > 0 else 1.0,
    )


def by_setup(trades: list[Trade]) -> pd.DataFrame:
    """Excursion statistics per setup, plus a pooled row."""
    rows = []
    pooled = analyse(trades, "all")
    if pooled:
        rows.append(pooled.to_dict())

    for setup in sorted({t.setup for t in trades}):
        stats = analyse([t for t in trades if t.setup == setup], setup)
        if stats:
            rows.append(stats.to_dict())

    return pd.DataFrame(rows)


def edge_decay(trades: list[Trade], buckets: int = 6) -> pd.DataFrame:
    """Where returns stop improving with holding time.

    This is the empirical stand-in for a maximum holding period. Instead of
    "close after N bars", it shows the bar count beyond which trades of this
    setup have historically stopped adding anything — and the exit manager
    tightens the trail there rather than closing on a schedule.
    """
    if len(trades) < 20:
        return pd.DataFrame()

    frame = pd.DataFrame(
        {
            "bars": [t.bars_held for t in trades],
            "r": [t.r_multiple for t in trades],
            "mfe": [t.mfe_r for t in trades],
            "setup": [t.setup for t in trades],
        }
    )
    frame["bucket"] = pd.qcut(
        frame["bars"], min(buckets, frame["bars"].nunique()), duplicates="drop"
    )

    grouped = (
        frame.groupby("bucket", observed=True)
        .agg(
            trades=("r", "size"),
            median_bars=("bars", "median"),
            avg_r=("r", "mean"),
            median_r=("r", "median"),
            avg_mfe=("mfe", "mean"),
            win_rate=("r", lambda s: float((s > 0).mean())),
        )
        .reset_index()
    )
    grouped["bucket"] = grouped["bucket"].astype(str)
    return grouped


def suggest_decay_bars(trades: list[Trade], buckets: int = 6) -> dict[str, int]:
    """Bar count at which each setup's average return peaks.

    Feeds ``ExitManager.edge_decay_bars``. Returns an empty mapping when there
    is not enough history, so a young system simply does not apply decay rather
    than applying a number invented from nothing.
    """
    out: dict[str, int] = {}

    for setup in {t.setup for t in trades}:
        subset = [t for t in trades if t.setup == setup]
        if len(subset) < 30:
            continue
        table = edge_decay(subset, buckets)
        if table.empty:
            continue
        best = table.loc[table["avg_r"].idxmax()]
        out[setup] = int(best["median_bars"])

    return out


def suggested_atr_multiples(trades: list[Trade], current_stop_mult: float) -> dict[str, float]:
    """Translate the MAE finding back into the ATR multiple the engine uses.

    Round trip: the stop was placed at ``current_stop_mult`` x ATR, which by
    construction equals 1R. If winners only ever used 0.5R of it, the same
    protection is available at half the multiple — halving the average loss
    without cutting the winners.
    """
    out: dict[str, float] = {}

    for setup in {t.setup for t in trades} | {"all"}:
        subset = trades if setup == "all" else [t for t in trades if t.setup == setup]
        stats = analyse(subset, setup)
        if stats is None:
            continue
        out[setup] = round(current_stop_mult * stats.suggested_stop_r, 3)

    return out


def summarise(trades: list[Trade], current_stop_mult: float) -> dict:
    """Everything the exit optimiser reports, in one structure."""
    pooled = analyse(trades, "all")
    return {
        "per_setup": by_setup(trades).to_dict(orient="records"),
        "edge_decay": edge_decay(trades).to_dict(orient="records"),
        "decay_bars": suggest_decay_bars(trades),
        "suggested_atr_multiples": suggested_atr_multiples(trades, current_stop_mult),
        "pooled": pooled.to_dict() if pooled else None,
    }
