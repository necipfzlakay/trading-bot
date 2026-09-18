"""SQLite persistence — the bot's only durable memory.

Bybit's demo environment keeps order history for seven days, so the exchange is
not an archive. Everything the system needs to explain itself later lives here:
every order, fill, closed trade with its excursion, every conviction breakdown,
and — importantly — every bar on which a symbol was scanned and *not* traded.

Written in WAL mode so the dashboard can read while the engine writes.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from tbot.core.clock import now_ms
from tbot.core.models import (
    Conviction,
    EquityPoint,
    Fill,
    Order,
    Position,
    ScanRow,
    Trade,
)

SCHEMA_VERSION = 1

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per engine start. Lets us tie any event back to the process and
-- config that produced it, and makes restart storms visible.
CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_ts    INTEGER NOT NULL,
    stopped_ts    INTEGER,
    mode          TEXT NOT NULL,
    venue         TEXT NOT NULL,
    config_hash   TEXT NOT NULL,
    git_rev       TEXT,
    stop_reason   TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    client_id       TEXT PRIMARY KEY,
    run_id          INTEGER REFERENCES runs(id),
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,
    type            TEXT NOT NULL,
    quantity        REAL NOT NULL,
    price           REAL,
    status          TEXT NOT NULL,
    exchange_id     TEXT,
    filled_quantity REAL NOT NULL DEFAULT 0,
    average_price   REAL NOT NULL DEFAULT 0,
    fee_paid        REAL NOT NULL DEFAULT 0,
    reduce_only     INTEGER NOT NULL DEFAULT 0,
    reject_reason   TEXT,
    created_ts      INTEGER NOT NULL,
    updated_ts      INTEGER NOT NULL,
    meta            TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_symbol_ts ON orders(symbol, created_ts);
CREATE INDEX IF NOT EXISTS idx_orders_status    ON orders(status);

CREATE TABLE IF NOT EXISTS fills (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    order_client_id   TEXT NOT NULL,
    run_id            INTEGER REFERENCES runs(id),
    symbol            TEXT NOT NULL,
    side              TEXT NOT NULL,
    quantity          REAL NOT NULL,
    price             REAL NOT NULL,
    fee               REAL NOT NULL,
    is_maker          INTEGER NOT NULL DEFAULT 0,
    exchange_trade_id TEXT,
    ts                INTEGER NOT NULL,
    UNIQUE(order_client_id, exchange_trade_id)
);
CREATE INDEX IF NOT EXISTS idx_fills_symbol_ts ON fills(symbol, ts);

-- Open positions. Reconciled against the exchange on every start; on any
-- disagreement the exchange wins and the difference is logged.
CREATE TABLE IF NOT EXISTS positions (
    symbol              TEXT PRIMARY KEY,
    run_id              INTEGER REFERENCES runs(id),
    side                TEXT NOT NULL,
    quantity            REAL NOT NULL,
    entry_price         REAL NOT NULL,
    entry_ts            INTEGER NOT NULL,
    initial_stop        REAL NOT NULL,
    current_stop        REAL NOT NULL,
    setup               TEXT NOT NULL,
    risk_amount         REAL NOT NULL,
    leverage            REAL NOT NULL DEFAULT 1.0,
    fees_paid           REAL NOT NULL DEFAULT 0,
    max_favorable_price REAL NOT NULL,
    max_adverse_price   REAL NOT NULL,
    bars_held           INTEGER NOT NULL DEFAULT 0,
    conviction_at_entry REAL NOT NULL DEFAULT 0,
    entry_conviction_id INTEGER,
    meta                TEXT
);

-- Closed round trips: the unit of performance measurement. MFE/MAE are stored
-- here because empirical stop and target calibration is impossible without them.
CREATE TABLE IF NOT EXISTS trades (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              INTEGER REFERENCES runs(id),
    symbol              TEXT NOT NULL,
    side                TEXT NOT NULL,
    setup               TEXT NOT NULL,
    entry_ts            INTEGER NOT NULL,
    exit_ts             INTEGER NOT NULL,
    entry_price         REAL NOT NULL,
    exit_price          REAL NOT NULL,
    quantity            REAL NOT NULL,
    pnl                 REAL NOT NULL,
    fees                REAL NOT NULL,
    r_multiple          REAL NOT NULL,
    mfe_r               REAL NOT NULL,
    mae_r               REAL NOT NULL,
    bars_held           INTEGER NOT NULL,
    exit_reason         TEXT NOT NULL,
    regime_at_entry     TEXT NOT NULL,
    conviction_at_entry REAL NOT NULL DEFAULT 0,
    leverage            REAL NOT NULL DEFAULT 1.0,
    meta                TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_exit_ts ON trades(exit_ts);
CREATE INDEX IF NOT EXISTS idx_trades_symbol  ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_setup   ON trades(setup);

CREATE TABLE IF NOT EXISTS equity (
    ts             INTEGER PRIMARY KEY,
    run_id         INTEGER REFERENCES runs(id),
    equity         REAL NOT NULL,
    cash           REAL NOT NULL,
    unrealized     REAL NOT NULL,
    open_positions INTEGER NOT NULL,
    drawdown       REAL NOT NULL
);

-- Full scoring breakdown, kept whole rather than just the total, so the
-- dashboard can show which indicator contributed what — months later.
CREATE TABLE IF NOT EXISTS convictions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER REFERENCES runs(id),
    symbol          TEXT NOT NULL,
    bar_ts          INTEGER NOT NULL,
    side            TEXT NOT NULL,
    total           REAL NOT NULL,
    threshold       REAL NOT NULL,
    regime          TEXT NOT NULL,
    blocks_voting   INTEGER NOT NULL DEFAULT 0,
    passed          INTEGER NOT NULL DEFAULT 0,
    no_trade_reason TEXT,
    components      TEXT NOT NULL,
    UNIQUE(symbol, bar_ts, side)
);
CREATE INDEX IF NOT EXISTS idx_conv_bar ON convictions(bar_ts);

-- Every scanned symbol on every bar, traded or not. This is what makes
-- "why did the bot do nothing yesterday?" a query rather than a guess.
CREATE TABLE IF NOT EXISTS scans (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER REFERENCES runs(id),
    bar_ts          INTEGER NOT NULL,
    symbol          TEXT NOT NULL,
    regime          TEXT NOT NULL,
    best_side       TEXT,
    score           REAL NOT NULL,
    threshold       REAL NOT NULL,
    no_trade_reason TEXT,
    spread_bps      REAL NOT NULL DEFAULT 0,
    volume_24h      REAL NOT NULL DEFAULT 0,
    UNIQUE(bar_ts, symbol)
);
CREATE INDEX IF NOT EXISTS idx_scans_bar ON scans(bar_ts);

-- Every LLM call: prompt, response, latency, and — for veto roles — what the
-- trade would have done anyway. A role that cannot show its value gets removed.
CREATE TABLE IF NOT EXISTS llm_calls (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id            INTEGER REFERENCES runs(id),
    ts                INTEGER NOT NULL,
    role              TEXT NOT NULL,
    provider          TEXT NOT NULL,
    model             TEXT NOT NULL,
    prompt            TEXT NOT NULL,
    response          TEXT,
    parsed_ok         INTEGER NOT NULL DEFAULT 0,
    latency_ms        INTEGER NOT NULL DEFAULT 0,
    error             TEXT,
    symbol            TEXT,
    decision          TEXT,
    counterfactual_r  REAL
);
CREATE INDEX IF NOT EXISTS idx_llm_role_ts ON llm_calls(role, ts);

-- Operational events: errors, restarts, risk tier changes, degraded mode.
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id   INTEGER REFERENCES runs(id),
    ts       INTEGER NOT NULL,
    level    TEXT NOT NULL,
    kind     TEXT NOT NULL,
    message  TEXT NOT NULL,
    payload  TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts   ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind);
"""


class Database:
    """Thin SQLite wrapper. Deliberately not an ORM — the schema is the contract.

    Every method that persists a domain object takes the object itself, so the
    call sites read as intent ("record this fill") rather than as SQL.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
        self._conn.commit()
        self.run_id: int | None = None

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._conn.commit()
        self._conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Transaction scope. Rolls back on exception rather than half-writing."""
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def start_run(self, mode: str, venue: str, config_hash: str, git_rev: str | None = None) -> int:
        cur = self._conn.execute(
            "INSERT INTO runs(started_ts, mode, venue, config_hash, git_rev) VALUES(?,?,?,?,?)",
            (now_ms(), mode, venue, config_hash, git_rev),
        )
        self._conn.commit()
        self.run_id = int(cur.lastrowid)
        return self.run_id

    def stop_run(self, reason: str = "clean") -> None:
        if self.run_id is None:
            return
        self._conn.execute(
            "UPDATE runs SET stopped_ts=?, stop_reason=? WHERE id=?",
            (now_ms(), reason, self.run_id),
        )
        self._conn.commit()

    def restarts_since(self, since_ts: int) -> int:
        """How many runs started since a timestamp — restart-storm detection."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM runs WHERE started_ts >= ?", (since_ts,)
        ).fetchone()
        return int(row["n"])

    # -- orders and fills --------------------------------------------------

    def upsert_order(self, order: Order) -> None:
        self._conn.execute(
            """INSERT INTO orders(client_id, run_id, symbol, side, type, quantity, price, status,
                                  exchange_id, filled_quantity, average_price, fee_paid,
                                  reduce_only, reject_reason, created_ts, updated_ts, meta)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(client_id) DO UPDATE SET
                   status=excluded.status,
                   exchange_id=COALESCE(excluded.exchange_id, orders.exchange_id),
                   filled_quantity=excluded.filled_quantity,
                   average_price=excluded.average_price,
                   fee_paid=excluded.fee_paid,
                   reject_reason=excluded.reject_reason,
                   updated_ts=excluded.updated_ts,
                   meta=excluded.meta""",
            (
                order.client_id,
                self.run_id,
                order.symbol,
                order.side.value,
                order.type.value,
                order.quantity,
                order.price,
                order.status.value,
                order.exchange_id,
                order.filled_quantity,
                order.average_price,
                order.fee_paid,
                int(order.reduce_only),
                order.reject_reason,
                order.created_ts,
                order.updated_ts,
                json.dumps(order.meta),
            ),
        )
        self._conn.commit()

    def order_exists(self, client_id: str) -> bool:
        """Idempotency check used before sending — survives a crash mid-send."""
        row = self._conn.execute(
            "SELECT 1 FROM orders WHERE client_id=? LIMIT 1", (client_id,)
        ).fetchone()
        return row is not None

    def record_fill(self, fill: Fill) -> None:
        self._conn.execute(
            """INSERT OR IGNORE INTO fills(order_client_id, run_id, symbol, side, quantity,
                                           price, fee, is_maker, exchange_trade_id, ts)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                fill.order_client_id,
                self.run_id,
                fill.symbol,
                fill.side.value,
                fill.quantity,
                fill.price,
                fill.fee,
                int(fill.is_maker),
                fill.exchange_trade_id,
                fill.ts,
            ),
        )
        self._conn.commit()

    # -- positions and trades ---------------------------------------------

    def upsert_position(self, pos: Position) -> None:
        self._conn.execute(
            """INSERT INTO positions(symbol, run_id, side, quantity, entry_price, entry_ts,
                                     initial_stop, current_stop, setup, risk_amount, leverage,
                                     fees_paid, max_favorable_price, max_adverse_price, bars_held,
                                     conviction_at_entry, entry_conviction_id, meta)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(symbol) DO UPDATE SET
                   quantity=excluded.quantity, current_stop=excluded.current_stop,
                   fees_paid=excluded.fees_paid,
                   max_favorable_price=excluded.max_favorable_price,
                   max_adverse_price=excluded.max_adverse_price,
                   bars_held=excluded.bars_held, meta=excluded.meta""",
            (
                pos.symbol,
                self.run_id,
                pos.side.value,
                pos.quantity,
                pos.entry_price,
                pos.entry_ts,
                pos.initial_stop,
                pos.current_stop,
                pos.setup,
                pos.risk_amount,
                pos.leverage,
                pos.fees_paid,
                pos.max_favorable_price,
                pos.max_adverse_price,
                pos.bars_held,
                pos.conviction_at_entry,
                pos.entry_conviction_id,
                json.dumps(pos.meta),
            ),
        )
        self._conn.commit()

    def delete_position(self, symbol: str) -> None:
        self._conn.execute("DELETE FROM positions WHERE symbol=?", (symbol,))
        self._conn.commit()

    def load_positions(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._conn.execute("SELECT * FROM positions")]

    def record_trade(self, trade: Trade) -> int:
        cur = self._conn.execute(
            """INSERT INTO trades(run_id, symbol, side, setup, entry_ts, exit_ts, entry_price,
                                  exit_price, quantity, pnl, fees, r_multiple, mfe_r, mae_r,
                                  bars_held, exit_reason, regime_at_entry, conviction_at_entry,
                                  leverage, meta)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                self.run_id,
                trade.symbol,
                trade.side.value,
                trade.setup,
                trade.entry_ts,
                trade.exit_ts,
                trade.entry_price,
                trade.exit_price,
                trade.quantity,
                trade.pnl,
                trade.fees,
                trade.r_multiple,
                trade.mfe_r,
                trade.mae_r,
                trade.bars_held,
                trade.exit_reason.value,
                trade.regime_at_entry.value,
                trade.conviction_at_entry,
                trade.leverage,
                json.dumps(trade.meta),
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def recent_trades(self, limit: int = 100) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self._conn.execute(
                "SELECT * FROM trades ORDER BY exit_ts DESC LIMIT ?", (limit,)
            )
        ]

    # -- equity, convictions, scans ---------------------------------------

    def record_equity(self, point: EquityPoint) -> None:
        self._conn.execute(
            """INSERT OR REPLACE INTO equity(ts, run_id, equity, cash, unrealized,
                                             open_positions, drawdown)
               VALUES(?,?,?,?,?,?,?)""",
            (
                point.ts,
                self.run_id,
                point.equity,
                point.cash,
                point.unrealized,
                point.open_positions,
                point.drawdown,
            ),
        )
        self._conn.commit()

    def record_conviction(self, conv: Conviction) -> int:
        components = json.dumps(
            [
                {
                    "name": c.name,
                    "score": c.score,
                    "max_score": c.max_score,
                    "detail": c.detail,
                    "signals": [
                        {
                            "name": s.name,
                            "category": s.category.value,
                            "timeframe": s.timeframe,
                            "value": s.value,
                            "raw": s.raw,
                        }
                        for s in c.signals
                    ],
                }
                for c in conv.components
            ]
        )
        cur = self._conn.execute(
            """INSERT INTO convictions(run_id, symbol, bar_ts, side, total, threshold, regime,
                                       blocks_voting, passed, no_trade_reason, components)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(symbol, bar_ts, side) DO UPDATE SET
                   total=excluded.total, threshold=excluded.threshold,
                   blocks_voting=excluded.blocks_voting, passed=excluded.passed,
                   no_trade_reason=excluded.no_trade_reason, components=excluded.components""",
            (
                self.run_id,
                conv.symbol,
                conv.bar_ts,
                conv.side.value,
                conv.total,
                conv.threshold,
                conv.regime.value,
                conv.blocks_voting,
                int(conv.passed),
                conv.no_trade_reason.value if conv.no_trade_reason else None,
                components,
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def record_scans(self, rows: list[ScanRow]) -> None:
        """Bulk-insert one bar's scan table. Written even when nothing traded."""
        self._conn.executemany(
            """INSERT INTO scans(run_id, bar_ts, symbol, regime, best_side, score, threshold,
                                 no_trade_reason, spread_bps, volume_24h)
               VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(bar_ts, symbol) DO UPDATE SET
                   score=excluded.score, threshold=excluded.threshold,
                   best_side=excluded.best_side, no_trade_reason=excluded.no_trade_reason""",
            [
                (
                    self.run_id,
                    r.bar_ts,
                    r.symbol,
                    r.regime.value,
                    r.best_side.value if r.best_side else None,
                    r.score,
                    r.threshold,
                    r.no_trade_reason.value if r.no_trade_reason else None,
                    r.spread_bps,
                    r.volume_24h,
                )
                for r in rows
            ],
        )
        self._conn.commit()

    # -- ops ---------------------------------------------------------------

    def record_event(
        self, kind: str, message: str, level: str = "info", payload: dict[str, Any] | None = None
    ) -> None:
        self._conn.execute(
            "INSERT INTO events(run_id, ts, level, kind, message, payload) VALUES(?,?,?,?,?,?)",
            (self.run_id, now_ms(), level, kind, message, json.dumps(payload or {})),
        )
        self._conn.commit()

    def record_llm_call(self, **kwargs: Any) -> int:
        cols = (
            "ts",
            "role",
            "provider",
            "model",
            "prompt",
            "response",
            "parsed_ok",
            "latency_ms",
            "error",
            "symbol",
            "decision",
            "counterfactual_r",
        )
        values = [kwargs.get(c) for c in cols]
        values[0] = values[0] or now_ms()
        cur = self._conn.execute(
            f"INSERT INTO llm_calls(run_id, {','.join(cols)}) "
            f"VALUES(?,{','.join('?' * len(cols))})",
            (self.run_id, *values),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def set_counterfactual(self, call_id: int, r_multiple: float) -> None:
        """Record what a vetoed trade would have done.

        Without this the event guard can never be judged: it would sit in the
        system indefinitely on the strength of sounding useful.
        """
        self._conn.execute(
            "UPDATE llm_calls SET counterfactual_r=? WHERE id=?", (r_multiple, call_id)
        )
        self._conn.commit()

    def pending_counterfactuals(self, older_than_ts: int) -> list[dict[str, Any]]:
        """Vetoes old enough to have an outcome, still unmeasured."""
        return [
            dict(r)
            for r in self._conn.execute(
                "SELECT id, ts, symbol, decision FROM llm_calls "
                "WHERE role='event_guard' AND counterfactual_r IS NULL "
                "AND decision LIKE '%True%' AND ts <= ?",
                (older_than_ts,),
            )
        ]

    def llm_calls_today(self, role: str, since_ts: int) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM llm_calls WHERE role=? AND ts >= ?", (role, since_ts)
        ).fetchone()
        return int(row["n"])

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self._conn.commit()

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """Escape hatch for reports and the dashboard."""
        return [dict(r) for r in self._conn.execute(sql, params)]
