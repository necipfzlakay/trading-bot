"""Position sizing: risk-first, volatility-normalised, exchange-aware.

Size is derived from the stop, never chosen first. The chain is:

    risk amount (1R) -> stop distance -> quantity

so every position risks the same fraction of equity regardless of how volatile
the symbol is. Volatility targeting then adjusts on top, so a calm symbol and a
wild one contribute comparable risk to the portfolio rather than comparable
notional.

The last step is the one that actually rejects trades in practice: exchange
minimums. With a $100 account and a $5 minimum notional, a correctly sized
position is often below what the venue will accept — and taking it anyway by
rounding up would silently multiply the intended risk.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from tbot.core.config import RiskConfig
from tbot.core.logging import get_logger
from tbot.core.models import MarketSpec, NoTradeReason, Side

log = get_logger("risk.sizing")


@dataclass(slots=True)
class SizingResult:
    quantity: float
    notional: float
    risk_amount: float
    stop_distance: float
    vol_scale: float
    rejected: NoTradeReason | None = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.rejected is None and self.quantity > 0


def volatility_scale(
    recent_returns: np.ndarray,
    target_annual_vol: float,
    bars_per_year: int,
    min_scale: float,
    max_scale: float,
) -> float:
    """Scale factor that pushes realised volatility toward the target.

    Bounded in both directions: unbounded scaling would size a dead-quiet symbol
    into an enormous position right before it stops being quiet.
    """
    if recent_returns.size < 5:
        return 1.0
    realised = float(np.std(recent_returns, ddof=1)) * math.sqrt(bars_per_year)
    if realised <= 0:
        return 1.0
    return float(np.clip(target_annual_vol / realised, min_scale, max_scale))


def size_position(
    equity: float,
    risk_amount: float,
    entry_price: float,
    stop_price: float,
    spec: MarketSpec,
    config: RiskConfig,
    side: Side = Side.BUY,
    recent_returns: np.ndarray | None = None,
    bars_per_year: int = 8760,
    available_cash: float | None = None,
    leverage: float = 1.0,
    min_notional_buffer: float = 1.15,
) -> SizingResult:
    """Turn a risk budget and a stop into an executable quantity.

    Returns a rejection rather than a clipped size whenever the trade cannot be
    placed as intended. Silently shrinking a position to fit would mean the
    stop no longer corresponds to 1R, and every performance number downstream
    would be measured in a unit that quietly changed.
    """
    stop_distance = abs(entry_price - stop_price)
    if stop_distance <= 0:
        return SizingResult(0, 0, 0, 0, 1.0, NoTradeReason.BELOW_THRESHOLD, "zero stop distance")

    vol_scale = 1.0
    if recent_returns is not None:
        vol_scale = volatility_scale(
            recent_returns,
            config.vol_target_annual,
            bars_per_year,
            config.vol_scale_min,
            config.vol_scale_max,
        )

    effective_risk = risk_amount * vol_scale
    quantity = effective_risk / stop_distance

    # Exchange precision: round DOWN so rounding can never increase risk.
    if spec.amount_step > 0:
        quantity = math.floor(quantity / spec.amount_step) * spec.amount_step

    notional = quantity * entry_price
    max_notional = equity * config.max_portfolio_exposure * leverage
    if notional > max_notional:
        quantity = max_notional / entry_price
        if spec.amount_step > 0:
            quantity = math.floor(quantity / spec.amount_step) * spec.amount_step
        notional = quantity * entry_price
        effective_risk = quantity * stop_distance

    if available_cash is not None and notional > available_cash * leverage:
        return SizingResult(
            0,
            notional,
            effective_risk,
            stop_distance,
            vol_scale,
            NoTradeReason.INSUFFICIENT_BALANCE,
            f"needs {notional:.2f}, cash {available_cash:.2f}",
        )

    if quantity < spec.min_amount or quantity <= 0:
        return SizingResult(
            0,
            notional,
            effective_risk,
            stop_distance,
            vol_scale,
            NoTradeReason.MIN_NOTIONAL,
            f"qty {quantity:.8f} below exchange minimum {spec.min_amount:.8f}",
        )

    required = spec.min_notional * min_notional_buffer if spec.min_notional else 0.0
    if required and notional < required:
        # Bumping the size to clear the minimum would multiply the intended
        # risk. Skipping the trade is the only option that keeps 1R meaning 1R.
        return SizingResult(
            0,
            notional,
            effective_risk,
            stop_distance,
            vol_scale,
            NoTradeReason.MIN_NOTIONAL,
            f"notional {notional:.2f} below minimum {required:.2f} "
            f"(risk-correct size is too small for this venue)",
        )

    return SizingResult(
        quantity=quantity,
        notional=notional,
        risk_amount=quantity * stop_distance,
        stop_distance=stop_distance,
        vol_scale=vol_scale,
        detail=f"vol_scale={vol_scale:.2f} risk={quantity * stop_distance:.4f}",
    )


def correlation_conflict(
    candidate: str,
    open_symbols: list[str],
    correlations: dict[tuple[str, str], float],
    threshold: float,
) -> bool:
    """Whether a candidate duplicates risk already held.

    In crypto, five different coins are frequently five expressions of one bet
    on Bitcoin. Without this check the portfolio limit counts positions while
    the actual exposure is concentrated.
    """
    for held in open_symbols:
        key = (candidate, held) if candidate < held else (held, candidate)
        rho = correlations.get(key)
        if rho is not None and abs(rho) >= threshold:
            return True
    return False
