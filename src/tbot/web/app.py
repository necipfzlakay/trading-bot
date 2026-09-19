"""Local dashboard.

Read-mostly by design. It reads the same files and tables the engine writes, so
it cannot disagree with the bot about what is happening — there is no second
source of truth to drift.

The two controls it does expose (pause, flatten) write a command file the engine
picks up, rather than reaching into the running process. That keeps the engine
single-threaded and means a crashed dashboard cannot leave the bot in a strange
state.

Binds to localhost only. Nothing here is built to face a network.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse

from tbot.core.config import Config, load_config, load_secrets
from tbot.core.db import Database

STATIC = Path(__file__).parent / "static"


def create_app(config: Config | None = None) -> FastAPI:
    cfg = config or load_config("config/base.yaml")
    secrets = load_secrets()

    # One connection for the life of the process, not one per request. A fresh
    # Database per request leaked a file descriptor every time — the panel polls
    # several endpoints a second, so it walked into the launchd file limit (256)
    # in a couple of minutes and then went blind: "[Errno 24] Too many open
    # files" on state.json, "unable to open database file" on every query.
    # KeepAlive restarted it and the climb began again.
    #
    # Opening it once also stops the panel writing to the engine's database on
    # every request: Database.__init__ runs the schema script and stamps the
    # schema version. A read-mostly dashboard should do that once, at most.
    if sqlite3.threadsafety < 3:
        # The endpoints are sync, so FastAPI runs them in a threadpool and they
        # share this connection. That is only safe when SQLite is serialised.
        # Fail at startup rather than return quietly corrupted reads.
        raise RuntimeError(
            f"sqlite3.threadsafety is {sqlite3.threadsafety}; the dashboard shares "
            "one connection across FastAPI's threadpool and needs a serialised build (3)"
        )
    handle: Database | None = None

    def db() -> Database:
        nonlocal handle
        if handle is None:
            handle = Database(cfg.db_path)
        return handle

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if handle is not None:
            handle.close()

    app = FastAPI(
        title="tbot dashboard", docs_url=None, redoc_url=None, lifespan=lifespan
    )

    def check_token(token: str | None) -> None:
        """Guards the two control endpoints only; reads are open on localhost."""
        if token != secrets.web_token:
            raise HTTPException(status_code=403, detail="bad token")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (STATIC / "index.html").read_text(encoding="utf-8")

    @app.get("/api/state")
    def state() -> JSONResponse:
        path = cfg.state_path
        if not path.exists():
            return JSONResponse({"status": "not_started"})
        return JSONResponse(json.loads(path.read_text(encoding="utf-8")))

    @app.get("/api/equity")
    def equity(limit: int = Query(2000, le=20000)) -> JSONResponse:
        rows = db().query(
            "SELECT ts, equity, cash, unrealized, open_positions, drawdown "
            "FROM equity ORDER BY ts DESC LIMIT ?",
            (limit,),
        )
        return JSONResponse(list(reversed(rows)))

    @app.get("/api/trades")
    def trades(limit: int = Query(200, le=5000)) -> JSONResponse:
        return JSONResponse(db().recent_trades(limit))

    @app.get("/api/scan")
    def scan() -> JSONResponse:
        """The latest bar's scan table: every symbol, its score, and why not."""
        rows = db().query(
            "SELECT * FROM scans WHERE bar_ts = (SELECT MAX(bar_ts) FROM scans) ORDER BY score DESC"
        )
        return JSONResponse(rows)

    @app.get("/api/conviction/{symbol:path}")
    def conviction(symbol: str, bar_ts: int | None = None) -> JSONResponse:
        """Full scoring breakdown — which indicator contributed what.

        This is what makes a trade explainable months later instead of being a
        line in a ledger with no reasoning attached.
        """
        if bar_ts:
            rows = db().query(
                "SELECT * FROM convictions WHERE symbol=? AND bar_ts=?", (symbol, bar_ts)
            )
        else:
            rows = db().query(
                "SELECT * FROM convictions WHERE symbol=? ORDER BY bar_ts DESC LIMIT 1",
                (symbol,),
            )
        if not rows:
            raise HTTPException(status_code=404, detail="no conviction record")
        row = rows[0]
        row["components"] = json.loads(row["components"])
        return JSONResponse(row)

    @app.get("/api/no-trade-reasons")
    def no_trade_reasons(hours: int = Query(24, le=720)) -> JSONResponse:
        """Why the bot did nothing — a query, not a guess."""
        rows = db().query(
            "SELECT no_trade_reason AS reason, COUNT(*) AS n FROM scans "
            "WHERE bar_ts > (SELECT MAX(bar_ts) - ? FROM scans) "
            "GROUP BY no_trade_reason ORDER BY n DESC",
            (hours * 3_600_000,),
        )
        return JSONResponse(rows)

    @app.get("/api/events")
    def events(limit: int = Query(100, le=1000)) -> JSONResponse:
        return JSONResponse(db().query("SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,)))

    @app.get("/api/llm")
    def llm_calls(limit: int = Query(50, le=500)) -> JSONResponse:
        return JSONResponse(
            db().query(
                "SELECT ts, role, provider, model, parsed_ok, latency_ms, symbol, "
                "decision, counterfactual_r, error FROM llm_calls "
                "ORDER BY ts DESC LIMIT ?",
                (limit,),
            )
        )

    @app.get("/api/excursion")
    def excursion() -> JSONResponse:
        """MAE/MFE scatter data — the raw material for exit calibration."""
        return JSONResponse(
            db().query(
                "SELECT symbol, setup, r_multiple, mfe_r, mae_r, bars_held, exit_reason "
                "FROM trades ORDER BY exit_ts DESC LIMIT 1000"
            )
        )

    @app.post("/api/control/{action}")
    def control(action: str, token: str = Query(...)) -> JSONResponse:
        """Write a command file for the engine to pick up on its next bar."""
        check_token(token)
        if action not in ("pause", "resume", "flatten"):
            raise HTTPException(status_code=400, detail="unknown action")
        command_file = cfg.runtime_dir / "command.json"
        command_file.write_text(json.dumps({"action": action}), encoding="utf-8")
        return JSONResponse({"ok": True, "action": action})

    return app


app = create_app()
