#!/usr/bin/env python3
"""What account size does this strategy actually need?

    python scripts/capital_check.py
    python scripts/capital_check.py --equity 250

Derived from the universe's own stop distribution, not assumed. The constraint
is arithmetic: an account can only express stops wider than
``risk_amount / min_notional``, so a small balance silently skips every
wide-stop setup.

That matters more than it sounds. Below the minimum the bot is not running a
scaled-down version of the tested strategy — it is running a different one, on a
quieter subset of the universe, and the backtest that justified it does not
apply to what it will actually trade.
"""

from __future__ import annotations

import argparse

import numpy as np
from _bootstrap import setup, universe

from tbot.data.store import ParquetStore
from tbot.indicators import ta
from tbot.risk.capital import diagnose, minimum_viable_equity
from tbot.risk.sizing import volatility_scale

BARS_PER_YEAR = {"1h": 8760, "4h": 2190, "1d": 365}


def collect_observations(cfg, store, symbols: list[str]) -> tuple[list[float], list[float]]:
    """Stop distances and the volatility scale that would apply alongside each.

    Both are needed: volatility targeting shrinks size on exactly the symbols
    that carry wide stops, so the two effects compound. Measuring stops alone
    understates what the account has to support.
    """
    stops: list[float] = []
    scales: list[float] = []
    bars_per_year = BARS_PER_YEAR.get(cfg.timeframes.trigger, 8760)

    for symbol in symbols:
        setup_df = store.read_ohlcv(symbol, cfg.timeframes.setup)
        trigger_df = store.read_ohlcv(symbol, cfg.timeframes.trigger)
        if len(setup_df) < 300 or len(trigger_df) < 200:
            continue

        atr = ta.atr(setup_df["high"], setup_df["low"], setup_df["close"], cfg.exits.atr_period)
        stop_series = (cfg.exits.initial_stop_atr_mult * atr / setup_df["close"]).dropna()

        returns = trigger_df["close"].pct_change().dropna().to_numpy()
        step = max(1, len(returns) // max(1, len(stop_series)))
        for i, stop in enumerate(stop_series.tolist()):
            window_end = min(len(returns), (i + 1) * step)
            window = returns[max(0, window_end - cfg.risk.vol_lookback_bars) : window_end]
            if window.size < 5:
                continue
            stops.append(stop)
            scales.append(
                volatility_scale(
                    window,
                    cfg.risk.vol_target_annual,
                    bars_per_year,
                    cfg.risk.vol_scale_min,
                    cfg.risk.vol_scale_max,
                )
            )

    return stops, scales


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/base.yaml")
    parser.add_argument("--equity", type=float, default=None)
    parser.add_argument("--min-notional", type=float, default=5.0)
    args = parser.parse_args()

    cfg = setup(args.config)
    store = ParquetStore(cfg.data.parquet_dir)
    symbols = universe(cfg)

    stops, scales = collect_observations(cfg, store, symbols)
    if not stops:
        print("No stored data — run scripts/backfill.py first.")
        return 1

    stop_array = np.asarray(stops)
    scale_array = np.asarray(scales)
    # What the account actually has to support: stop width divided by the size
    # multiplier that will apply at the same time.
    effective = stop_array / scale_array
    minimum = minimum_viable_equity(cfg, stops, args.min_notional, vol_scales=scales)
    quantiles = np.quantile(stop_array, [0.5, 0.75, 0.9, 0.95, 0.99])

    print(f"\nUniverse: {len(symbols)} symbols · {minimum.observed_stops:,} observed stops")
    print(
        f"Risk per trade: {cfg.risk.risk_per_trade:.2%} · "
        f"venue minimum: {minimum.min_notional:.2f} {cfg.quote_currency}\n"
    )

    print(
        "Stop distance distribution "
        f"(ATR x {cfg.exits.initial_stop_atr_mult} on {cfg.timeframes.setup}):"
    )
    for label, value in zip(("p50", "p75", "p90", "p95", "p99"), quantiles, strict=True):
        print(f"  {label}  {value:>6.1%}")

    eff_q = np.quantile(effective, [0.5, 0.75, 0.9, 0.95, 0.99])
    print("\nEffective requirement (stop / volatility scale) — what sizing really needs:")
    for label, value in zip(("p50", "p75", "p90", "p95", "p99"), eff_q, strict=True):
        print(f"  {label}  {value:>6.1%}")
    print(
        f"  volatility scale: p10 {np.quantile(scale_array, 0.1):.2f} · "
        f"median {np.median(scale_array):.2f} · p90 {np.quantile(scale_array, 0.9):.2f}"
    )

    print(
        f"\nMinimum viable equity: {minimum.minimum_equity:>8.2f} {cfg.quote_currency}"
        "   (covers 95% of setups)"
    )
    print(
        f"Comfortable:           {minimum.comfortable_equity:>8.2f} {cfg.quote_currency}"
        "   (covers 99%)"
    )

    print("\nWhat each balance can reach:")
    print(f"  {'equity':>9} {'1R':>9} {'widest stop':>12} {'setups reachable':>18}")
    print(f"  {'-' * 52}")
    for equity in (50, 100, 150, 200, 300, 500, 1000):
        d = diagnose(cfg, float(equity), args.min_notional)
        reachable = float(np.mean(effective <= d.max_stop_pct))
        flag = "" if reachable >= 0.95 else "  <- partial universe"
        print(
            f"  {equity:>9} {d.risk_amount:>9.2f} {d.max_stop_pct:>11.1%} {reachable:>17.0%}{flag}"
        )

    if args.equity:
        d = diagnose(cfg, args.equity, args.min_notional)
        reachable = float(np.mean(effective <= d.max_stop_pct))
        print(
            f"\nAt {args.equity:.2f} {cfg.quote_currency}: 1R = {d.risk_amount:.2f}, "
            f"widest stop {d.max_stop_pct:.1%}, {reachable:.0%} of setups reachable"
        )
        print(f"  {d.note}")

    print(
        "\nRaising risk per trade widens the reachable set but raises portfolio risk:\n"
        f"  {'risk/trade':>11} {'min equity':>12} {'portfolio risk':>16}"
    )
    for rpt in (0.005, 0.0075, 0.01, 0.015, 0.02):
        scaled = setup(args.config)
        scaled.risk.risk_per_trade = rpt
        m = minimum_viable_equity(scaled, stops, args.min_notional, vol_scales=scales)
        print(f"  {rpt:>10.2%} {m.minimum_equity:>12.2f} {rpt * cfg.risk.max_positions:>15.1%}")

    print(
        "\nBelow the minimum the bot does not trade a smaller version of this strategy.\n"
        "It trades a quieter subset of the universe, and the backtest does not apply to it."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
