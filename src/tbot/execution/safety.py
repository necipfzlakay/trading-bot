"""Pre-flight and runtime safety checks for live trading.

Each of these exists because of a specific way a live bot loses money that a
backtest cannot show:

* **Withdrawal-enabled API keys.** A key that can move funds turns any
  compromise — a leaked ``.env``, a bad dependency — from "someone can trade my
  account badly" into "someone can empty it". Checked before the first order.
* **Slippage drift.** The backtest assumed a cost. If reality is worse, every
  result that justified going live was optimistic. Measured per fill rather than
  assumed to still hold.
* **Implausible equity jumps.** Bad data, a wrong symbol or a misread balance
  can make the account look like it moved 40% in one bar. Sizing off that number
  turns a data error into a real loss.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from tbot.core.logging import get_logger

log = get_logger("execution.safety")


@dataclass(slots=True)
class KeyPermissionReport:
    can_trade: bool
    can_withdraw: bool
    read_only: bool
    ip_restricted: bool
    expires_ts: int | None = None
    raw: dict = field(default_factory=dict)
    error: str | None = None

    @property
    def safe_for_trading(self) -> bool:
        return self.can_trade and not self.can_withdraw

    def summary(self) -> str:
        if self.error:
            return f"could not read key permissions: {self.error}"
        parts = [f"trade={self.can_trade}", f"withdraw={self.can_withdraw}"]
        if self.ip_restricted:
            parts.append("ip-restricted")
        return " ".join(parts)


def check_key_permissions(exchange) -> KeyPermissionReport:
    """Read what the API key is allowed to do.

    Bybit exposes this at ``/v5/user/query-api``. A key that can withdraw is
    refused for live trading: this bot never needs that permission, so its
    presence is pure downside.
    """
    try:
        response = exchange.privateGetV5UserQueryApi()
        result = response.get("result", {}) or {}
        permissions = result.get("permissions", {}) or {}
    except Exception as exc:  # noqa: BLE001 — any failure here means "unknown"
        return KeyPermissionReport(
            can_trade=False,
            can_withdraw=False,
            read_only=True,
            ip_restricted=False,
            error=str(exc)[:200],
        )

    wallet = permissions.get("Wallet", []) or []
    spot = permissions.get("Spot", []) or []
    contract = permissions.get("ContractTrade", []) or []

    return KeyPermissionReport(
        can_trade=bool(spot or contract),
        can_withdraw=any("Withdraw" in str(p) for p in wallet),
        read_only=str(result.get("readOnly", "")) in ("1", "True", "true"),
        ip_restricted=bool(result.get("ips") and result["ips"] != ["*"]),
        expires_ts=int(result["expiredAt"]) if result.get("expiredAt") else None,
        raw=result,
    )


def assert_key_is_safe(exchange, allow_withdraw: bool = False) -> KeyPermissionReport:
    """Refuse to trade live with a key that can move funds off the exchange."""
    report = check_key_permissions(exchange)

    if report.error:
        log.warning("key_permissions_unknown", error=report.error)
        return report

    if report.can_withdraw and not allow_withdraw:
        raise RuntimeError(
            "This API key has withdrawal permission. The bot never needs it, and a key "
            "that can move funds turns any leak into a total loss instead of a bad trade. "
            "Create a key with Spot Trade + Read only, and enable an IP whitelist."
        )

    if not report.can_trade:
        raise RuntimeError("This API key cannot trade — check its permissions on Bybit.")

    if not report.ip_restricted:
        log.warning(
            "key_not_ip_restricted",
            advice="add an IP whitelist on Bybit; it costs nothing and blocks remote reuse",
        )

    log.info("key_permissions_ok", summary=report.summary())
    return report


@dataclass(slots=True)
class SlippageTracker:
    """Compares expected fill prices against what the venue actually gave.

    The backtest's cost assumption is the single number most likely to be wrong
    in an optimistic direction. This turns it from an assumption into a
    measurement, and says plainly when reality is worse than the model.
    """

    assumed_bps: float
    samples: list[float] = field(default_factory=list)
    max_samples: int = 500

    def record(self, expected_price: float, actual_price: float, side_sign: int) -> float:
        """Log one fill's slippage in basis points. Positive = worse than expected."""
        if expected_price <= 0:
            return 0.0
        drift = (actual_price - expected_price) / expected_price * side_sign * 10_000.0
        self.samples.append(drift)
        if len(self.samples) > self.max_samples:
            self.samples.pop(0)
        return drift

    @property
    def median_bps(self) -> float:
        return float(np.median(self.samples)) if self.samples else 0.0

    @property
    def p90_bps(self) -> float:
        return float(np.percentile(self.samples, 90)) if self.samples else 0.0

    def exceeds_assumption(self, min_samples: int = 20) -> bool:
        """True once there is enough evidence that real costs beat the model."""
        return len(self.samples) >= min_samples and self.median_bps > self.assumed_bps

    def summary(self) -> dict:
        return {
            "samples": len(self.samples),
            "assumed_bps": self.assumed_bps,
            "median_bps": round(self.median_bps, 2),
            "p90_bps": round(self.p90_bps, 2),
            "worse_than_assumed": self.exceeds_assumption(),
        }


def equity_jump_is_implausible(previous: float, current: float, max_move: float = 0.35) -> bool:
    """Guard against sizing off a number that cannot be real.

    Between two bars a spot account with capped exposure cannot plausibly move
    by a third. When it appears to, the cause is almost always bad data, a
    misread balance or a symbol mix-up — and trading on it converts a data error
    into a real loss.
    """
    if previous <= 0:
        return False
    return abs(current - previous) / previous > max_move
