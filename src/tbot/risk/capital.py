"""What the account size actually permits.

A small account does not simply trade smaller — it silently trades a *different
universe*. Risk-correct sizing on $100 produces a position below the exchange
minimum whenever the stop is wide, so volatile symbols get skipped while quiet
ones go through. Nothing announces this; the trades just never appear, and the
strategy being run is not the one that was backtested.

So it is computed and reported at startup instead of being discovered months
later in a report nobody thought to generate.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from tbot.core.config import Config
from tbot.core.logging import get_logger

log = get_logger("risk.capital")


@dataclass(slots=True)
class CapitalDiagnostic:
    equity: float
    risk_per_trade: float
    risk_amount: float
    min_notional: float
    max_stop_pct: float
    usable: bool
    note: str

    def summary(self) -> str:
        return (
            f"{self.equity:.0f} {'' if self.usable else '(TOO SMALL) '}"
            f"risks {self.risk_amount:.2f}/trade; "
            f"widest usable stop {self.max_stop_pct:.1%}"
        )


def diagnose(config: Config, equity: float, min_notional: float = 5.0) -> CapitalDiagnostic:
    """Widest stop the account can express at the intended risk.

    The chain is: risk amount / stop distance = quantity, and quantity x price
    must clear the venue minimum. Rearranged, the widest stop that still
    produces a legal order is ``risk_amount / min_notional``.
    """
    risk_amount = equity * config.risk.risk_per_trade
    required = min_notional * config.execution.min_notional_buffer
    max_stop_pct = risk_amount / required if required > 0 else 1.0

    if max_stop_pct >= 0.25:
        note = "comfortable — almost any setup can be sized correctly"
        usable = True
    elif max_stop_pct >= 0.10:
        note = (
            f"stops wider than {max_stop_pct:.0%} cannot be sized at this risk level, "
            f"so volatile symbols will be skipped without appearing as rejections"
        )
        usable = True
    else:
        note = (
            f"only stops under {max_stop_pct:.1%} are reachable — most setups will be "
            f"silently skipped and the live universe will not match the backtest"
        )
        usable = False

    return CapitalDiagnostic(
        equity=equity,
        risk_per_trade=config.risk.risk_per_trade,
        risk_amount=risk_amount,
        min_notional=required,
        max_stop_pct=max_stop_pct,
        usable=usable,
        note=note,
    )


@dataclass(slots=True)
class MinimumCapital:
    """Smallest balance at which the system trades the strategy it was tested on."""

    coverage_target: float
    required_stop_pct: float
    minimum_equity: float
    comfortable_equity: float
    min_notional: float
    risk_per_trade: float
    observed_stops: int

    def summary(self) -> str:
        return (
            f"minimum {self.minimum_equity:.0f}, comfortable {self.comfortable_equity:.0f} "
            f"(covers {self.coverage_target:.0%} of setups at {self.risk_per_trade:.2%} risk)"
        )


def minimum_viable_equity(
    config: Config,
    stop_distributions: list[float],
    min_notional: float = 5.0,
    coverage: float = 0.95,
    comfortable_coverage: float = 0.99,
    vol_scales: list[float] | None = None,
) -> MinimumCapital:
    """Smallest account that can size the strategy the backtest measured.

    Derived from the universe's own distributions rather than assumed. The chain
    is fixed by arithmetic:

        risk_amount = equity x risk_per_trade x vol_scale
        quantity    = risk_amount / stop_distance
        notional    = quantity x price = risk_amount / stop_pct

    and the venue rejects anything below ``min_notional``.

    ``vol_scales`` matters more than it looks, and omitting it makes this
    answer optimistic. Volatility targeting shrinks position size on volatile
    symbols — precisely the symbols that also carry the widest stops. The two
    effects push the same way and compound: a symbol at 200% annualised
    volatility gets both a wide stop *and* a 0.4x size multiplier, so its
    notional is roughly a fifth of what stop distance alone would suggest.

    Pass the observed (stop, scale) pairs to get the real requirement. Without
    them this returns the stop-only figure, which is a floor rather than an
    estimate.

    Below the returned minimum the bot does not simply trade smaller — it trades
    a *different, quieter subset* of the universe, because wide-stop and
    high-volatility setups are silently skipped. That is not a scaled-down
    version of the tested strategy; it is a different one, and its backtest does
    not apply to it.
    """
    if not stop_distributions:
        return MinimumCapital(coverage, 0.0, 0.0, 0.0, min_notional, config.risk.risk_per_trade, 0)

    stops = np.asarray(stop_distributions, dtype="float64")
    valid = np.isfinite(stops) & (stops > 0)

    if vol_scales is not None and len(vol_scales) == len(stops):
        scales = np.asarray(vol_scales, dtype="float64")
        valid &= np.isfinite(scales) & (scales > 0)
        stops, scales = stops[valid], scales[valid]
        # Effective requirement per observation, accounting for both effects.
        effective = stops / scales
    else:
        stops = stops[valid]
        effective = stops

    required = min_notional * config.execution.min_notional_buffer

    def equity_for(quantile: float) -> float:
        return required * float(np.quantile(effective, quantile)) / config.risk.risk_per_trade

    return MinimumCapital(
        coverage_target=coverage,
        required_stop_pct=float(np.quantile(stops, coverage)),
        minimum_equity=equity_for(coverage),
        comfortable_equity=equity_for(comfortable_coverage),
        min_notional=required,
        risk_per_trade=config.risk.risk_per_trade,
        observed_stops=int(stops.size),
    )


def report(config: Config, equity: float, min_notional: float = 5.0) -> CapitalDiagnostic:
    """Diagnose and log. Warns rather than blocks: it is the operator's money."""
    diagnostic = diagnose(config, equity, min_notional)

    log.info(
        "capital_diagnostic",
        equity=round(equity, 2),
        risk_amount=round(diagnostic.risk_amount, 4),
        max_stop_pct=round(diagnostic.max_stop_pct, 4),
        usable=diagnostic.usable,
    )
    if not diagnostic.usable:
        log.warning("account_too_small_for_intended_risk", note=diagnostic.note)

    return diagnostic
