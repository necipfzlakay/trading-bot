"""The five LLM roles, and the boundary they cannot cross.

The iron rule is enforced here in code rather than requested in a prompt:

    An LLM may only ever *reduce* risk. It cannot create a signal, size a
    position, move a stop, raise a limit, or open leverage.

That is not caution for its own sake. The 2026 literature is consistent that
adding news and sentiment to market signals *lowered* returns and raised
drawdowns, that a finance-tuned model fabricated content in 41% of probe cases,
and that LLM trading agents tend toward aggressive sizing with weak risk
control. Putting a component with those properties on the decision path would
place the least reliable part of the system at its most consequential point.

What LLMs are genuinely good at — reading unstructured text, summarising, and
proposing hypotheses — is what these roles do.

Role 1 is the only one that can affect trading at all, and only by blocking a
new entry. It is measured against its own counterfactual and switched off if it
cannot show it helped.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tbot.core.clock import now_ms
from tbot.core.logging import get_logger
from tbot.llm.provider import LLMProvider

log = get_logger("llm.roles")


@dataclass(slots=True)
class RoleResult:
    role: str
    ok: bool
    data: dict[str, Any]
    latency_ms: int = 0
    provider: str = ""
    model: str = ""
    error: str | None = None


class BaseRole:
    name = "base"
    schema: set[str] = set()

    def __init__(self, provider: LLMProvider, db=None, max_calls_per_day: int = 10):
        self.provider = provider
        self.db = db
        self.max_calls_per_day = max_calls_per_day
        # Id of the most recent logged call, so a veto can be matched to its
        # outcome once one exists.
        self.last_call_id: int | None = None

    def _budget_ok(self) -> bool:
        if self.db is None:
            return True
        since = (now_ms() // 86_400_000) * 86_400_000
        return self.db.llm_calls_today(self.name, since) < self.max_calls_per_day

    def _run(
        self,
        prompt: str,
        payload: dict | None = None,
        cache_key: str | None = None,
        symbol: str | None = None,
    ) -> RoleResult:
        if not self._budget_ok():
            return RoleResult(self.name, False, {}, error="daily call budget spent")

        response = self.provider.ask(prompt, self.schema, payload, cache_key)

        self.last_call_id = None
        if self.db is not None:
            self.last_call_id = self.db.record_llm_call(
                role=self.name,
                provider=response.provider,
                model=response.model,
                prompt=prompt[:2000],
                response=response.raw[:2000],
                parsed_ok=int(response.ok),
                latency_ms=response.latency_ms,
                error=response.error,
                symbol=symbol,
                decision=str(response.data)[:200] if response.data else None,
            )

        return RoleResult(
            self.name,
            response.ok,
            response.data or {},
            response.latency_ms,
            response.provider,
            response.model,
            response.error,
        )


class EventGuard(BaseRole):
    """Blocks new entries around high-impact events. Veto only.

    The only role that touches trading, and only in the direction of doing less.
    It cannot open a position, cannot size one, and cannot touch anything
    already open — an existing position keeps its stop and its plan.

    Every veto is recorded with what the trade would have done anyway, so the
    role is judged on measured contribution. If ninety days of counterfactuals
    do not show a net benefit, it gets switched off rather than kept for
    plausibility.
    """

    name = "event_guard"
    schema = {"block_new_entries", "severity", "reason"}

    PROMPT = (
        "You are a risk filter for an automated crypto spot trading system.\n"
        "Decide ONLY whether new position entries should be paused right now "
        "because of an unusual, high-impact market event: an exchange halt or "
        "hack, an imminent delisting, an emergency regulatory action, or a "
        "market-wide dislocation.\n\n"
        "You cannot open, size, or close positions. Your sole output is whether "
        "to pause NEW entries.\n\n"
        "Default to false. Routine volatility, ordinary news, price moves and "
        "general uncertainty are NOT reasons to block — the system already "
        "handles those with its own risk controls. Block only for something "
        "genuinely exceptional.\n\n"
        'Respond as JSON: {"block_new_entries": bool, "severity": "none|low|'
        'medium|high", "reason": "one short sentence"}'
    )

    def check(self, market_context: dict) -> RoleResult:
        result = self._run(
            self.PROMPT, market_context, cache_key=f"event_guard:{now_ms() // 3_600_000}"
        )
        if not result.ok:
            # Unreachable or malformed means no veto. Failing closed would let a
            # provider outage stop the bot trading, which is the opposite of
            # what this role is for.
            return RoleResult(self.name, False, {"block_new_entries": False})

        blocked = bool(result.data.get("block_new_entries"))
        severity = str(result.data.get("severity", "none"))
        # Only high severity is honoured. A model that is merely uneasy does not
        # get to stop the strategy.
        if blocked and severity != "high":
            result.data["block_new_entries"] = False
            result.data["downgraded"] = f"severity {severity} below the 'high' bar"

        if result.data.get("block_new_entries"):
            log.warning("llm_event_veto", reason=result.data.get("reason"), severity=severity)

        return result


class PostMortem(BaseRole):
    """Proposes hypotheses from closed trades. Never changes anything.

    Output goes to ``research/hypotheses.md`` as prose for a human to read. A
    hypothesis becomes code only after it has been turned into a backtest and
    passed CPCV — the LLM is an idea generator, and ideas are cheap.
    """

    name = "post_mortem"
    schema = {"observations", "hypotheses"}

    PROMPT = (
        "You are analysing closed trades from a rule-based crypto trading system.\n"
        "All figures are in R units (1R = the risk taken on that trade); there "
        "are no currency amounts.\n\n"
        "Identify patterns worth investigating. Be specific and testable: "
        '"entries in the range regime show twice the average MAE" is useful, '
        '"risk management could be improved" is not.\n\n'
        "Do not recommend parameter values. Do not claim causation from small "
        "samples — if there are fewer than 30 trades in a group, say the sample "
        "is too small.\n\n"
        'Respond as JSON: {"observations": ["..."], "hypotheses": [{"claim": '
        '"...", "test": "how to test it", "confidence": "low|medium|high"}]}'
    )

    def analyse(self, trade_stats: dict) -> RoleResult:
        return self._run(self.PROMPT, trade_stats)


class AnomalyTriage(BaseRole):
    """Summarises error bursts. Operations only; never touches money."""

    name = "anomaly_triage"
    schema = {"summary", "likely_cause", "suggested_check"}

    PROMPT = (
        "You are triaging errors from a trading bot's logs.\n"
        "Summarise what is happening, give the most likely cause, and suggest "
        "one concrete check.\n"
        "Be brief. If the logs are insufficient to tell, say so.\n\n"
        'Respond as JSON: {"summary": "...", "likely_cause": "...", '
        '"suggested_check": "...", "severity": "low|medium|high"}'
    )

    def triage(self, error_context: dict) -> RoleResult:
        return self._run(self.PROMPT, error_context)


class ReportWriter(BaseRole):
    """Turns computed numbers into prose.

    The numbers are computed in code and passed in. The model is explicitly told
    not to calculate anything, because arithmetic is exactly where hallucination
    would be both most likely and most damaging.
    """

    name = "report_writer"
    schema = {"headline", "body"}

    PROMPT = (
        "Write a short status summary for the operator of an automated trading "
        "system, using ONLY the figures provided.\n\n"
        "Do not calculate anything. Do not add figures that are not in the data. "
        "Do not predict future performance. If results are poor, say so plainly.\n\n"
        'Respond as JSON: {"headline": "one line", "body": "2-4 sentences"}'
    )

    def write(self, stats: dict) -> RoleResult:
        return self._run(self.PROMPT, stats)


class ConfigAuditor(BaseRole):
    """Sanity-checks configuration changes. Advisory: it warns, it cannot block."""

    name = "config_auditor"
    schema = {"concerns", "verdict"}

    PROMPT = (
        "Review a configuration change to an automated trading system for "
        "obvious hazards: excessive risk per trade, stops too tight to survive "
        "normal noise, position limits inconsistent with account size, "
        "contradictory settings.\n\n"
        "Flag only genuine problems. Do not suggest 'optimisations'.\n\n"
        'Respond as JSON: {"concerns": ["..."], "verdict": "ok|caution|dangerous"}'
    )

    def audit(self, config_diff: dict) -> RoleResult:
        return self._run(self.PROMPT, config_diff)


ROLE_CLASSES = {
    "event_guard": EventGuard,
    "post_mortem": PostMortem,
    "anomaly_triage": AnomalyTriage,
    "report_writer": ReportWriter,
    "config_auditor": ConfigAuditor,
}


def build_roles(config, secrets, db=None) -> dict[str, BaseRole]:
    """Instantiate the roles enabled in config. Returns {} when LLM is off."""
    if not config.llm.enabled:
        return {}

    provider = LLMProvider(
        gemini_key=secrets.gemini_api_key,
        groq_key=secrets.groq_api_key,
        primary=config.llm.primary_provider,
        fallback=config.llm.fallback_provider,
        gemini_model=config.llm.gemini_model,
        groq_model=config.llm.groq_model,
        timeout=config.llm.timeout_seconds,
        max_calls_per_day=config.llm.max_calls_per_day,
        cache_ttl_seconds=config.llm.cache_ttl_seconds,
        circuit_breaker_failures=config.llm.circuit_breaker_failures,
    )

    roles: dict[str, BaseRole] = {}
    for name, role_config in config.llm.roles.items():
        if not role_config.enabled:
            continue
        cls = ROLE_CLASSES.get(name)
        if cls is None:
            log.warning("unknown_llm_role", role=name)
            continue
        roles[name] = cls(provider, db, role_config.max_calls_per_day)

    log.info("llm_roles_enabled", roles=sorted(roles))
    return roles
