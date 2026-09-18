"""Redaction: what may leave this machine.

Free provider tiers generally reserve the right to train on submitted data, so
the rule here is not "avoid obvious secrets" but "send the minimum that answers
the question". Prices and percentages are fine; balances, keys and account
identifiers are not — and a position size in dollars reveals the account size
just as surely as the balance would.

Applied as a whitelist. A blacklist protects against the leaks someone thought
of; a whitelist protects against the ones they did not.
"""

from __future__ import annotations

import re
from typing import Any

# Anything matching these never leaves, wherever it appears in a payload.
FORBIDDEN_KEYS = {
    "api_key",
    "apikey",
    "secret",
    "api_secret",
    "token",
    "password",
    "passphrase",
    "balance",
    "cash",
    "equity",
    "account_id",
    "uid",
    "chat_id",
    "email",
    "wallet",
    "quantity",
    "notional",
    "pnl",
    "fee",
    "fees",
    "risk_amount",
    "starting_balance",
}

_SECRET_PATTERN = re.compile(
    r"(?i)\b(?:[A-Za-z0-9_\-]{32,}|sk-[A-Za-z0-9]{16,}|AIza[A-Za-z0-9_\-]{20,})\b"
)


def redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return _SECRET_PATTERN.sub("[REDACTED]", value)
    return value


def redact_dict(payload: dict[str, Any], allowed: set[str] | None = None) -> dict[str, Any]:
    """Strip forbidden keys and, when ``allowed`` is given, keep only those.

    Nested dicts and lists are walked, because a forbidden key one level down is
    just as much of a leak as one at the top.
    """
    out: dict[str, Any] = {}

    for key, value in payload.items():
        lowered = key.lower()
        if lowered in FORBIDDEN_KEYS:
            continue
        if allowed is not None and lowered not in allowed:
            continue
        if isinstance(value, dict):
            out[key] = redact_dict(value, None)
        elif isinstance(value, list):
            out[key] = [
                redact_dict(v, None) if isinstance(v, dict) else redact_value(v) for v in value
            ]
        else:
            out[key] = redact_value(value)

    return out


def assert_clean(payload: dict[str, Any]) -> None:
    """Raise if anything forbidden survived. Called before every send."""

    def walk(node: Any, path: str = "") -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key.lower() in FORBIDDEN_KEYS:
                    raise ValueError(
                        f"redaction failed: forbidden key {path}{key!r} in LLM payload"
                    )
                walk(value, f"{path}{key}.")
        elif isinstance(node, list):
            for item in node:
                walk(item, path)
        elif isinstance(node, str) and _SECRET_PATTERN.search(node):
            raise ValueError(f"redaction failed: secret-like string at {path}")

    walk(payload)
