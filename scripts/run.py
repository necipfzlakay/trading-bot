#!/usr/bin/env python3
"""Start the trading engine.

    python scripts/run.py --mode paper
    python scripts/run.py --config config/demo.yaml
    TBOT_CONFIRM_LIVE=yes python scripts/run.py --config config/live.yaml

Runs until stopped by hand or by signal. Under launchd it comes straight back
after any crash, which is the intended behaviour.
"""

from __future__ import annotations

import argparse

from _bootstrap import indicator_selection, setup, universe

from tbot.core.config import assert_runnable, load_secrets
from tbot.core.db import Database
from tbot.core.logging import get_logger
from tbot.core.models import Mode, Venue
from tbot.data.store import ParquetStore
from tbot.engine.lock import AlreadyRunning, SingleInstanceLock
from tbot.engine.runner import Runner, build_broker, venue_label
from tbot.risk.capital import diagnose as diagnose_capital


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/paper.yaml")
    parser.add_argument("--mode", choices=[m.value for m in Mode])
    parser.add_argument("--venue", choices=[v.value for v in Venue])
    parser.add_argument("--symbols", default="")
    args = parser.parse_args()

    cfg = setup(args.config)
    if args.mode:
        cfg.mode = Mode(args.mode)
    if args.venue:
        cfg.venue = Venue(args.venue)

    secrets = load_secrets()
    assert_runnable(cfg, secrets)  # refuses live money without an explicit opt-in

    log = get_logger("run")
    symbols = (
        [s.strip() for s in args.symbols.split(",") if s.strip()] if args.symbols else universe(cfg)
    )
    if not symbols:
        log.error("empty_universe", hint="run scripts/backfill.py first")
        return 1

    blocks, selected, decay = indicator_selection(cfg)
    if not blocks:
        log.warning(
            "no_indicator_selection",
            hint="run scripts/ic_report.py — without it every indicator votes separately",
        )

    # Acquire before announcing anything: a blocked second instance printing a
    # startup banner reads as "it started", which is exactly wrong.
    lock = SingleInstanceLock(cfg.runtime_dir / "engine.lock")
    try:
        lock.acquire()
    except AlreadyRunning as exc:
        print(f"\n{exc}\n")
        return 1

    print(f"\n  mode    {cfg.mode.value}")
    print(f"  venue   {cfg.venue.value}  [{venue_label(cfg.venue)}]")
    print(f"  symbols {len(symbols)}")
    print(f"  equity  {cfg.starting_balance} {cfg.quote_currency}")
    print(
        f"  bar     {cfg.timeframes.trigger} (setup {cfg.timeframes.setup}, "
        f"permission {cfg.timeframes.permission})\n"
    )

    # A small account does not just trade smaller — it silently trades a
    # different universe, because a risk-correct size can fall below the venue
    # minimum. Say so up front rather than letting it be discovered later.
    diagnostic = diagnose_capital(cfg, cfg.starting_balance)
    marker = " " if diagnostic.usable else "!"
    print(
        f"  risk  {marker} {diagnostic.risk_amount:.2f} {cfg.quote_currency}/trade · "
        f"widest usable stop {diagnostic.max_stop_pct:.1%}"
    )
    if not diagnostic.usable:
        print(f"          {diagnostic.note}")
    print()

    db = Database(cfg.db_path)
    store = ParquetStore(cfg.data.parquet_dir)
    broker = build_broker(cfg, secrets, symbols)

    runner = Runner(
        config=cfg,
        secrets=secrets,
        db=db,
        store=store,
        broker=broker,
        symbols=symbols,
        block_lookup=blocks,
        selected_by_regime=selected,
        edge_decay_bars=decay,
    )
    try:
        runner.start()
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
