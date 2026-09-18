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
from datetime import UTC, datetime

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

    @property
    def expires_iso(self) -> str:
        if not self.expires_ts:
            return "never"
        return datetime.fromtimestamp(self.expires_ts / 1000, tz=UTC).strftime("%Y-%m-%d")

    def summary(self) -> str:
        if self.error:
            return f"could not read key permissions: {self.error}"
        parts = [f"trade={self.can_trade}", f"withdraw={self.can_withdraw}"]
        if self.ip_restricted:
            parts.append("ip-restricted")
        parts.append(f"expires={self.expires_iso}")
        return " ".join(parts)


# Bybit has returned this field as an ISO-8601 string, as epoch seconds and as
# epoch milliseconds depending on the endpoint and the account type. It is
# informational, so an unrecognised shape must never break the permission check
# that surrounds it — that check is the one standing between a leaked key and an
# emptied account.
def _parse_expiry(raw) -> int | None:
    """Epoch milliseconds, or None if the venue sent something unexpected."""
    if raw in (None, "", 0, "0"):
        return None

    text = str(raw).strip()
    try:
        if text.replace(".", "", 1).isdigit():
            value = float(text)
            # Seconds and milliseconds are told apart by magnitude: anything
            # below ~1e11 is a second-resolution timestamp for any plausible date.
            return int(value * 1000) if value < 1e11 else int(value)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return int(parsed.timestamp() * 1000)
    except (ValueError, OverflowError):
        log.warning("key_expiry_unparsed", value=text[:64])
        return None


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
        expires_ts=_parse_expiry(result.get("expiredAt")),
        raw=result,
    )


def assert_key_is_safe(
    exchange,
    allow_withdraw: bool = False,
    ip_whitelist_acknowledged: bool = False,
) -> KeyPermissionReport:
    """Refuse to trade live with a key that can move funds off the exchange.

    ``ip_whitelist_acknowledged`` downgrades the missing-whitelist warning to an
    informational line. It does **not** change what is checked, and it cannot
    touch the two conditions that actually refuse to start (withdrawal
    permission, expired key) — those have no acknowledgement flag by design.

    The reason it exists at all: a warning nothing can clear is a warning that
    gets ignored, and then the next real one is ignored with it. On a dynamic
    IP a whitelist is not merely inconvenient, it is wrong — it locks the bot
    out every time the address changes, mid-position. An operator who has
    weighed that has made a decision, and a decision should read as a decision
    rather than as an outstanding defect.
    """
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
        if ip_whitelist_acknowledged:
            log.info(
                "key_not_ip_restricted_acknowledged",
                residual_risk="a leaked key could be used from anywhere",
                bounded_by="the key cannot withdraw, so the worst case is unwanted "
                "trades on this account, not loss of funds",
            )
        else:
            log.warning(
                "key_not_ip_restricted",
                advice="add an IP whitelist on Bybit; it costs nothing and blocks "
                "remote reuse. On a dynamic IP set security.ip_whitelist_acknowledged "
                "instead — a warning that cannot be cleared trains you to ignore the channel",
            )

    if report.expires_ts:
        days = (report.expires_ts - datetime.now(UTC).timestamp() * 1000) / 86_400_000
        if days <= 0:
            raise RuntimeError(
                f"This API key expired on {report.expires_iso}. Orders would be rejected "
                f"and open positions would stop being managed. Create a new key first."
            )
        if days <= 14:
            log.warning("key_expires_soon", days=round(days, 1), expires=report.expires_iso)

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
