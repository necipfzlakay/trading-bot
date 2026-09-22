"""The other engines trading the same account.

Three engines share one Bybit demo account and one Unified wallet. Each one is
its own process with its own book, but two things cannot be decided by an
engine alone: which perpetual symbols are already taken (one-way mode nets two
positions into one — see ``engine.claims``), and how much of the shared USDT is
actually this engine's (see ``Runner`` seeding).

Both need the same answer to "who else is here?", so it lives in one place. An
engine is identified by its namespace, exactly as ``bot.sh`` and the panel do:
``demo`` and ``live`` share the empty namespace and are one engine that is
never installed twice, while ``fast`` and ``scalp`` are separate services.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tbot.core.clock import now_ms
from tbot.core.config import PROJECT_ROOT, Config, load_config
from tbot.core.logging import get_logger
from tbot.core.models import Mode

log = get_logger("engine.siblings")


def sibling_configs(config: Config, config_dir: Path | None = None) -> list[Config]:
    """Every *other* live engine on the same venue, one per namespace.

    A config that fails to load is skipped rather than fatal — a broken file in
    ``config/`` must not stop a healthy engine from starting — but it is logged,
    because a sibling that cannot be read is one whose claims and capital this
    engine is now ignoring.
    """
    directory = config_dir or PROJECT_ROOT / "config"
    found: dict[str, Config] = {}
    for path in sorted(directory.glob("*.yaml")):
        if path.stem == "base":
            continue
        try:
            other = load_config(str(path))
        except Exception as exc:  # noqa: BLE001 — see docstring
            log.warning("sibling_config_unreadable", path=path.name, error=str(exc)[:160])
            continue
        if other.mode is not Mode.LIVE or other.venue is not config.venue:
            continue
        if other.engine.namespace == config.engine.namespace:
            continue  # this engine, or its demo/live twin — never both installed
        found.setdefault(other.engine.namespace, other)
    return list(found.values())


def assert_claims_consistent(config: Config, siblings: list[Config] | None = None) -> None:
    """Refuse to start a perp engine beside another perp engine that ignores claims.

    One engine honouring claims is no protection: the one that does not will
    open the symbol regardless, and the venue nets the two positions. Every
    perpetual engine on the account must use the same claim table, or none may
    run beside another.
    """
    if config.execution.market_type != "linear":
        return
    peers = [
        s
        for s in (siblings if siblings is not None else sibling_configs(config))
        if s.execution.market_type == "linear"
    ]
    if not peers:
        return
    offenders = [
        s.engine.namespace or "(default)"
        for s in [config, *peers]
        if not s.engine.symbol_claims
    ]
    if offenders:
        raise RuntimeError(
            "several perpetual engines share this account, and one-way mode nets "
            "their positions per symbol — every one of them must set "
            f"engine.symbol_claims: true. Missing on: {', '.join(offenders)}"
        )
    paths = {s.engine.claims_path for s in [config, *peers]}
    if len(paths) > 1:
        raise RuntimeError(
            "perpetual engines on one account must share one claim table; "
            f"found {sorted(str(p) for p in paths)}"
        )


# A sibling's own state is trusted for this long after its last heartbeat
# (invariant 37: every 60s). Past it the sibling is presumed down, and its
# whole allocation stays reserved — a stopped engine still owns its money.
STATE_FRESH_MS = 15 * 60_000


def sibling_reservation(
    config: Config, siblings: list[Config] | None = None, now: int | None = None
) -> tuple[float, dict[str, float]]:
    """USDT the other engines own out of the shared free balance.

    A running sibling reports its own ``cash`` in ``state.json``: portfolio cash
    excludes margin posted for open positions, and so does the venue's free
    balance, so the two subtract cleanly. A sibling that is down, or has never
    run, reserves its full ``max_capital`` instead — over-reserving if it holds
    positions, which is the safe direction.
    """
    now = now_ms() if now is None else now
    breakdown: dict[str, float] = {}
    for sibling in siblings if siblings is not None else sibling_configs(config):
        name = sibling.engine.namespace or "(default)"
        cap = sibling.risk.max_capital
        if cap <= 0:
            # Two engines that each own "whatever is left" cannot both be right.
            log.error(
                "sibling_without_capital_cap",
                sibling=name,
                consequence="its share of the wallet cannot be separated from this engine's",
            )
            continue
        amount = cap
        try:
            state = json.loads(sibling.state_path.read_text(encoding="utf-8"))
            if now - int(state.get("updated_ts") or 0) <= STATE_FRESH_MS:
                amount = max(0.0, float(state.get("cash") or 0.0))
        except (OSError, ValueError):
            pass
        breakdown[name] = round(amount, 2)
    return sum(breakdown.values()), breakdown


def own_starting_cash(
    *,
    venue_free: float,
    positions_value: float,
    cap: float,
    last_equity: float | None,
    reserved_for_siblings: float,
) -> float:
    """The cash this engine may seed its book with at startup.

    **Capped engine** (``max_capital`` set): the cap is its *allocation* — what
    it starts with the first time. After that it continues from its own last
    equity, loss and profit alike, because both are its own. It used to re-seed
    at ``min(cap, free)`` on every start, applied to cash alone: open positions
    were counted on top (the fast engine seeded 313.94 on a 300 cap), any loss
    vanished at restart — an engine at $500 of a $1000 allocation came back at
    $1000, refilled from the shared wallet, so a kill criterion measured on
    equity could never be reached — and, once every engine was capped, any
    profit above the cap was clipped away at each restart.

    **Residual engine** (no cap — the main spot engine): owns what the others do
    not. It used to adopt the entire free balance, so a sibling's allocation was
    sized against twice, and a sibling's losses slid into this engine's starting
    capital under the 25% restart alarm (invariant 28).
    """
    if cap > 0:
        target = last_equity if last_equity and last_equity > 0 else cap
        if target > cap * 1.5:
            # Possible — a strategy can make half its allocation — but that is
            # also what a misattributed balance looks like. Say so; do not clip.
            log.warning("capital_carried_well_above_allocation", equity=target, cap=cap)
        return min(venue_free, max(0.0, target - positions_value))
    return max(0.0, venue_free - reserved_for_siblings)


@dataclass(frozen=True, slots=True)
class CarriedBook:
    """What the previous run's last snapshot says this engine's book was."""

    equity: float | None = None
    # The price each open position was valued at inside ``equity``.
    marks: dict[str, float] = field(default_factory=dict)
    peak: float | None = None
    starting: float | None = None


def carried_book(snapshot: dict[str, Any] | None) -> CarriedBook:
    """Read the previous run's book out of its ``state.json``.

    ``equity`` valued every open position at a price, and a restart takes those
    positions back out of it to find the cash. It has to use the *same* price.
    Seeding took them out at entry instead: the unrealised P&L at shutdown was
    baked into cash, then counted a second time once the position was marked.
    Measured on the demo engine: restarted with -4.50 unrealised on three spot
    longs, seeded 314.82 instead of 319.31, and carried the gap for good. A
    winner would have been counted twice the same way, upwards. A snapshot
    without ``equity_marks`` (written before they were saved) yields none, which
    is the old entry-price behaviour.

    ``peak`` and ``starting`` continue the capped engine's book across restarts
    (``own_starting_cash``). Reset at each start, the peak made the drawdown
    ladder (invariant 4) and the capital freeze forget every loss the moment
    the process restarted: an engine 12% down on 0.5x came back on 1.0x.
    """
    if not snapshot:
        return CarriedBook()

    def positive(key: str) -> float | None:
        try:
            value = float(snapshot.get(key) or 0.0)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    marks: dict[str, float] = {}
    for symbol, price in (snapshot.get("equity_marks") or {}).items():
        try:
            if float(price) > 0:
                marks[str(symbol)] = float(price)
        except (TypeError, ValueError):
            continue
    return CarriedBook(
        equity=positive("equity"),
        marks=marks,
        peak=positive("peak_equity"),
        starting=positive("starting_equity"),
    )
