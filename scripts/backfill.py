#!/usr/bin/env python3
"""Fetch historical candles and derivative series from Bybit into the archive.

Resumable: run it again and it continues from what is already stored, so an
interrupted run costs nothing. Writes the universe it selected to
``runtime/universe.json`` so the engine and the backtester scan the same symbols.

    python scripts/backfill.py --years 3 --with-derivatives
    python scripts/backfill.py --symbols BTC/USDT,ETH/USDT --tf 1h,4h,1d
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tbot.core.clock import iso, now_ms  # noqa: E402
from tbot.core.config import load_config  # noqa: E402
from tbot.core.logging import configure, get_logger  # noqa: E402
from tbot.data.backfill import Backfiller, BybitFetcher  # noqa: E402
from tbot.data.integrity import check_store, derivative_coverage, write_report  # noqa: E402
from tbot.data.store import ParquetStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/base.yaml")
    parser.add_argument("--symbols", default="", help="comma-separated; default = auto universe")
    parser.add_argument("--tf", default="", help="comma-separated timeframes; default = config")
    parser.add_argument("--years", type=float, default=None)
    parser.add_argument("--with-derivatives", action="store_true")
    parser.add_argument("--no-report", action="store_true")
    parser.add_argument("--refresh-universe", action="store_true")
    args = parser.parse_args()

    cfg = load_config(args.config)
    configure(cfg.log_dir, level=cfg.log_level)
    log = get_logger("backfill")

    years = args.years if args.years is not None else cfg.data.backfill_years
    timeframes = [t.strip() for t in args.tf.split(",") if t.strip()] or cfg.timeframes.all

    fetcher = BybitFetcher(rate_limit_ms=cfg.data.rest_rate_limit_ms)
    store = ParquetStore(cfg.data.parquet_dir)
    backfiller = Backfiller(fetcher, store)

    universe_path = cfg.runtime_dir / "universe.json"

    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    elif cfg.universe.symbols:
        symbols = cfg.universe.symbols
    elif universe_path.exists() and not args.refresh_universe:
        symbols = json.loads(universe_path.read_text())["symbols"]
        log.info("universe_loaded_from_disk", count=len(symbols))
    else:
        symbols = fetcher.select_universe(
            top_n=cfg.universe.auto_top_n,
            quote=cfg.universe.quote,
            min_volume_24h=cfg.universe.min_volume_24h_usd,
            exclude=cfg.universe.exclude,
            min_history_days=cfg.universe.min_history_days,
        )
        universe_path.parent.mkdir(parents=True, exist_ok=True)
        universe_path.write_text(
            json.dumps({"selected_ts": now_ms(), "symbols": symbols}, indent=2)
        )

    if not symbols:
        log.error("empty_universe", hint="lower universe.min_volume_24h_usd or min_history_days")
        return 1

    print(f"\nUniverse: {len(symbols)} symbols | timeframes: {timeframes} | {years}y\n")
    started = time.time()
    failures: list[str] = []

    for i, symbol in enumerate(symbols, 1):
        for timeframe in timeframes:
            result = backfiller.backfill_symbol(symbol, timeframe, years=years)
            if result.error:
                failures.append(f"{symbol} {timeframe}: {result.error}")
                status = f"ERROR {result.error[:50]}"
            else:
                span = (
                    f"{iso(result.first_ts)[:10]}..{iso(result.last_ts)[:10]}"
                    if result.first_ts
                    else "—"
                )
                status = f"{result.total_bars:>6} bars  {span}  (+{result.bars_fetched})"
            print(f"[{i:>2}/{len(symbols)}] {symbol:<12} {timeframe:<4} {status}")

        if args.with_derivatives or cfg.data.fetch_derivatives:
            counts = backfiller.backfill_derivatives(symbol, years=years)
            print(
                f"           {'':<12} deriv funding={counts['funding']:>5} "
                f"oi={counts['open_interest']:>5} ls={counts['long_short']:>5}"
            )

    elapsed = time.time() - started
    print(f"\nFetched in {elapsed:.0f}s. Failures: {len(failures)}")
    for failure in failures:
        print(f"  ! {failure}")

    if not args.no_report:
        reports = check_store(store, symbols, timeframes, max_gap_ratio=cfg.data.max_gap_ratio)
        deriv = derivative_coverage(store, symbols) if cfg.data.fetch_derivatives else None
        path = write_report(reports, deriv, cfg.reports_dir / "integrity.md")
        bad = [r for r in reports if not r.ok]
        print(f"\nIntegrity: {len(reports) - len(bad)}/{len(reports)} clean -> {path}")
        for r in bad:
            print(f"  ! {r.symbol} {r.timeframe}: {'; '.join(r.issues)}")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
