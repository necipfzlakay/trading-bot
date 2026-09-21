"""Capital between engines that share one Unified wallet.

Three engines, one USDT balance. Each owns a slice, and the slices have to stay
separate across restarts — otherwise one engine's losses become another's
starting capital, and a kill criterion measured on equity can never be reached.
"""

from __future__ import annotations

import json

import pytest

from tbot.core.config import load_config
from tbot.engine.siblings import (
    STATE_FRESH_MS,
    own_starting_cash,
    sibling_reservation,
)

NOW = 1_790_000_000_000


def _sibling(tmp_path, namespace: str, cap: float, state: dict | None):
    cfg = load_config(
        "config/fast.yaml",
        overrides={
            "engine": {"namespace": namespace},
            "risk": {"max_capital": cap},
            "runtime_dir": str(tmp_path / namespace),
        },
    )
    cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
    if state is not None:
        cfg.state_path.write_text(json.dumps(state))
    return cfg


# --------------------------------------------------------------------------
# Capped engines continue from their own equity
# --------------------------------------------------------------------------


def test_a_capped_engine_that_lost_money_does_not_refill_at_restart():
    """The kill criterion depends on this.

    The seed used to be ``min(cap, free)``: an engine at $500 of a $1000
    allocation restarted at $1000, refilled from the shared wallet, and an
    equity floor of $500 was unreachable by construction.
    """
    cash = own_starting_cash(
        venue_free=5_000.0, positions_value=0.0, cap=1_000.0,
        last_equity=500.0, reserved_for_siblings=0.0,
    )
    assert cash == 500.0


def test_open_positions_count_inside_the_equity_not_on_top_of_it():
    """Measured: the fast engine seeded 313.94 on a 300 cap — cash capped, the
    open SOL position's 112.09 added on top of it."""
    cash = own_starting_cash(
        venue_free=1_500.0, positions_value=112.09, cap=300.0,
        last_equity=300.0, reserved_for_siblings=0.0,
    )
    assert cash + 112.09 == pytest.approx(300.0)


def test_a_capped_engine_keeps_its_own_profit_across_restarts():
    """With every engine capped, clipping at the cap would take each engine's
    profit away at every restart — and the main engine's whole edge is in the
    long winners that span restarts (invariant 51)."""
    cash = own_starting_cash(
        venue_free=5_000.0, positions_value=0.0, cap=1_000.0,
        last_equity=1_100.0, reserved_for_siblings=0.0,
    )
    assert cash == 1_100.0


def test_a_capped_engine_never_seeds_more_than_the_wallet_has_free():
    cash = own_starting_cash(
        venue_free=120.0, positions_value=0.0, cap=300.0,
        last_equity=300.0, reserved_for_siblings=0.0,
    )
    assert cash == 120.0


def test_a_capped_engine_on_its_first_run_starts_at_the_cap():
    assert own_starting_cash(
        venue_free=5_000.0, positions_value=0.0, cap=300.0,
        last_equity=None, reserved_for_siblings=0.0,
    ) == 300.0


# --------------------------------------------------------------------------
# The residual engine owns what the others do not
# --------------------------------------------------------------------------


def test_the_residual_engine_does_not_adopt_its_siblings_money():
    """After the $1300 top-up: fast owns 300, scalp 1000, main what is left."""
    assert own_starting_cash(
        venue_free=1_616.12, positions_value=0.0, cap=0.0,
        last_equity=1_021.0, reserved_for_siblings=1_300.0,
    ) == pytest.approx(316.12)


def test_a_running_sibling_reserves_its_reported_cash(tmp_path):
    fresh = {"cash": 187.9, "equity": 300.0, "updated_ts": NOW - 60_000}
    siblings = [_sibling(tmp_path, "fast", 300.0, fresh)]
    total, breakdown = sibling_reservation(siblings[0], siblings=siblings, now=NOW)
    assert total == pytest.approx(187.9)
    assert breakdown == {"fast": 187.9}


def test_a_sibling_that_is_down_or_never_ran_reserves_its_whole_allocation(tmp_path):
    """A stopped engine still owns its money."""
    stale = {"cash": 12.0, "updated_ts": NOW - STATE_FRESH_MS - 1}
    siblings = [
        _sibling(tmp_path, "fast", 300.0, stale),
        _sibling(tmp_path, "sc", 1_000.0, None),
    ]
    total, breakdown = sibling_reservation(siblings[0], siblings=siblings, now=NOW)
    assert breakdown == {"fast": 300.0, "sc": 1_000.0}
    assert total == 1_300.0


def test_an_uncapped_sibling_reserves_nothing_and_says_so(tmp_path):
    """Two engines each owning 'the rest' cannot both be right; it is logged,
    not silently split."""
    siblings = [_sibling(tmp_path, "other", 0.0, None)]
    total, breakdown = sibling_reservation(siblings[0], siblings=siblings, now=NOW)
    assert total == 0.0 and breakdown == {}
