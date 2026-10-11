"""What the three low-frequency strategies want to hold. Pure functions.

H19 carry, H20 trend and H21 cross-sectional momentum (``research/hypotheses.md``)
each reduce to one question asked at a daily close: *which symbols should this
book hold now?* The answer is a function of closed data only — daily closes for
the two price strategies, settled funding for carry — so the backtest and the
live engine call the same function on the same kind of input (invariant 59),
and nothing here can see a bar that has not closed (invariant 1).

Every number these functions read is pre-registered in ``RebalanceConfig``.
They were fixed from the literature's defaults before the archive was asked
anything; they are the hypothesis, not settings to tune.

The answer is a set of names, never a size. A position is sized once, at entry,
to an equal share of the allocation, and held at that quantity until its rule
says to leave: no partial resizing. That keeps one position one trade (the
ledger's unit, see ``Portfolio.close_position``) and the turnover — which is
the whole cost — at what the rule implies and nothing more.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

DAY_MS = 86_400_000


def trend_wants(closes: Sequence[float], sma_days: int) -> bool | None:
    """Long if the last close is above its ``sma_days`` simple average.

    ``None`` when there is not enough history: "no opinion" is not "flat"
    (invariant 10). The caller keeps whatever it holds.
    """
    if len(closes) < sma_days:
        return None
    window = closes[-sma_days:]
    if any(not math.isfinite(c) or c <= 0 for c in window):
        return None
    return closes[-1] > sum(window) / sma_days


def momentum(closes: Sequence[float], lookback_days: int) -> float | None:
    """Return over the last ``lookback_days`` closed days, or ``None``."""
    if len(closes) < lookback_days + 1:
        return None
    then, now = closes[-lookback_days - 1], closes[-1]
    if not (math.isfinite(then) and math.isfinite(now)) or then <= 0:
        return None
    return now / then - 1.0


def xsmom_wants(
    closes: Mapping[str, Sequence[float]],
    held: set[str],
    *,
    lookback_days: int,
    top_k: int,
    hold_rank: int,
) -> list[str]:
    """The names to hold after this rebalance, best first.

    Rank every symbol with enough history by its ``lookback_days`` return. A
    name already held stays while it ranks within ``hold_rank`` and its return
    is still positive — the buffer keeps a name sitting at rank 5 from being
    sold and rebought every week, which would pay the round trip for nothing.
    Free slots go to the best-ranked names with a positive return. Fewer than
    ``top_k`` positive names means fewer holdings and the rest in cash: the
    absolute filter is what keeps a falling market from being bought because
    something has to rank first.
    """
    scored = {s: momentum(c, lookback_days) for s, c in closes.items()}
    ranked = sorted(
        (s for s, m in scored.items() if m is not None), key=lambda s: scored[s], reverse=True
    )
    rank = {s: i + 1 for i, s in enumerate(ranked)}

    keep = [
        s for s in ranked
        if s in held and rank[s] <= hold_rank and (scored[s] or 0.0) > 0.0
    ]
    for symbol in ranked:
        if len(keep) >= top_k:
            break
        if symbol not in keep and (scored[symbol] or 0.0) > 0.0 and rank[symbol] <= top_k:
            keep.append(symbol)
    # Unscored held names (history gap) are kept: no opinion is not a sale.
    # Held names never exceed ``top_k``, so neither does the result.
    keep.extend(s for s in held if scored.get(s) is None and s not in keep)
    return keep


def funding_sum(
    settlements: Sequence[tuple[int, float]], now_ms: int, window_days: int
) -> float | None:
    """Sum of the funding rates settled in the last ``window_days``.

    A sum rather than a mean because Bybit settles some contracts every 8 hours
    and others every 4 or 1: a mean per settlement would rank an hourly
    contract's small rates against an 8-hourly one's large ones. What carry
    earns is the sum. ``None`` with no settlement in the window.
    """
    since = now_ms - window_days * DAY_MS
    rates = [rate for ts, rate in settlements if since < ts <= now_ms]
    if not rates:
        return None
    return float(sum(rates))


def carry_wants(
    sums: Mapping[str, float | None],
    held: set[str],
    *,
    enter_sum: float,
    exit_sum: float,
    max_pairs: int,
) -> list[str]:
    """The pairs to hold: spot long + perp short on each name, best first.

    Enter when the trailing funding sum clears ``enter_sum`` — the yield that
    pays back the round trip within the expected hold. Stay until it falls to
    ``exit_sum`` or below. The gap between the two is the hysteresis that stops
    a pair being closed and reopened as funding wobbles around one threshold.
    """
    keep = [s for s in held if (sums.get(s) is None) or (sums[s] or 0.0) > exit_sum]
    candidates = sorted(
        (s for s, v in sums.items() if v is not None and v >= enter_sum and s not in keep),
        key=lambda s: sums[s] or 0.0,
        reverse=True,
    )
    for symbol in candidates:
        if len(keep) >= max_pairs:
            break
        keep.append(symbol)
    return keep
