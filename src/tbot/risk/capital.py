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
