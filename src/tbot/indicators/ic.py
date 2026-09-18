"""Information Coefficient: measuring whether an indicator predicts anything.

The IC is the rank correlation between an indicator's signal at bar *t* and the
return that follows. It is the honest answer to "does this indicator work?",
and it replaces the usual approach of assuming that a popular indicator must.

What the numbers mean in practice:

* ``|IC| < 0.02`` — noise. Excluded from scoring however well-known it is.
* ``0.02–0.05`` — weak but usable as one vote among several.
* ``> 0.05`` — strong for a single technical indicator on liquid crypto.
  Anything above 0.15 should be treated as a bug until proven otherwise.

Two corrections matter more than the headline number:

**Overlapping windows.** A 24-bar forward return sampled every bar reuses the
same future 24 times. Naive significance testing on that is wrong by roughly
sqrt(horizon), so the effective sample size is deflated accordingly.

**Stability.** A single pooled IC hides an indicator that worked brilliantly in
2023 and stopped in 2024. IC-IR (mean IC divided by its standard deviation
across sub-periods) catches that; a high IC with a low IC-IR is a period effect
wearing an indicator's clothes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd
from scipy import stats

from tbot.core.logging import get_logger
from tbot.core.models import Regime
from tbot.indicators.registry import REGISTRY, compute_signals
from tbot.strategy.regime import classify_series

log = get_logger("indicators.ic")


@dataclass(slots=True)
class ICResult:
    indicator: str
    category: str
    role: str
    horizon: int
    regime: str
    ic: float
    ic_ir: float
    t_stat: float
    p_value: float
    samples: int
    effective_samples: int

    @property
    def is_significant(self) -> bool:
        return abs(self.t_stat) > 2.0

    def passes(self, min_abs_ic: float, min_ic_ir: float) -> bool:
        return abs(self.ic) >= min_abs_ic and abs(self.ic_ir) >= min_ic_ir


def forward_returns(close: pd.Series, horizon: int) -> pd.Series:
    """Return from bar *t* close to bar *t+horizon* close.

    This is the one place in the codebase where a negative shift is correct: the
    label is *supposed* to be the future. It must never be fed back into a
    signal, which is why it lives in the analysis module and not in indicators.
    """
    return close.shift(-horizon) / close - 1.0


def spearman_ic(signal: pd.Series, fwd: pd.Series) -> tuple[float, int]:
    """Rank correlation between a signal and the return that followed it."""
    paired = pd.concat([signal, fwd], axis=1).dropna()
    if len(paired) < 30:
        return float("nan"), len(paired)
    a = paired.iloc[:, 0].to_numpy()
    b = paired.iloc[:, 1].to_numpy()
    if np.std(a) == 0 or np.std(b) == 0:
        return float("nan"), len(paired)
    rho, _ = stats.spearmanr(a, b)
    return float(rho), len(paired)


def ic_information_ratio(
    signal: pd.Series, fwd: pd.Series, windows: int = 8, min_window: int = 60
) -> float:
    """Mean IC across sub-periods divided by its dispersion.

    An indicator that only worked in one market phase shows a decent mean IC and
    an IC-IR near zero. That distinction is the difference between an edge and a
    coincidence.
    """
    paired = pd.concat([signal, fwd], axis=1).dropna()
    if len(paired) < windows * min_window:
        windows = max(2, len(paired) // min_window)
    if windows < 2:
        return float("nan")

    # Split by positional index rather than handing the frame to array_split:
    # numpy returns bare ndarrays for a DataFrame and the column labels are lost.
    ics = []
    for positions in np.array_split(np.arange(len(paired)), windows):
        if len(positions) < min_window:
            continue
        chunk = paired.iloc[positions]
        a, b = chunk.iloc[:, 0].to_numpy(), chunk.iloc[:, 1].to_numpy()
        if np.std(a) == 0 or np.std(b) == 0:
            continue
        rho, _ = stats.spearmanr(a, b)
        if not np.isnan(rho):
            ics.append(rho)

    if len(ics) < 2:
        return float("nan")
    sd = float(np.std(ics, ddof=1))
    return float(np.mean(ics) / sd) if sd > 0 else float("nan")


def _t_statistic(ic: float, samples: int, horizon: int) -> tuple[float, float, int]:
    """t-statistic with the effective sample size deflated for overlap.

    Sampling a horizon-bar forward return every bar reuses each future window
    ``horizon`` times. Treating those as independent observations overstates
    significance by roughly sqrt(horizon), which is exactly how a noise
    indicator acquires a publishable t-stat.
    """
    effective = max(10, int(samples / max(1, horizon)))
    if np.isnan(ic) or effective <= 3:
        return float("nan"), float("nan"), effective
    denom = 1.0 - ic**2
    if denom <= 0:
        return float("nan"), float("nan"), effective
    t = ic * np.sqrt((effective - 2) / denom)
    p = float(2 * (1 - stats.t.cdf(abs(t), effective - 2)))
    return float(t), p, effective


def analyse_symbol(
    df: pd.DataFrame,
    regime_labels: pd.Series,
    horizons: list[int],
    enabled: list[str] | None = None,
) -> list[ICResult]:
    """IC for every indicator on one symbol, overall and per regime."""
    signals = compute_signals(df, enabled)
    if signals.empty:
        return []

    results: list[ICResult] = []
    close = df["close"]

    for horizon in horizons:
        fwd = forward_returns(close, horizon)

        for name in signals.columns:
            spec = REGISTRY[name]
            signal = signals[name]

            ic, samples = spearman_ic(signal, fwd)
            t_stat, p_value, effective = _t_statistic(ic, samples, horizon)
            results.append(
                ICResult(
                    indicator=name,
                    category=spec.category.value,
                    role=spec.role.value,
                    horizon=horizon,
                    regime="all",
                    ic=ic,
                    ic_ir=ic_information_ratio(signal, fwd),
                    t_stat=t_stat,
                    p_value=p_value,
                    samples=samples,
                    effective_samples=effective,
                )
            )

            for regime in (Regime.TREND_UP, Regime.TREND_DOWN, Regime.RANGE, Regime.HIGH_VOL):
                mask = regime_labels == regime.value
                if int(mask.sum()) < 50:
                    continue
                r_ic, r_samples = spearman_ic(signal[mask], fwd[mask])
                r_t, r_p, r_eff = _t_statistic(r_ic, r_samples, horizon)
                results.append(
                    ICResult(
                        indicator=name,
                        category=spec.category.value,
                        role=spec.role.value,
                        horizon=horizon,
                        regime=regime.value,
                        ic=r_ic,
                        ic_ir=ic_information_ratio(signal[mask], fwd[mask]),
                        t_stat=r_t,
                        p_value=r_p,
                        samples=r_samples,
                        effective_samples=r_eff,
                    )
                )

    return results


def analyse_universe(
    store,
    symbols: list[str],
    timeframe: str,
    horizons: list[int],
    regime_timeframe: str = "1d",
    enabled: list[str] | None = None,
) -> pd.DataFrame:
    """Run the IC analysis across the universe and pool the per-symbol results.

    Regime is classified on the slower timeframe and mapped down, matching how
    the live engine sees it — the permission layer is always the slow chart.
    """
    rows: list[dict] = []

    for symbol in symbols:
        df = store.read_with_derivatives(symbol, timeframe)
        if len(df) < 300:
            log.debug("ic_skip_short_series", symbol=symbol, bars=len(df))
            continue

        regime_df = store.read_ohlcv(symbol, regime_timeframe)
        if len(regime_df) < 250:
            continue
        regime_labels = _map_regime_down(classify_series(regime_df), regime_df["ts"], df["ts"])

        for result in analyse_symbol(df, regime_labels, horizons, enabled):
            rows.append({"symbol": symbol, **asdict(result)})

    return pd.DataFrame(rows)


def _map_regime_down(labels: pd.Series, slow_ts: pd.Series, fast_ts: pd.Series) -> pd.Series:
    """Project slow-timeframe regime labels onto fast-timeframe bars.

    Backward-looking by construction: a fast bar takes the label of the most
    recent *completed* slow bar. Using the slow bar that contains it would leak
    a label formed partly from the fast bar's own future.
    """
    slow = pd.DataFrame({"ts": slow_ts.to_numpy(), "regime": labels.to_numpy()}).sort_values("ts")
    fast = pd.DataFrame({"ts": fast_ts.to_numpy()}).sort_values("ts")
    merged = pd.merge_asof(fast, slow, on="ts", direction="backward", allow_exact_matches=False)
    return pd.Series(merged["regime"].fillna(Regime.UNKNOWN.value).to_numpy(), index=fast_ts.index)


def aggregate(raw: pd.DataFrame) -> pd.DataFrame:
    """Pool per-symbol IC into one verdict per (indicator, horizon, regime).

    Uses the median across symbols rather than the mean: one altcoin with a
    freak run should not promote an indicator that does nothing elsewhere.
    ``consistency`` — the share of symbols agreeing on sign — is the column that
    separates a real effect from an averaging artefact.
    """
    if raw.empty:
        return raw

    grouped = (
        raw.groupby(["indicator", "category", "role", "horizon", "regime"])
        .agg(
            ic=("ic", "median"),
            ic_mean=("ic", "mean"),
            ic_ir=("ic_ir", "median"),
            t_stat=("t_stat", "median"),
            symbols=("symbol", "nunique"),
            samples=("samples", "sum"),
        )
        .reset_index()
    )

    consistency = (
        raw.assign(sign=np.sign(raw["ic"]))
        .groupby(["indicator", "horizon", "regime"])["sign"]
        .agg(lambda s: float(np.abs(s.mean())) if len(s) else np.nan)
        .reset_index()
        .rename(columns={"sign": "consistency"})
    )

    return grouped.merge(consistency, on=["indicator", "horizon", "regime"], how="left")


def select_indicators(
    agg: pd.DataFrame,
    min_abs_ic: float = 0.02,
    min_ic_ir: float = 0.3,
    min_consistency: float = 0.4,
) -> dict[str, list[str]]:
    """Which indicators earn a vote, per regime.

    An indicator qualifies for a regime if it clears the IC floor, the stability
    floor and the cross-symbol sign-agreement floor at any measured horizon.
    Everything else is excluded — including indicators that are household names.
    """
    selected: dict[str, list[str]] = {}
    directional = agg[agg["role"] == "directional"]

    for regime in directional["regime"].unique():
        subset = directional[directional["regime"] == regime]
        qualifying = subset[
            (subset["ic"].abs() >= min_abs_ic)
            & (subset["ic_ir"].abs() >= min_ic_ir)
            & (subset["consistency"] >= min_consistency)
        ]
        selected[str(regime)] = sorted(qualifying["indicator"].unique().tolist())

    return selected


# --------------------------------------------------------------------------
# Tail analysis — what the bot will actually trade
# --------------------------------------------------------------------------


@dataclass(slots=True)
class TailResult:
    """Conditional returns in the extremes of an indicator's distribution."""

    indicator: str
    category: str
    horizon: int
    regime: str
    top_decile_return: float
    bottom_decile_return: float
    base_return: float
    spread: float
    top_excess: float
    top_net_of_costs: float
    top_hit_rate: float
    samples: int
    effective_samples: int
    ci_low: float
    ci_high: float

    @property
    def is_tradeable(self) -> bool:
        """Positive after costs, and the confidence interval stays above zero."""
        return self.top_net_of_costs > 0 and self.ci_low > 0


def tail_analysis(
    signal: pd.Series,
    fwd: pd.Series,
    horizon: int,
    quantile: float = 0.9,
    round_trip_cost: float = 0.002,
    bootstrap: int = 500,
    seed: int = 7,
) -> tuple[float, float, float, float, int, float, float] | None:
    """Mean forward return in the top and bottom deciles of a signal.

    This matters more than the IC for this system, and the two can disagree
    sharply. The IC is a rank correlation over *every* bar, so it measures a
    monotonic relationship across the whole distribution. This bot never trades
    the middle of the distribution — it waits for extremes. An indicator whose
    relationship is flat or noisy in the middle but strong in the tails scores a
    near-zero (or negative) IC while being exactly what the strategy wants.

    Confidence intervals come from a **block** bootstrap, resampling contiguous
    chunks of length ``horizon``. Resampling individual bars would treat
    overlapping forward windows as independent and produce intervals several
    times too narrow — which is how noise acquires a confident-looking result.
    """
    paired = pd.concat([signal, fwd], axis=1).dropna()
    if len(paired) < 500:
        return None

    values = paired.iloc[:, 0].to_numpy()
    returns = paired.iloc[:, 1].to_numpy()
    if np.std(values) == 0:
        return None

    hi_cut = np.quantile(values, quantile)
    lo_cut = np.quantile(values, 1.0 - quantile)
    hi_mask = values >= hi_cut
    lo_mask = values <= lo_cut
    if hi_mask.sum() < 50 or lo_mask.sum() < 50:
        return None

    top = float(np.mean(returns[hi_mask]))
    bottom = float(np.mean(returns[lo_mask]))
    base = float(np.mean(returns))
    hit_rate = float(np.mean(returns[hi_mask] > 0))

    top_returns = returns[hi_mask]
    effective = max(10, int(len(top_returns) / max(1, horizon)))

    rng = np.random.default_rng(seed)
    block = max(1, horizon)
    n_blocks = max(1, len(top_returns) // block)
    means = np.empty(bootstrap)
    for i in range(bootstrap):
        starts = rng.integers(0, max(1, len(top_returns) - block), size=n_blocks)
        sample = np.concatenate([top_returns[s : s + block] for s in starts])
        means[i] = sample.mean()
    ci_low, ci_high = np.percentile(means, [2.5, 97.5])

    return (
        top,
        bottom,
        base,
        hit_rate,
        effective,
        float(ci_low) - round_trip_cost,
        float(ci_high) - round_trip_cost,
    )


def analyse_tails(
    store,
    symbols: list[str],
    timeframe: str,
    horizons: list[int],
    round_trip_cost: float = 0.002,
    enabled: list[str] | None = None,
    regime_timeframe: str | None = None,
) -> pd.DataFrame:
    """Pooled tail analysis across the universe.

    Signals are pooled across symbols before bucketing so the decile cut is a
    universe-wide threshold, matching how the scanner ranks candidates against
    each other rather than against each symbol's own history.
    """
    pooled: dict[int, list[pd.DataFrame]] = {h: [] for h in horizons}

    for symbol in symbols:
        df = store.read_with_derivatives(symbol, timeframe)
        if len(df) < 400:
            continue
        signals = compute_signals(df, enabled)
        if signals.empty:
            continue
        regime_col = None
        if regime_timeframe:
            slow = store.read_ohlcv(symbol, regime_timeframe)
            if len(slow) >= 250:
                regime_col = _map_regime_down(classify_series(slow), slow["ts"], df["ts"])

        for horizon in horizons:
            fwd = forward_returns(df["close"], horizon)
            frame = signals.copy()
            frame["__fwd"] = fwd
            if regime_col is not None:
                frame["__regime"] = regime_col.to_numpy()
            pooled[horizon].append(frame)

    rows: list[dict] = []
    for horizon, frames in pooled.items():
        if not frames:
            continue
        data = pd.concat(frames, ignore_index=True)
        indicator_cols = [c for c in data.columns if not c.startswith("__")]

        regimes = ["all"]
        if "__regime" in data.columns:
            regimes += [r for r in data["__regime"].dropna().unique() if r != Regime.UNKNOWN.value]

        for regime in regimes:
            subset = data if regime == "all" else data[data["__regime"] == regime]
            if len(subset) < 1000:
                continue
            for name in indicator_cols:
                spec = REGISTRY.get(name)
                if spec is None or spec.role.value != "directional":
                    continue
                result = tail_analysis(
                    subset[name], subset["__fwd"], horizon, round_trip_cost=round_trip_cost
                )
                if result is None:
                    continue
                top, bottom, base, hit, effective, ci_low, ci_high = result
                rows.append(
                    {
                        "indicator": name,
                        "category": spec.category.value,
                        "horizon": horizon,
                        "regime": str(regime),
                        "top_decile": top,
                        "bottom_decile": bottom,
                        "base": base,
                        "spread": top - bottom,
                        "top_excess": top - base,
                        "top_net": top - round_trip_cost,
                        "hit_rate": hit,
                        "samples": len(subset),
                        "effective": effective,
                        "ci_low": ci_low,
                        "ci_high": ci_high,
                        "tradeable": ci_low > 0 and (top - base) > 0,
                    }
                )

    return pd.DataFrame(rows)


def select_by_tails(
    tails: pd.DataFrame, min_excess: float = 0.005, require_ci: bool = True
) -> dict[str, list[str]]:
    """Indicators whose top decile beats the base rate by a real margin.

    Selection is on *excess over the base rate*, not raw return. In a period when
    everything rose, a raw 4% return means nothing; beating the unconditional
    mean by 3% is the part that could survive a flat market.
    """
    selected: dict[str, list[str]] = {}
    if tails.empty:
        return selected

    for regime in tails["regime"].unique():
        subset = tails[tails["regime"] == regime]
        qualifying = subset[subset["top_excess"] >= min_excess]
        if require_ci:
            qualifying = qualifying[qualifying["ci_low"] > 0]
        selected[str(regime)] = sorted(qualifying["indicator"].unique().tolist())

    return selected
