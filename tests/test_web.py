"""Dashboard tests.

The panel is read-mostly and runs for weeks under launchd, so the thing worth
protecting is not what an endpoint returns but what it costs to ask: a handle
opened per request is invisible in a single call and fatal after an hour.
"""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from tbot.core.config import load_config
from tbot.strategy.conviction import UNSCORED_REASONS
from tbot.web import app as web_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    cfg = load_config("config/base.yaml")
    cfg.runtime_dir = tmp_path
    opened = []

    real_database = web_app.Database

    def counting_database(path):
        opened.append(path)
        return real_database(path)

    monkeypatch.setattr(web_app, "Database", counting_database)
    with TestClient(web_app.create_app(cfg)) as c:
        yield c, opened


def test_the_panel_opens_one_database_handle_however_many_requests_it_serves(client):
    """A Database per request leaked a file descriptor per request.

    The panel polls several endpoints a second, so it reached the launchd file
    limit (256) in minutes and then went blind — "[Errno 24] Too many open
    files" on state.json, "unable to open database file" on every query — while
    KeepAlive restarted it into the same climb. A connection is process-scoped.
    """
    c, opened = client
    for _ in range(40):
        assert c.get("/api/equity").status_code == 200
        assert c.get("/api/trades").status_code == 200
        assert c.get("/api/scan").status_code == 200
        assert c.get("/api/events").status_code == 200

    assert len(opened) == 1, f"opened {len(opened)} database handles, expected 1"


def test_reads_stay_correct_when_the_handle_is_shared(client):
    """Sharing one connection across FastAPI's threadpool must not lose rows."""
    c, _ = client
    assert c.get("/api/equity").json() == []
    assert c.get("/api/no-trade-reasons").json() == []
    assert c.get("/api/conviction/BTC%2FUSDT").status_code == 404


def test_the_panel_is_told_which_reasons_mean_no_score_was_taken(client):
    """The panel must not draw a vetoed symbol as a score of zero.

    A symbol blocked before scoring has no opinion attached to it (invariant
    10); rendering "0.0" claims a measurement the engine never made, and puts
    it in the same column as a symbol that really was scored and fell short.
    The panel reads the engine's own set rather than restating it in JavaScript,
    where it would drift the first time a reason is added.
    """
    c, _ = client
    served = c.get("/api/meta").json()["unscored_reasons"]
    assert set(served) == {str(r) for r in UNSCORED_REASONS}
    assert "regime_veto" in served
    assert "below_threshold" not in served, "below_threshold is a real score, not a veto"


def test_a_serialised_sqlite_build_is_required_to_share_the_handle(tmp_path, monkeypatch):
    """The endpoints are sync, so the threadpool shares the connection.

    On a build where SQLite is not serialised that is quietly unsafe, which is
    the one failure mode a dashboard must not have: wrong numbers, no error.
    """
    cfg = load_config("config/base.yaml")
    cfg.runtime_dir = tmp_path
    monkeypatch.setattr(sqlite3, "threadsafety", 1)
    with pytest.raises(RuntimeError, match="threadsafety"):
        web_app.create_app(cfg)


# --------------------------------------------------------------------------
# Two engines, one panel
# --------------------------------------------------------------------------


def test_the_panel_serves_every_engine_not_only_its_own(tmp_path):
    """The page draws one tab per engine, so the list has to come from the
    server. A tab bar hardcoded in JavaScript would be wrong the first time
    someone added a config.
    """
    from tbot.core.config import load_config

    main = load_config("config/demo.yaml")
    main.runtime_dir = tmp_path / "main"
    fast = load_config("config/fast.yaml")
    fast.runtime_dir = tmp_path / "fast"

    with TestClient(web_app.create_app(main, engines={"main": main, "fast": fast})) as client:
        rows = client.get("/api/engines").json()
        by_name = {r["name"]: r for r in rows}
        assert set(by_name) == {"main", "fast"}
        assert by_name["main"]["primary"] is True
        assert by_name["fast"]["market"] == "linear"
        assert by_name["fast"]["port"] != by_name["main"]["port"]


def test_each_engine_reads_its_own_state_file(tmp_path):
    """One engine's positions shown under the other's equity would be worse
    than no panel at all."""
    import json

    from tbot.core.config import load_config

    main = load_config("config/demo.yaml")
    main.runtime_dir = tmp_path / "main"
    main.runtime_dir.mkdir(parents=True)
    (main.runtime_dir / "state.json").write_text(json.dumps({"status": "running", "equity": 1000.0}))

    fast = load_config("config/fast.yaml")
    fast.runtime_dir = tmp_path / "fast"
    fast.runtime_dir.mkdir(parents=True)
    (fast.runtime_dir / "state.json").write_text(json.dumps({"status": "paused", "equity": 300.0}))

    with TestClient(web_app.create_app(main, engines={"main": main, "fast": fast})) as client:
        assert client.get("/api/state").json()["equity"] == 1000.0
        assert client.get("/api/state?engine=fast").json()["equity"] == 300.0
        assert client.get("/api/state?engine=nope").status_code == 404


def test_a_control_command_reaches_only_the_engine_it_names(tmp_path):
    """pause aimed at the fast engine must not stop the one holding positions."""
    import json

    from tbot.core.config import load_config, load_secrets

    main = load_config("config/demo.yaml")
    main.runtime_dir = tmp_path / "main"
    main.runtime_dir.mkdir(parents=True)
    fast = load_config("config/fast.yaml")
    fast.runtime_dir = tmp_path / "fast"
    fast.runtime_dir.mkdir(parents=True)

    token = load_secrets().web_token
    with TestClient(web_app.create_app(main, engines={"main": main, "fast": fast})) as client:
        assert client.post(f"/api/control/pause?token={token}&engine=fast").status_code == 200

    assert (fast.runtime_dir / "command.json").exists()
    assert not (main.runtime_dir / "command.json").exists()
    assert json.loads((fast.runtime_dir / "command.json").read_text())["action"] == "pause"


def test_realised_money_is_reported_per_symbol_and_totalled(tmp_path):
    """"How much did each of these actually make me?"

    R says how a trade did against its own risk and deliberately says nothing
    about size, so a page that only speaks R cannot answer the question the
    money is in. This groups every closed trade — not a recent window, because
    a symbol's contribution is the sum of its life and a rolling cut would let
    a loser drop out of the table by simply not trading lately.
    """
    from tbot.core.models import ExitReason, Side, Trade

    cfg = load_config("config/base.yaml")
    cfg.runtime_dir = tmp_path
    db = web_app.Database(cfg.db_path)

    def trade(symbol: str, pnl: float, fees: float, exit_ts: int) -> Trade:
        return Trade(
            symbol=symbol,
            side=Side.BUY,
            setup="momentum_continuation",
            entry_ts=exit_ts - 3_600_000,
            exit_ts=exit_ts,
            entry_price=100.0,
            exit_price=100.0 + pnl,
            quantity=1.0,
            pnl=pnl,
            fees=fees,
            r_multiple=pnl / 10.0,
            mfe_r=1.0,
            mae_r=-0.2,
            bars_held=4,
            exit_reason=ExitReason.TAKE_PROFIT,
        )

    for t in (
        trade("ETH/USDT", 12.0, 0.4, 1_000),
        trade("ETH/USDT", -4.0, 0.4, 2_000),
        trade("VVV/USDT", 3.0, 0.1, 3_000),
    ):
        db.record_trade(t)
    db.close()

    with TestClient(web_app.create_app(cfg)) as client:
        body = client.get("/api/pnl").json()

    rows = {r["symbol"]: r for r in body["symbols"]}
    assert rows["ETH/USDT"]["trades"] == 2
    assert rows["ETH/USDT"]["wins"] == 1
    assert rows["ETH/USDT"]["pnl"] == pytest.approx(8.0)
    assert rows["ETH/USDT"]["fees"] == pytest.approx(0.8)
    assert rows["VVV/USDT"]["pnl"] == pytest.approx(3.0)

    # The total the operator reads, not one they have to add up by eye.
    assert body["realised_total"] == pytest.approx(11.0)
    assert body["fees_total"] == pytest.approx(0.9)
    # Best first: the question is which symbol paid, so the answer leads.
    assert [r["symbol"] for r in body["symbols"]] == ["ETH/USDT", "VVV/USDT"]


def test_percentages_are_served_the_run_s_own_starting_equity(tmp_path):
    """Invariant 33. The panel divided by a hardcoded 100 for a long time and
    showed a 1014 USDT account as +914.8%. Serving the base beside the money
    keeps the page from reaching for a number the engine never agreed to.
    """
    import json

    cfg = load_config("config/base.yaml")
    cfg.runtime_dir = tmp_path
    cfg.state_path.parent.mkdir(parents=True, exist_ok=True)

    with TestClient(web_app.create_app(cfg)) as client:
        assert client.get("/api/pnl").json()["starting_equity"] is None  # no run yet
        cfg.state_path.write_text(json.dumps({"starting_equity": 300.0}), encoding="utf-8")
        assert client.get("/api/pnl").json()["starting_equity"] == 300.0
