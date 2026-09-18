"""Leverage promotion gate.

The agreement with the operator: the ceiling is theirs (``risk.max_leverage``,
default 1.0, hard-capped at 2.0 by config validation), and whether leverage is
used at all is the system's call within that ceiling.

So leverage is not a setting that takes effect when flipped. Raising the config
ceiling only makes leverage *possible*; this gate decides whether it is
*earned*, and it is deliberately hard to satisfy: ninety days of live or demo
trading, a hundred trades, and risk-adjusted results that held up out of sample.

The gate can only ever lower the effective leverage. Nothing in the codebase can
raise it above the operator's ceiling.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from tbot.core.clock import now_ms
from tbot.core.logging import get_logger

log = get_logger("risk.leverage")

MS_PER_DAY = 86_400_000


@dataclass(slots=True)
class PromotionCriteria:
    """What has to be true before leverage above 1.0 is permitted at all."""

    min_days: int = 90
    min_trades: int = 100
    min_profit_factor: float = 1.3
    min_expectancy_r: float = 0.1
    max_drawdown: float = 0.15
    require_oos_validation: bool = True


@dataclass(slots=True)
class PromotionStatus:
    """Current standing against the criteria, with the reasons spelled out."""

    eligible: bool
    granted_leverage: float
    days_running: float
    trades: int
    profit_factor: float
    expectancy_r: float
    max_drawdown: float
    oos_validated: bool
    failures: list[str] = field(default_factory=list)
    evaluated_ts: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


class LeverageGate:
    """Evaluates and persists leverage eligibility."""

    def __init__(self, path: Path, criteria: PromotionCriteria | None = None):
        self.path = Path(path)
        self.criteria = criteria or PromotionCriteria()

    def evaluate(
        self,
        *,
        first_trade_ts: int | None,
        trades: int,
        profit_factor: float,
        expectancy_r: float,
        max_drawdown: float,
        oos_validated: bool,
        user_ceiling: float,
        now: int | None = None,
    ) -> PromotionStatus:
        """Check every criterion and return the leverage actually allowed."""
        now = now or now_ms()
        # `is not None`, not truthiness: epoch (0) is a valid timestamp and
        # treating it as "no trades yet" would silently fail every evaluation.
        days = (now - first_trade_ts) / MS_PER_DAY if first_trade_ts is not None else 0.0
        failures: list[str] = []

        if days < self.criteria.min_days:
            failures.append(f"{days:.0f}/{self.criteria.min_days} days of track record")
        if trades < self.criteria.min_trades:
            failures.append(f"{trades}/{self.criteria.min_trades} trades")
        if profit_factor < self.criteria.min_profit_factor:
            failures.append(
                f"profit factor {profit_factor:.2f} < {self.criteria.min_profit_factor}"
            )
        if expectancy_r < self.criteria.min_expectancy_r:
            failures.append(f"expectancy {expectancy_r:.3f}R < {self.criteria.min_expectancy_r}R")
        if max_drawdown > self.criteria.max_drawdown:
            failures.append(f"max drawdown {max_drawdown:.1%} > {self.criteria.max_drawdown:.0%}")
        if self.criteria.require_oos_validation and not oos_validated:
            failures.append("no out-of-sample validation on record")

        eligible = not failures
        # Even a clean sheet cannot exceed the operator's ceiling. This min() is
        # the line that keeps the decision theirs.
        granted = min(user_ceiling, 2.0) if eligible else 1.0

        status = PromotionStatus(
            eligible=eligible,
            granted_leverage=granted,
            days_running=round(days, 1),
            trades=trades,
            profit_factor=round(profit_factor, 3),
            expectancy_r=round(expectancy_r, 4),
            max_drawdown=round(max_drawdown, 4),
            oos_validated=oos_validated,
            failures=failures,
            evaluated_ts=now,
        )
        self.save(status)
        return status

    def save(self, status: PromotionStatus) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(status.to_dict(), indent=2), encoding="utf-8")

    def load(self) -> PromotionStatus | None:
        if not self.path.exists():
            return None
        try:
            return PromotionStatus(**json.loads(self.path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, TypeError) as exc:
            log.warning("promotion_file_unreadable", error=str(exc))
            return None

    def allowed_leverage(self, user_ceiling: float) -> float:
        """Leverage permitted right now.

        Defaults to 1.0 whenever the gate file is missing or unreadable — an
        absent record is not evidence of a passed gate.
        """
        if user_ceiling <= 1.0:
            return 1.0
        status = self.load()
        if status is None or not status.eligible:
            return 1.0
        return min(user_ceiling, status.granted_leverage, 2.0)

    def explain(self, user_ceiling: float) -> str:
        if user_ceiling <= 1.0:
            return "leverage ceiling is 1.0x (spot only) — set risk.max_leverage to raise it"
        status = self.load()
        if status is None:
            return "no promotion record yet — leverage stays at 1.0x"
        if status.eligible:
            return f"gate passed: {status.granted_leverage:.1f}x permitted (ceiling {user_ceiling:.1f}x)"
        return "gate not passed: " + "; ".join(status.failures)
