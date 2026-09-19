"""Dashboard tests.

The panel is read-mostly and runs for weeks under launchd, so the thing worth
protecting is not what an endpoint returns but what it costs to ask: a handle
opened per request is invisible in a single call and fatal after an hour.
"""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from tbot.core.config import load_config
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
