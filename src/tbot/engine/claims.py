"""Which engine owns which perpetual, when several share one account.

Bybit's linear market runs in one-way mode: an account holds **one position per
symbol**. Two engines that each open SOL/USDT:USDT do not get two positions —
the venue nets them into one, and whichever engine looks next finds a size it
never ordered. Reconciliation then "accepts the venue value" (the right rule
for a single engine) and one engine's book silently absorbs the other's trade.

The operator's rule is simple: every symbol is open to every perp engine, the
first to open it owns it until it is flat again, and the others leave it alone
meanwhile. This module makes that rule atomic across processes.

**A claim is a row, not a lock.** A file lock dies with its process, but the
position does not: the stop sits at the venue and the symbol is still taken
while the owner is down. So a claim outlives a crash, and only its owner — or
the operator, through ``scripts/claims.py`` — ever releases it.

**Ordering is the whole contract.** Claim *before* the entry order is sent, so a
racing engine cannot send its own; release only *after* the venue shows the
position flat, so a symbol is never handed on while it still carries size.

SQLite provides the atomicity: ``INSERT … ON CONFLICT DO NOTHING`` either takes
the row or finds someone else's, in one statement, under the database's own
lock, whichever process gets there first.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from tbot.core.clock import now_ms
from tbot.core.logging import get_logger

log = get_logger("engine.claims")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS claims (
    symbol     TEXT PRIMARY KEY,
    owner      TEXT NOT NULL,
    claimed_ts INTEGER NOT NULL,
    reason     TEXT NOT NULL DEFAULT ''
);
"""


class SymbolClaims:
    """One engine's view of the shared claim table."""

    def __init__(self, path: Path, owner: str):
        if not owner:
            # An empty owner would match every other empty owner, which is the
            # one property a claim must not have.
            raise ValueError("a claim owner needs a non-empty engine namespace")
        self.path = Path(path)
        self.owner = owner
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Autocommit: every statement is its own transaction, so nothing is ever
        # held open across a network call to the venue.
        self._db = sqlite3.connect(self.path, timeout=10.0, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=10000")
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    # -- the rule ----------------------------------------------------------

    def claim(self, symbol: str, reason: str = "") -> bool:
        """Take the symbol, or confirm we already hold it. False if another does."""
        self._db.execute(
            "INSERT INTO claims (symbol, owner, claimed_ts, reason) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(symbol) DO NOTHING",
            (symbol, self.owner, now_ms(), reason),
        )
        owner = self.owner_of(symbol)
        if owner != self.owner:
            log.info("symbol_claimed_elsewhere", symbol=symbol, owner=owner)
            return False
        return True

    def release(self, symbol: str) -> bool:
        """Give the symbol back. Only ever releases our own claim."""
        cursor = self._db.execute(
            "DELETE FROM claims WHERE symbol = ? AND owner = ?", (symbol, self.owner)
        )
        return cursor.rowcount > 0

    # -- reading -----------------------------------------------------------

    def owner_of(self, symbol: str) -> str | None:
        row = self._db.execute("SELECT owner FROM claims WHERE symbol = ?", (symbol,)).fetchone()
        return str(row[0]) if row else None

    def mine(self) -> set[str]:
        rows = self._db.execute("SELECT symbol FROM claims WHERE owner = ?", (self.owner,))
        return {str(r[0]) for r in rows}

    def held_by_others(self) -> dict[str, str]:
        rows = self._db.execute(
            "SELECT symbol, owner FROM claims WHERE owner != ?", (self.owner,)
        )
        return {str(s): str(o) for s, o in rows}

    def all(self) -> list[tuple[str, str, int, str]]:
        rows = self._db.execute(
            "SELECT symbol, owner, claimed_ts, reason FROM claims ORDER BY claimed_ts"
        )
        return [(str(s), str(o), int(t), str(r)) for s, o, t, r in rows]

    # -- operator ----------------------------------------------------------

    def force_release(self, symbol: str) -> str | None:
        """Drop a claim whoever holds it. Operator use only (``scripts/claims.py``)."""
        owner = self.owner_of(symbol)
        self._db.execute("DELETE FROM claims WHERE symbol = ?", (symbol,))
        return owner
