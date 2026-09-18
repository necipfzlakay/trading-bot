"""State snapshots and failure containment."""

import json

from tbot.engine.state import EngineState, RestartTracker, StateWriter
from tbot.engine.watchdog import CircuitBreaker, Watchdog


def test_state_is_written_atomically(tmp_path):
    """A reader polling this file must never catch it half-written."""
    writer = StateWriter(tmp_path / "state.json")
    writer.write(EngineState(mode="paper", status="running", equity=100.0))

    data = json.loads((tmp_path / "state.json").read_text())
    assert data["status"] == "running"
    assert data["equity"] == 100.0
    assert "uptime_human" in data
    assert not list(tmp_path.glob("*.tmp"))  # no temp files left behind


def test_state_round_trips(tmp_path):
    writer = StateWriter(tmp_path / "state.json")
    state = EngineState(
        mode="live", venue="bybit-demo", status="degraded", degraded_reason="stale data"
    )
    writer.write(state)
    assert writer.read()["degraded_reason"] == "stale data"


def test_reading_a_missing_state_file_returns_none(tmp_path):
    assert StateWriter(tmp_path / "nope.json").read() is None


def test_corrupt_state_file_does_not_raise(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json")
    assert StateWriter(path).read() is None


def test_restart_tracker_counts_only_the_recent_window(tmp_path):
    tracker = RestartTracker(tmp_path / "restarts.json", window_seconds=3600)
    for _ in range(3):
        tracker.record()
    assert tracker.count() == 3

    stale = RestartTracker(tmp_path / "restarts.json", window_seconds=0)
    assert stale.count() == 0  # everything has aged out


def test_circuit_opens_after_repeated_failures():
    breaker = CircuitBreaker("api", failure_threshold=3, cooldown_seconds=60)
    assert not breaker.is_open
    for _ in range(3):
        breaker.record_failure()
    assert breaker.is_open


def test_success_closes_the_circuit():
    breaker = CircuitBreaker("api", failure_threshold=2)
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.is_open
    breaker.record_success()
    assert not breaker.is_open
    assert breaker.failures == 0


def test_circuit_goes_half_open_after_the_cooldown():
    """Recovery must not require a restart."""
    breaker = CircuitBreaker("api", failure_threshold=1, cooldown_seconds=0)
    breaker.record_failure()
    assert not breaker.is_open  # cooldown already elapsed -> probe allowed


def test_watchdog_returns_a_default_instead_of_raising():
    """One symbol's failure must not take down the loop managing every other."""
    watchdog = Watchdog(max_retries=1, base_delay=0.001)

    def boom():
        raise RuntimeError("venue down")

    assert watchdog.call("thing", boom, default="fallback") == "fallback"
    assert watchdog.breaker("thing").failures == 2  # initial attempt plus one retry


def test_watchdog_retries_then_succeeds():
    watchdog = Watchdog(max_retries=3, base_delay=0.001)
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectionError("transient")
        return "ok"

    assert watchdog.call("flaky", flaky) == "ok"
    assert attempts["n"] == 3
    assert not watchdog.breaker("flaky").is_open


def test_open_circuit_short_circuits_without_calling():
    watchdog = Watchdog()
    watchdog.breakers["x"] = CircuitBreaker("x", failure_threshold=1, cooldown_seconds=300)
    watchdog.breakers["x"].record_failure()

    calls = {"n": 0}

    def counted():
        calls["n"] += 1
        return 1

    assert watchdog.call("x", counted, default=None) is None
    assert calls["n"] == 0


def test_watchdog_health_is_reportable():
    watchdog = Watchdog()
    watchdog.call("ok", lambda: 1)
    assert watchdog.health()["ok"]["open"] is False
    assert not watchdog.any_open()
