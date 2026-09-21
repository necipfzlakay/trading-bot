"""Smoke tests for the command-line entry points.

The CLIs were only ever exercised by hand. These import each one, check it
parses arguments, and verify the shared bootstrap does what every script relies
on — so a rename in the library cannot silently break a script that is only run
once a month.
"""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = sorted(p for p in (ROOT / "scripts").glob("*.py") if not p.name.startswith("_"))


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_script_help_runs(script):
    """--help exercises imports and the argument parser without side effects."""
    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=ROOT,
    )
    assert result.returncode == 0, f"{script.name} --help failed:\n{result.stderr[-1500:]}"
    assert "usage:" in result.stdout.lower()


def test_every_script_is_executable():
    for script in SCRIPTS:
        assert script.stat().st_mode & 0o111, f"{script.name} is not executable"


def test_bootstrap_loads_config_and_universe():
    sys.path.insert(0, str(ROOT / "scripts"))
    from _bootstrap import indicator_selection, setup, universe

    cfg = setup("config/base.yaml")
    assert cfg.starting_balance > 0

    symbols = universe(cfg)
    assert isinstance(symbols, list)

    blocks, selected, decay = indicator_selection(cfg)
    assert isinstance(blocks, dict)
    assert isinstance(selected, dict)
    assert isinstance(decay, dict)


def test_indicator_selection_falls_back_cleanly(tmp_path, monkeypatch):
    """No selection file means every indicator votes — the honest fallback.

    Silently filtering with a stale or absent measurement would be worse: the
    engine would claim a selection it never made.
    """
    sys.path.insert(0, str(ROOT / "scripts"))
    from _bootstrap import indicator_selection, setup

    cfg = setup("config/base.yaml")
    cfg.runtime_dir = tmp_path
    blocks, selected, decay = indicator_selection(cfg)
    assert blocks == {}
    assert selected == {}
    assert decay == {}


def test_all_shipped_configs_are_loadable_by_the_cli():
    sys.path.insert(0, str(ROOT / "scripts"))
    from _bootstrap import setup

    for name in ("base", "demo", "live", "slow"):
        cfg = setup(f"config/{name}.yaml")
        assert cfg.timeframes.all


def test_healthcheck_reports_unhealthy_without_a_running_engine(tmp_path):
    sys.path.insert(0, str(ROOT / "scripts"))
    from healthcheck import check_freshness

    from tbot.core.config import load_config

    cfg = load_config("config/base.yaml")
    cfg.runtime_dir = tmp_path
    healthy, message, _ = check_freshness(cfg, max_age=3600)
    assert not healthy
    assert "never run" in message


def test_capital_diagnostic_flags_an_account_that_is_too_small():
    from tbot.core.config import load_config
    from tbot.risk.capital import diagnose

    cfg = load_config("config/base.yaml")

    tiny = diagnose(cfg, equity=20.0)
    assert not tiny.usable
    assert "silently skipped" in tiny.note

    comfortable = diagnose(cfg, equity=5000.0)
    assert comfortable.usable
    assert comfortable.max_stop_pct > tiny.max_stop_pct


def test_capital_diagnostic_matches_the_sizing_rule():
    """The diagnostic must agree with what sizing actually does."""
    from tbot.core.config import load_config
    from tbot.core.models import MarketSpec
    from tbot.risk.capital import diagnose
    from tbot.risk.sizing import size_position

    cfg = load_config("config/base.yaml")
    spec = MarketSpec(
        symbol="X/USDT",
        base="X",
        quote="USDT",
        min_notional=5.0,
        min_amount=1e-9,
        amount_step=1e-9,
        price_step=1e-6,
        maker_fee=0.001,
        taker_fee=0.001,
    )
    diagnostic = diagnose(cfg, equity=100.0)

    just_inside = size_position(
        equity=100.0,
        risk_amount=diagnostic.risk_amount,
        entry_price=100.0,
        stop_price=100.0 * (1 - diagnostic.max_stop_pct * 0.95),
        spec=spec,
        config=cfg.risk,
        min_notional_buffer=cfg.execution.min_notional_buffer,
    )
    just_outside = size_position(
        equity=100.0,
        risk_amount=diagnostic.risk_amount,
        entry_price=100.0,
        stop_price=100.0 * (1 - diagnostic.max_stop_pct * 1.10),
        spec=spec,
        config=cfg.risk,
        min_notional_buffer=cfg.execution.min_notional_buffer,
    )
    assert just_inside.ok
    assert not just_outside.ok


def test_healthcheck_process_lookup_is_scoped_to_one_engine(monkeypatch):
    """A dead-man switch that cannot tell two engines apart is not one.

    ``pgrep -f scripts/run.py`` answers about whichever engine started first.
    Run against the stopped engine while the other one was up, it reported the
    *other* engine's pid as proof of life — so the one check that exists to
    notice a vanished engine would have said it was fine.
    """
    sys.path.insert(0, str(ROOT / "scripts"))
    import healthcheck

    seen = {}

    class Result:
        stdout = ""

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return Result()

    monkeypatch.setattr(healthcheck.subprocess, "run", fake_run)

    alive, message = healthcheck.check_process("config/fast.yaml")

    assert seen["cmd"][:2] == ["pgrep", "-f"]
    assert seen["cmd"][2] == "scripts/run.py --config config/fast.yaml"
    assert not alive
    assert "config/fast.yaml" in message, "the message must name which engine is missing"
