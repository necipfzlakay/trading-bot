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

from tbot.core.config import PROJECT_ROOT, Config, load_config, load_secrets
from tbot.core.db import Database
from tbot.strategy.conviction import UNSCORED_REASONS

STATIC = Path(__file__).parent / "static"


def engine_key(cfg: Config) -> str:
    """How the panel names an engine. The namespace is what makes it separate."""
    return cfg.engine.namespace or "main"


def discover_engines() -> dict[str, Config]:
    """Every engine defined in ``config/``, keyed by namespace.

    Keyed by namespace rather than by filename because that is what actually
    separates two engines: runtime directory, database, state file and launchd
    labels all follow from it. ``demo`` and ``live`` share the default
    namespace, so they are one engine as far as the panel is concerned — they
    read the same ``runtime/`` and only one can be installed at a time. Which of
    the two wins the key is therefore irrelevant: the panel never reads ``mode``
    or ``venue`` from a config, only from the ``state.json`` the running engine
    writes.
    """
    engines: dict[str, Config] = {}
    config_dir = PROJECT_ROOT / "config"
    if not config_dir.is_dir():
        return engines
    for path in sorted(config_dir.glob("*.yaml")):
        if path.stem == "base":
            continue
        try:
            cfg = load_config(str(path))
        except Exception:  # noqa: BLE001 — a broken config must not blind the panel
            continue
        engines.setdefault(engine_key(cfg), cfg)
    return engines


def create_app(config: Config | None = None, engines: dict[str, Config] | None = None) -> FastAPI:
    cfg = config or load_config("config/base.yaml")
    secrets = load_secrets()

    # The panel serves every engine, not only the one it was started for, so a
    # single page can show both and compare them. The config it was handed always
    # wins its own key: tests and one-off runs point it at a runtime directory
    # that discovery would otherwise overwrite with the checked-in one.
    known: dict[str, Config] = dict(engines if engines is not None else discover_engines())
    primary = engine_key(cfg)
    known[primary] = cfg

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
    # One connection per engine, opened on first use and closed at shutdown —
    # the same contract as before, now held per engine rather than once.
    handles: dict[str, Database] = {}

    def engine_config(name: str | None) -> Config:
        chosen = known.get(name or primary)
        if chosen is None:
            raise HTTPException(status_code=404, detail=f"unknown engine: {name}")
        return chosen

    def db(name: str | None = None) -> Database:
        target = engine_config(name)
        key = engine_key(target)
        if key not in handles:
            handles[key] = Database(target.db_path)
        return handles[key]

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        for handle in handles.values():
            handle.close()
        handles.clear()

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

    @app.get("/api/engines")
    def engines_list() -> JSONResponse:
        """Which engines exist, so the page can draw one tab per engine.

        Served rather than hardcoded in the page: an engine is created by adding
        a config, and a tab bar that has to be edited to match would be wrong
        the first time someone did that.
        """
        rows = []
        for name in sorted(known, key=lambda n: (n != "main", n)):
            target = known[name]
            state_path = target.state_path
            live = {}
            if state_path.exists():
                try:
                    raw = json.loads(state_path.read_text(encoding="utf-8"))
                    live = {
                        "status": raw.get("status"),
                        "mode": raw.get("mode"),
                        "venue": raw.get("venue"),
                        "equity": raw.get("equity"),
                        "starting_equity": raw.get("starting_equity"),
                        "updated_ts": raw.get("updated_ts"),
                        "open_positions": len(raw.get("open_positions") or []),
                    }
                except (OSError, ValueError):
                    live = {}
            rows.append(
                {
                    "name": name,
                    "namespace": target.engine.namespace,
                    "primary": name == primary,
                    "market": target.execution.market_type,
                    "port": target.web.port,
                    "runtime_dir": target.runtime_dir.name,
                    "state": live,
                }
            )
        return JSONResponse(rows)

    @app.get("/api/state")
    def state(engine: str | None = Query(None)) -> JSONResponse:
        path = engine_config(engine).state_path
        if not path.exists():
            return JSONResponse({"status": "not_started"})
        return JSONResponse(json.loads(path.read_text(encoding="utf-8")))

    @app.get("/api/equity")
    def equity(limit: int = Query(2000, le=20000), engine: str | None = Query(None)) -> JSONResponse:
        rows = db(engine).query(
            "SELECT ts, equity, cash, unrealized, open_positions, drawdown "
            "FROM equity ORDER BY ts DESC LIMIT ?",
            (limit,),
        )
        return JSONResponse(list(reversed(rows)))

    @app.get("/api/trades")
    def trades(limit: int = Query(200, le=5000), engine: str | None = Query(None)) -> JSONResponse:
        return JSONResponse(db(engine).recent_trades(limit))

    @app.get("/api/pnl")
    def pnl(engine: str | None = Query(None)) -> JSONResponse:
        """Realised money per symbol, plus the run's starting equity.

        Only the *closed* half lives here. Open positions are marked with the
        same venue price the positions table uses, so computing them here would
        put a second, slightly different unrealised number on the page — and
        two numbers for one quantity is worse than one.

        ``starting_equity`` rides along because every percentage on the page is
        measured against it (invariant 33). Serving it beside the money keeps
        the panel from reaching for a base the engine never agreed to.
        """
        rows = db(engine).realised_by_symbol()
        path = engine_config(engine).state_path
        snapshot = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        return JSONResponse(
            {
                "symbols": rows,
                "starting_equity": snapshot.get("starting_equity"),
                "realised_total": sum(float(r["pnl"] or 0.0) for r in rows),
                "fees_total": sum(float(r["fees"] or 0.0) for r in rows),
            }
        )

    @app.get("/api/meta")
    def meta() -> JSONResponse:
        """Constants the panel must agree with the engine about.

        The panel has to tell "blocked before scoring" apart from "scored and
        came up short" — a vetoed symbol has no opinion, not a score of zero
        (invariant 10). Serving the engine's own set beats restating it in
        JavaScript, where it would drift the first time a reason is added.
        """
        return JSONResponse({"unscored_reasons": sorted(str(r) for r in UNSCORED_REASONS)})

    @app.get("/api/scan")
    def scan(engine: str | None = Query(None)) -> JSONResponse:
        """The latest bar's scan table: every symbol, its score, and why not."""
        rows = db(engine).query(
            "SELECT * FROM scans WHERE bar_ts = (SELECT MAX(bar_ts) FROM scans) ORDER BY score DESC"
        )
        return JSONResponse(rows)

    @app.get("/api/conviction/{symbol:path}")
    def conviction(symbol: str, bar_ts: int | None = None, engine: str | None = Query(None)) -> JSONResponse:
        """Full scoring breakdown — which indicator contributed what.

        This is what makes a trade explainable months later instead of being a
        line in a ledger with no reasoning attached.
        """
        if bar_ts:
            rows = db(engine).query(
                "SELECT * FROM convictions WHERE symbol=? AND bar_ts=?", (symbol, bar_ts)
            )
        else:
            rows = db(engine).query(
                "SELECT * FROM convictions WHERE symbol=? ORDER BY bar_ts DESC LIMIT 1",
                (symbol,),
            )
        if not rows:
            raise HTTPException(status_code=404, detail="no conviction record")
        row = rows[0]
        row["components"] = json.loads(row["components"])
        return JSONResponse(row)

    @app.get("/api/no-trade-reasons")
    def no_trade_reasons(hours: int = Query(24, le=720), engine: str | None = Query(None)) -> JSONResponse:
        """Why the bot did nothing — a query, not a guess."""
        rows = db(engine).query(
            "SELECT no_trade_reason AS reason, COUNT(*) AS n FROM scans "
            "WHERE bar_ts > (SELECT MAX(bar_ts) - ? FROM scans) "
            "GROUP BY no_trade_reason ORDER BY n DESC",
            (hours * 3_600_000,),
        )
        return JSONResponse(rows)

    @app.get("/api/events")
    def events(limit: int = Query(100, le=1000), engine: str | None = Query(None)) -> JSONResponse:
        return JSONResponse(
            db(engine).query("SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,))
        )

    @app.get("/api/llm")
    def llm_calls(limit: int = Query(50, le=500), engine: str | None = Query(None)) -> JSONResponse:
        return JSONResponse(
            db(engine).query(
                "SELECT ts, role, provider, model, parsed_ok, latency_ms, symbol, "
                "decision, counterfactual_r, error FROM llm_calls "
                "ORDER BY ts DESC LIMIT ?",
                (limit,),
            )
        )

    @app.get("/api/excursion")
    def excursion(engine: str | None = Query(None)) -> JSONResponse:
        """MAE/MFE scatter data — the raw material for exit calibration."""
        return JSONResponse(
            db(engine).query(
                "SELECT symbol, setup, r_multiple, mfe_r, mae_r, bars_held, exit_reason "
                "FROM trades ORDER BY exit_ts DESC LIMIT 1000"
            )
        )

    @app.post("/api/control/{action}")
    def control(action: str, token: str = Query(...), engine: str | None = Query(None)) -> JSONResponse:
        """Write a command file for the engine to pick up on its next bar."""
        check_token(token)
        if action not in ("pause", "resume", "flatten"):
            raise HTTPException(status_code=400, detail="unknown action")
        command_file = engine_config(engine).runtime_dir / "command.json"
        command_file.write_text(json.dumps({"action": action}), encoding="utf-8")
        return JSONResponse({"ok": True, "action": action})

    return app
