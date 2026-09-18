"""The LLM boundary.

These tests exist because the iron rule — an LLM may only reduce risk — is a
property of the code, not a request in a prompt. If any of these fail, the model
has been handed influence it was never meant to have.
"""

import pytest

from tbot.core.config import load_config, load_secrets
from tbot.llm.provider import LLMProvider, _parse_json
from tbot.llm.redact import FORBIDDEN_KEYS, assert_clean, redact_dict
from tbot.llm.roles import EventGuard, build_roles

# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------


def test_forbidden_keys_never_leave_the_machine():
    payload = {
        "symbol": "BTC/USDT",
        "api_key": "abc",
        "balance": 1234.56,
        "equity": 99.0,
        "quantity": 0.01,
        "regime": "trend_up",
    }
    safe = redact_dict(payload)
    assert safe == {"symbol": "BTC/USDT", "regime": "trend_up"}


def test_redaction_walks_nested_structures():
    payload = {"outer": {"secret": "x", "ok": 1}, "list": [{"balance": 5, "keep": 2}]}
    safe = redact_dict(payload)
    assert "secret" not in safe["outer"]
    assert "balance" not in safe["list"][0]
    assert safe["list"][0]["keep"] == 2


def test_position_size_is_forbidden_because_it_reveals_account_size():
    assert "quantity" in FORBIDDEN_KEYS
    assert "notional" in FORBIDDEN_KEYS
    assert "pnl" in FORBIDDEN_KEYS


def test_api_key_shaped_strings_are_scrubbed_from_free_text():
    payload = {"note": "failed with key AIzaSyD1234567890abcdefghijklmnopqrstu"}
    safe = redact_dict(payload)
    assert "AIzaSy" not in safe["note"]


def test_assert_clean_raises_rather_than_sending():
    with pytest.raises(ValueError, match="redaction failed"):
        assert_clean({"api_secret": "leaked"})
    with pytest.raises(ValueError, match="redaction failed"):
        assert_clean({"nested": {"balance": 100}})
    assert_clean({"symbol": "BTC/USDT", "score": 72})  # clean payload passes


# --------------------------------------------------------------------------
# The provider refuses to work without permission
# --------------------------------------------------------------------------


def test_provider_is_unavailable_without_keys():
    assert not LLMProvider().available


def test_provider_stops_after_repeated_failures():
    provider = LLMProvider(gemini_key="x", circuit_breaker_failures=2)
    provider._failures = 2
    assert not provider.available


def test_provider_respects_the_daily_budget():
    from tbot.core.clock import now_ms

    provider = LLMProvider(gemini_key="x", max_calls_per_day=2)
    provider._day_start = now_ms() // 86_400_000  # today, so the counter is not reset
    provider._calls_today = 2
    assert not provider.available


def test_daily_budget_resets_on_a_new_day():
    from tbot.core.clock import now_ms

    provider = LLMProvider(gemini_key="x", max_calls_per_day=2)
    provider._day_start = now_ms() // 86_400_000 - 1  # yesterday
    provider._calls_today = 2
    assert provider.available


def test_unavailable_provider_returns_a_failure_not_an_exception():
    response = LLMProvider().ask("hi", {"answer"})
    assert not response.ok
    assert response.data is None


def test_free_text_is_not_accepted_as_a_decision():
    assert _parse_json("I think you should not trade today") is None
    assert _parse_json('{"block_new_entries": false}') == {"block_new_entries": False}
    assert _parse_json('```json\n{"a": 1}\n```') == {"a": 1}


# --------------------------------------------------------------------------
# The roles cannot cross the line
# --------------------------------------------------------------------------


def test_event_guard_fails_open_so_an_outage_cannot_stop_trading():
    """A provider being down must never block the strategy."""
    guard = EventGuard(LLMProvider(), db=None)
    result = guard.check({"symbols": ["BTC/USDT"]})
    assert result.data["block_new_entries"] is False


def test_event_guard_only_honours_high_severity():
    """A model that is merely uneasy does not get to stop the strategy."""

    class Stub(LLMProvider):
        @property
        def available(self):
            return True

        def ask(self, prompt, schema_keys, payload=None, cache_key=None):
            from tbot.llm.provider import LLMResponse

            return LLMResponse(
                True,
                {"block_new_entries": True, "severity": "medium", "reason": "feels risky"},
                "",
                10,
                "stub",
                "stub",
            )

    result = EventGuard(Stub(), db=None).check({})
    assert result.data["block_new_entries"] is False
    assert "downgraded" in result.data


def test_event_guard_schema_has_no_field_that_could_open_a_position():
    """The response shape itself makes "buy this" unrepresentable."""
    assert EventGuard.schema == {"block_new_entries", "severity", "reason"}
    forbidden = {"side", "symbol", "size", "quantity", "leverage", "entry", "stop", "target"}
    assert not (EventGuard.schema & forbidden)


def test_no_roles_are_built_when_llm_is_disabled():
    cfg = load_config("config/base.yaml")
    assert cfg.llm.enabled is False
    assert build_roles(cfg, load_secrets(), None) == {}


def test_enabled_roles_still_need_credentials_to_do_anything():
    cfg = load_config(
        "config/base.yaml",
        overrides={"llm": {"enabled": True, "roles": {"event_guard": {"enabled": True}}}},
    )
    roles = build_roles(cfg, load_secrets(), None)
    assert "event_guard" in roles
    assert not roles["event_guard"].provider.available  # no key, so no influence
