"""Symbol claims: two perp engines on one account, one position per symbol.

Bybit's linear market is one-way: the venue holds a single position per symbol
and nets whatever two engines send into it. Every test here guards one clause of
the operator's rule — first to open owns it, the other leaves it alone, it is
free again once flat.
"""

from __future__ import annotations

import multiprocessing as mp

import pytest

from tbot.core.config import load_config
from tbot.core.models import Side
from tbot.engine.claims import SymbolClaims
from tbot.engine.siblings import assert_claims_consistent


def _race(path: str, owner: str, start, results) -> None:
    claims = SymbolClaims(path, owner)
    start.wait()
    results.put((owner, claims.claim("SOL/USDT:USDT")))
    claims.close()


def test_two_processes_racing_for_one_symbol_produce_exactly_one_owner(tmp_path):
    """The property everything else rests on, tested across real processes.

    Threads would share one connection and prove nothing about SQLite's
    cross-process locking, which is the thing two launchd services depend on.
    """
    path = str(tmp_path / "claims.sqlite")
    SymbolClaims(path, "setup").close()  # schema exists before the race

    ctx = mp.get_context("spawn")
    start, results = ctx.Event(), ctx.Queue()
    workers = [ctx.Process(target=_race, args=(path, o, start, results)) for o in ("fa", "sc")]
    for w in workers:
        w.start()
    start.set()
    outcomes = dict(results.get(timeout=20) for _ in workers)
    for w in workers:
        w.join(timeout=20)

    assert sorted(outcomes.values()) == [False, True]
    winner = next(o for o, won in outcomes.items() if won)
    assert SymbolClaims(path, "check").owner_of("SOL/USDT:USDT") == winner


def test_a_claim_is_idempotent_for_its_owner_and_exclusive_for_everyone_else(tmp_path):
    path = tmp_path / "claims.sqlite"
    fast, scalp = SymbolClaims(path, "fast"), SymbolClaims(path, "sc")

    assert fast.claim("SOL/USDT:USDT") is True
    assert fast.claim("SOL/USDT:USDT") is True  # a restart re-claiming its own
    assert scalp.claim("SOL/USDT:USDT") is False
    assert scalp.held_by_others() == {"SOL/USDT:USDT": "fast"}


def test_only_the_owner_can_release(tmp_path):
    """Another engine releasing the symbol would hand it on while it carries size."""
    path = tmp_path / "claims.sqlite"
    fast, scalp = SymbolClaims(path, "fast"), SymbolClaims(path, "sc")
    fast.claim("SOL/USDT:USDT")

    assert scalp.release("SOL/USDT:USDT") is False
    assert fast.owner_of("SOL/USDT:USDT") == "fast"
    assert fast.release("SOL/USDT:USDT") is True
    assert scalp.claim("SOL/USDT:USDT") is True  # free again once released


def test_spot_and_perp_of_the_same_coin_are_different_symbols(tmp_path):
    """They are different instruments and cannot net, so they must not block."""
    claims = SymbolClaims(tmp_path / "c.sqlite", "fast")
    other = SymbolClaims(tmp_path / "c.sqlite", "sc")
    assert claims.claim("SOL/USDT:USDT")
    assert other.claim("SOL/USDT")


def test_an_empty_owner_is_refused(tmp_path):
    """An empty namespace would match every other empty namespace."""
    with pytest.raises(ValueError):
        SymbolClaims(tmp_path / "c.sqlite", "")


# --------------------------------------------------------------------------
# Startup consistency
# --------------------------------------------------------------------------


def _perp(namespace: str, claims: bool, path: str = "runtime-shared/claims.sqlite"):
    return load_config(
        "config/fast.yaml",
        overrides={"engine": {"namespace": namespace, "symbol_claims": claims, "claims_path": path}},
    )


def test_a_perp_engine_beside_one_that_ignores_claims_refuses_to_start():
    """One engine honouring claims protects nothing: the other opens regardless."""
    with pytest.raises(RuntimeError, match="symbol_claims"):
        assert_claims_consistent(_perp("fast", True), siblings=[_perp("sc", False)])


def test_perp_engines_must_share_one_claim_table():
    with pytest.raises(RuntimeError, match="one claim table"):
        assert_claims_consistent(
            _perp("fast", True, "/tmp/a.sqlite"), siblings=[_perp("sc", True, "/tmp/b.sqlite")]
        )


def test_a_perp_engine_trading_alone_needs_no_claims():
    assert_claims_consistent(_perp("fast", False), siblings=[])


# --------------------------------------------------------------------------
# Runner wiring
# --------------------------------------------------------------------------


def _runner_with_claims(tmp_path, venue_positions: dict[str, float]):
    from tbot.core.config import Config
    from tbot.engine.portfolio import Portfolio
    from tbot.engine.runner import Runner

    events: list[str] = []

    class Database:
        def record_event(self, name, *_a, **_k):
            events.append(name)

    runner = Runner.__new__(Runner)
    runner.config = Config()
    runner.db = Database()
    runner.portfolio = Portfolio(starting_cash=300.0)
    runner.claims = SymbolClaims(tmp_path / "claims.sqlite", "fast")
    runner.broker = type("B", (), {"fetch_positions": lambda self: dict(venue_positions)})()
    runner.watchdog = type("W", (), {"call": lambda self, _n, fn, default=None: fn()})()
    return runner, events


def test_a_stale_claim_is_released_only_once_the_venue_is_flat(tmp_path):
    """An entry whose answer was lost may still have filled.

    Releasing its claim on the local book's word alone would let the other
    engine open the same symbol, and the venue would net the two.
    """
    runner, events = _runner_with_claims(tmp_path, {"SOL/USDT:USDT": 1.0})
    runner.claims.claim("SOL/USDT:USDT")

    runner._sweep_claims()
    assert runner.claims.owner_of("SOL/USDT:USDT") == "fast"  # size at the venue: kept

    runner.broker = type("B", (), {"fetch_positions": lambda self: {}})()
    runner._sweep_claims()
    assert runner.claims.owner_of("SOL/USDT:USDT") is None  # flat: released


def test_an_unreadable_venue_releases_nothing(tmp_path):
    runner, _ = _runner_with_claims(tmp_path, {})
    runner.claims.claim("SOL/USDT:USDT")
    runner.broker = type("B", (), {"fetch_positions": lambda self: None})()
    runner._sweep_claims()
    assert runner.claims.owner_of("SOL/USDT:USDT") == "fast"


def test_an_entry_into_a_claimed_symbol_is_skipped_with_a_reason(tmp_path):
    """The scan row must say why, not read as ``eligible`` (invariant 34)."""
    from tbot.core.config import Config
    from tbot.core.models import NoTradeReason, Regime, ScanRow
    from tests.test_short_and_perp import _downtrend_frames, _live_runner

    SymbolClaims(tmp_path / "claims.sqlite", "sc").claim("SOL/USDT:USDT")

    config = Config()
    config.entries.allow_short = True
    runner = _live_runner(config)
    runner.claims = SymbolClaims(tmp_path / "claims.sqlite", "fast")
    sent: list = []

    from tbot.core.models import MarketSpec
    from tbot.execution.broker import Broker

    spec = MarketSpec(
        symbol="SOL/USDT:USDT", base="SOL", quote="USDT", min_notional=5.0,
        min_amount=0.1, amount_step=0.1, price_step=0.01, maker_fee=0.0001, taker_fee=0.0006,
    )
    class Venue:
        market_spec = staticmethod(lambda _s: spec)
        build_order = Broker.build_order
        submit = staticmethod(lambda o: sent.append(o) or o)

    runner.broker = Venue()
    row = ScanRow(
        symbol="SOL/USDT:USDT", bar_ts=1_758_300_000_000, regime=Regime.TREND_DOWN,
        best_side=Side.SELL, score=80.0, threshold=62.0, no_trade_reason=None,
    )
    data = _downtrend_frames()["SOL/USDT:USDT"]

    assert runner._enter(data, row, bar_ts=1_758_300_000_000) is False
    assert row.no_trade_reason is NoTradeReason.CLAIMED_BY_OTHER_ENGINE
    assert sent == []  # nothing left for the venue to net
