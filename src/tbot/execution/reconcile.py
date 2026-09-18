"""Startup reconciliation between local state and the venue.

Every restart begins by asking the exchange what it thinks is true and comparing
that against the database. On any disagreement **the exchange wins** — it holds
the money — and the difference is logged loudly rather than quietly corrected.

This matters more than usual on Bybit demo, which keeps order history for only
seven days. The local database is the sole durable record, and a reconciliation
pass is the only thing that keeps the two from drifting apart after a crash.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tbot.core.logging import get_logger
from tbot.core.models import Position

log = get_logger("execution.reconcile")

# Ignore dust: exchanges leave rounding residue that is not a position.
DUST_THRESHOLD = 1e-8


@dataclass(slots=True)
class ReconcileReport:
    matched: list[str] = field(default_factory=list)
    missing_locally: list[str] = field(default_factory=list)  # venue has it, we do not
    missing_remotely: list[str] = field(default_factory=list)  # we have it, venue does not
    quantity_mismatch: list[tuple[str, float, float]] = field(default_factory=list)
    orphan_orders: list[str] = field(default_factory=list)
    balance: dict[str, float] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        return not (
            self.missing_locally
            or self.missing_remotely
            or self.quantity_mismatch
            or self.orphan_orders
        )

    def summary(self) -> str:
        if self.clean:
            return f"reconciled cleanly: {len(self.matched)} positions match"
        parts = []
        if self.missing_locally:
            parts.append(f"{len(self.missing_locally)} held at venue but not locally")
        if self.missing_remotely:
            parts.append(f"{len(self.missing_remotely)} held locally but not at venue")
        if self.quantity_mismatch:
            parts.append(f"{len(self.quantity_mismatch)} quantity mismatches")
        if self.orphan_orders:
            parts.append(f"{len(self.orphan_orders)} orphan open orders")
        return "; ".join(parts)


def reconcile(
    local_positions: dict[str, Position],
    venue_positions: dict[str, float],
    open_orders: list,
    known_client_ids: set[str],
    quantity_tolerance: float = 0.01,
) -> ReconcileReport:
    """Compare local and venue state and report every divergence."""
    report = ReconcileReport(balance=dict(venue_positions))

    venue = {s: q for s, q in venue_positions.items() if q > DUST_THRESHOLD}

    for symbol, position in local_positions.items():
        remote_qty = venue.get(symbol)
        if remote_qty is None:
            # Usually a stop that filled while the process was down.
            report.missing_remotely.append(symbol)
            log.warning(
                "position_gone_at_venue",
                symbol=symbol,
                local_quantity=position.quantity,
                hint="likely closed while the bot was offline",
            )
            continue

        relative = abs(remote_qty - position.quantity) / max(position.quantity, 1e-12)
        if relative > quantity_tolerance:
            report.quantity_mismatch.append((symbol, position.quantity, remote_qty))
            log.warning(
                "position_quantity_mismatch",
                symbol=symbol,
                local=position.quantity,
                venue=remote_qty,
                resolution="venue value accepted",
            )
        else:
            report.matched.append(symbol)

    for symbol in venue:
        if symbol not in local_positions:
            report.missing_locally.append(symbol)
            log.warning(
                "unexpected_venue_position",
                symbol=symbol,
                quantity=venue[symbol],
                hint="manual trade, or a fill the bot never recorded",
            )

    for order in open_orders:
        if order.client_id not in known_client_ids:
            report.orphan_orders.append(order.client_id)
            log.warning("orphan_open_order", client_id=order.client_id, symbol=order.symbol)

    log.info("reconcile_complete", clean=report.clean, summary=report.summary())
    return report


def apply(
    report: ReconcileReport,
    local_positions: dict[str, Position],
    venue_positions: dict[str, float],
) -> dict[str, Position]:
    """Bring local state in line with the venue.

    Positions the venue no longer holds are dropped; quantities are corrected to
    the venue's. Positions the venue holds but the bot does not are *not*
    adopted — the bot has no stop, no entry price and no thesis for them, so
    inventing one would be worse than leaving them alone. They are surfaced for
    a human instead.
    """
    updated = dict(local_positions)

    for symbol in report.missing_remotely:
        updated.pop(symbol, None)

    for symbol, _local, remote in report.quantity_mismatch:
        if symbol in updated:
            updated[symbol].quantity = remote

    if report.missing_locally:
        log.warning(
            "unmanaged_positions_present",
            symbols=report.missing_locally,
            action="left untouched — the bot will not manage a position it has no plan for",
        )

    return updated
