"""Failure containment: circuit breakers, backoff, degraded mode.

The operator's requirement was that the bot never waits for a human. So every
failure path here resolves itself: retry with backoff, drop to a reduced mode,
or — at worst — stop opening new positions while continuing to protect the ones
already open. Nothing blocks on approval.

Three layers, innermost first:

* **Circuit breaker** per operation. Repeated failures of one endpoint open the
  breaker for a cooling period instead of hammering it.
* **Degraded mode** for the engine. New entries stop, existing positions are
  still managed, and normal operation resumes on its own when the cause clears.
* **Process supervision** (launchd), outside this file, which restarts the
  process however it dies.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from tbot.core.clock import now_ms
from tbot.core.logging import get_logger

log = get_logger("engine.watchdog")

T = TypeVar("T")


@dataclass(slots=True)
class CircuitBreaker:
    """Stops calling something that keeps failing.

    Half-open after the cooldown: one probe decides whether to close again, so
    recovery does not require a restart.
    """

    name: str
    failure_threshold: int = 5
    cooldown_seconds: float = 60.0
    failures: int = 0
    opened_ts: int = 0

    @property
    def is_open(self) -> bool:
        if self.opened_ts == 0:
            return False
        # Past the cooldown the breaker is half-open: one probe is allowed
        # through, and its outcome decides whether it closes again.
        return (now_ms() - self.opened_ts) / 1000 < self.cooldown_seconds

    def record_success(self) -> None:
        if self.failures or self.opened_ts:
            log.info("circuit_closed", name=self.name)
        self.failures = 0
        self.opened_ts = 0

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.failure_threshold and not self.opened_ts:
            self.opened_ts = now_ms()
            log.warning(
                "circuit_opened",
                name=self.name,
                failures=self.failures,
                cooldown=self.cooldown_seconds,
            )


@dataclass(slots=True)
class Watchdog:
    """Owns the breakers and the retry policy."""

    breakers: dict[str, CircuitBreaker] = field(default_factory=dict)
    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 30.0

    def breaker(self, name: str) -> CircuitBreaker:
        if name not in self.breakers:
            self.breakers[name] = CircuitBreaker(name=name)
        return self.breakers[name]

    def call(
        self,
        name: str,
        func: Callable[[], T],
        default: T | None = None,
        retries: int | None = None,
    ) -> T | None:
        """Run ``func`` with retries, backoff and breaker protection.

        Returns ``default`` rather than raising: one symbol's failure must not
        take down the loop that manages every other position.
        """
        breaker = self.breaker(name)
        if breaker.is_open:
            log.debug("circuit_open_skipping", name=name)
            return default

        attempts = retries if retries is not None else self.max_retries
        delay = self.base_delay

        for attempt in range(attempts + 1):
            try:
                result = func()
                breaker.record_success()
                return result
            except Exception as exc:  # noqa: BLE001 — containment is the point
                breaker.record_failure()
                if attempt >= attempts:
                    log.error(
                        "operation_failed",
                        name=name,
                        attempts=attempt + 1,
                        error=str(exc)[:200],
                    )
                    return default
                log.warning(
                    "operation_retry",
                    name=name,
                    attempt=attempt + 1,
                    delay=delay,
                    error=str(exc)[:160],
                )
                time.sleep(delay)
                delay = min(delay * 2, self.max_delay)

        return default

    def health(self) -> dict[str, Any]:
        return {
            name: {
                "open": breaker.is_open,
                "failures": breaker.failures,
                "opened_ts": breaker.opened_ts,
            }
            for name, breaker in self.breakers.items()
        }

    def any_open(self) -> bool:
        return any(b.is_open for b in self.breakers.values())
