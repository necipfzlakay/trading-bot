"""LLM provider abstraction with hard operational limits.

Every call is capped, cached, budgeted, schema-checked and logged. A provider
outage must be a non-event: the roles that use this layer are advisory, so when
it fails the trading system carries on completely unchanged.

Providers are free tiers, chosen for "no card required" and adequate daily
quota rather than capability — nothing here needs a frontier model.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from tbot.core.clock import now_ms
from tbot.core.logging import get_logger
from tbot.llm.redact import assert_clean, redact_dict

log = get_logger("llm.provider")

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


@dataclass(slots=True)
class LLMResponse:
    ok: bool
    data: dict[str, Any] | None
    raw: str
    latency_ms: int
    provider: str
    model: str
    error: str | None = None


@dataclass(slots=True)
class LLMProvider:
    """Calls a provider, enforces the budget, parses and validates JSON."""

    gemini_key: str = ""
    groq_key: str = ""
    primary: str = "gemini"
    fallback: str = "groq"
    gemini_model: str = "gemini-flash-latest"
    groq_model: str = "llama-3.3-70b-versatile"
    timeout: float = 5.0
    max_calls_per_day: int = 50
    cache_ttl_seconds: int = 900
    circuit_breaker_failures: int = 3

    _calls_today: int = 0
    _day_start: int = 0
    _failures: int = 0
    _cache: dict[str, tuple[int, LLMResponse]] = field(default_factory=dict)

    @property
    def available(self) -> bool:
        """False whenever a call would be pointless — no key, budget spent, or
        too many consecutive failures. Callers treat this as "no opinion"."""
        if self._failures >= self.circuit_breaker_failures:
            return False
        if self._calls_remaining() <= 0:
            return False
        return bool(self._key_for(self.primary) or self._key_for(self.fallback))

    def _key_for(self, provider: str) -> str:
        return {"gemini": self.gemini_key, "groq": self.groq_key}.get(provider, "")

    def _calls_remaining(self) -> int:
        day = now_ms() // 86_400_000
        if day != self._day_start:
            self._day_start = day
            self._calls_today = 0
        return self.max_calls_per_day - self._calls_today

    def ask(
        self,
        prompt: str,
        schema_keys: set[str],
        payload: dict[str, Any] | None = None,
        cache_key: str | None = None,
    ) -> LLMResponse:
        """Send a prompt and return parsed JSON, or a failure.

        ``schema_keys`` are required in the response. Free text is never
        accepted as a decision — a model that answers in prose has not answered.
        """
        if cache_key:
            cached = self._cache.get(cache_key)
            if cached and (now_ms() - cached[0]) / 1000 < self.cache_ttl_seconds:
                return cached[1]

        if not self.available:
            return LLMResponse(
                False, None, "", 0, "none", "", "provider unavailable or budget spent"
            )

        if payload:
            safe = redact_dict(payload)
            assert_clean(safe)
            prompt = f"{prompt}\n\nDATA:\n{json.dumps(safe, default=str)[:6000]}"

        for provider in (self.primary, self.fallback):
            if not self._key_for(provider):
                continue
            response = self._call(provider, prompt, schema_keys)
            if response.ok:
                self._failures = 0
                self._calls_today += 1
                if cache_key:
                    self._cache[cache_key] = (now_ms(), response)
                return response
            log.warning("llm_call_failed", provider=provider, error=response.error)

        self._failures += 1
        if self._failures >= self.circuit_breaker_failures:
            log.warning("llm_circuit_opened", failures=self._failures)
        return LLMResponse(False, None, "", 0, self.primary, "", "all providers failed")

    def _call(self, provider: str, prompt: str, schema_keys: set[str]) -> LLMResponse:
        started = time.time()
        try:
            raw = self._call_gemini(prompt) if provider == "gemini" else self._call_groq(prompt)
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
            return LLMResponse(
                False,
                None,
                "",
                int((time.time() - started) * 1000),
                provider,
                "",
                str(exc)[:200],
            )

        latency = int((time.time() - started) * 1000)
        model = self.gemini_model if provider == "gemini" else self.groq_model

        data = _parse_json(raw)
        if data is None:
            return LLMResponse(False, None, raw, latency, provider, model, "response was not JSON")

        missing = schema_keys - set(data)
        if missing:
            return LLMResponse(
                False,
                None,
                raw,
                latency,
                provider,
                model,
                f"missing required keys: {sorted(missing)}",
            )

        return LLMResponse(True, data, raw, latency, provider, model)

    def _call_gemini(self, prompt: str) -> str:
        response = httpx.post(
            GEMINI_URL.format(model=self.gemini_model),
            params={"key": self.gemini_key},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0.1,
                    "responseMimeType": "application/json",
                    "maxOutputTokens": 1024,
                },
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()["candidates"][0]["content"]["parts"][0]["text"]

    def _call_groq(self, prompt: str) -> str:
        response = httpx.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {self.groq_key}"},
            json={
                "model": self.groq_model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
                "max_tokens": 1024,
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]

    def stats(self) -> dict[str, Any]:
        return {
            "calls_today": self._calls_today,
            "remaining": max(0, self._calls_remaining()),
            "failures": self._failures,
            "circuit_open": self._failures >= self.circuit_breaker_failures,
            "cached": len(self._cache),
            "available": self.available,
        }


def _parse_json(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        text = text.removeprefix("json").strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
