#!/usr/bin/env python3
"""H19 / H20 / H21 on the archive — once, with the pre-registered parameters.

    python scripts/rebalance_research.py

Reads the spot daily archive (``runtime/data``) and the perpetual funding
archive (``runtime-fast/data``), replays each strategy with the numbers in
``RebalanceConfig``'s defaults, and prints the result beside its benchmark and
split into halves. Nothing here is a search: the parameters were fixed before
this was first run (``research/hypotheses.md``). Changing one and re-running
is a new trial, and the DSR counts it.
"""

from __future__ import annotations

import argparse
import json
import math

import pandas as pd
from _bootstrap import ROOT

from tbot.core.config import RebalanceConfig
from tbot.rebalance import backtest as bt
from tbot.rebalance.signals import DAY_MS

SPOT_COST = 0.001 + 0.0005  # taker + 5 bps slippage, per side
PERP_COST = 0.00055 + 0.0005  # measured perp taker + 5 bps


def load_bars(symbols: list[str]) -> bt.Bars:
    frames = {}
    for symbol in symbols:
        path = ROOT / "runtime/data/ohlcv" / symbol.replace("/", "_") / "1d.parquet"
        frames[symbol] = pd.read_parquet(path).set_index("ts")
    days = sorted(set().union(*(f.index for f in frames.values())))
    nan = float("nan")
    return bt.Bars(
        days=[int(d) for d in days],
        open={s: [float(f["open"].get(d, nan)) for d in days] for s, f in frames.items()},
        close={s: [float(f["close"].get(d, nan)) for d in days] for s, f in frames.items()},
    )


def load_funding(symbols: list[str]) -> dict[str, list[tuple[int, float]]]:
    out = {}
    for symbol in symbols:
        path = ROOT / "runtime-fast/data/derivatives" / (symbol.replace("/", "_") + "_USDT")
        path = path / "funding.parquet"
        if path.exists():
            frame = pd.read_parquet(path)
            out[symbol] = [(int(t), float(r)) for t, r in zip(frame["ts"], frame["funding_rate"], strict=True)]
    return out


def show(name: str, result: bt.Result) -> None:
    s = result.summary()
    print(
        f"  {name:28} trades {s['trades']:4d}  win {s['win_rate']:6.1%}  "
        f"trade {s['mean_trade']:+7.2%}  total {s['total_return']:+8.1%}  "
        f"CAGR {s['cagr']:+7.1%}  maxDD {s['max_drawdown']:6.1%}  Sharpe {s['sharpe']:5.2f}"
    )


def halves(bars: bt.Bars) -> list[bt.Bars]:
    mid = len(bars.days) // 2
    out = []
    for lo, hi in ((0, mid), (mid, len(bars.days))):
        out.append(bt.Bars(
            days=bars.days[lo:hi],
            open={s: v[lo:hi] for s, v in bars.open.items()},
            close={s: v[lo:hi] for s, v in bars.close.items()},
        ))
    return out


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    cfg = RebalanceConfig()
    spot = json.loads((ROOT / "runtime/universe.json").read_text())["symbols"]
    bars = load_bars(spot)
    first = pd.to_datetime(bars.days[0], unit="ms").date()
    last = pd.to_datetime(bars.days[-1], unit="ms").date()
    print(f"\nspot daily archive: {len(spot)} symbols, {first} → {last}\n")

    print("H20 trend — BTC+ETH, long above the "
          f"{cfg.trend_sma_days}-day SMA, decided on Mondays")
    pair = cfg.trend_symbols
    show("trend", bt.trend(bars, pair, sma_days=cfg.trend_sma_days, cost=SPOT_COST))
    show("buy & hold BTC+ETH", bt.buy_and_hold(bars, pair, cost=SPOT_COST))
    for k, part in enumerate(halves(bars), 1):
        show(f"  half {k}: trend", bt.trend(part, pair, sma_days=cfg.trend_sma_days, cost=SPOT_COST))
        show(f"  half {k}: buy & hold", bt.buy_and_hold(part, pair, cost=SPOT_COST))

    print(f"\nH21 xsmom — {len(spot)} symbols, {cfg.xsmom_lookback_days}d return, "
          f"top {cfg.xsmom_top_k}, hold to rank {cfg.xsmom_hold_rank}, Mondays")
    args = dict(lookback_days=cfg.xsmom_lookback_days, top_k=cfg.xsmom_top_k,
                hold_rank=cfg.xsmom_hold_rank, cost=SPOT_COST)
    show("xsmom", bt.xsmom(bars, **args))
    show(f"buy & hold all {len(spot)}", bt.buy_and_hold(bars, spot, cost=SPOT_COST))
    for k, part in enumerate(halves(bars), 1):
        show(f"  half {k}: xsmom", bt.xsmom(part, **args))
        show(f"  half {k}: buy & hold", bt.buy_and_hold(part, spot, cost=SPOT_COST))

    funding = load_funding(spot)
    all_ts = [ts for f in funding.values() for ts, _ in f]
    lo, hi = min(all_ts), max(all_ts)
    days = list(range(lo - lo % DAY_MS + cfg.carry_window_days * DAY_MS, hi - hi % DAY_MS, DAY_MS))
    print(f"\nH19 carry — {len(funding)} symbols with perp funding, "
          f"{pd.to_datetime(days[0], unit='ms').date()} → {pd.to_datetime(days[-1], unit='ms').date()}; "
          f"enter at {cfg.carry_window_days}d sum ≥ {cfg.carry_enter_sum:.2%}, "
          f"exit ≤ {cfg.carry_exit_sum:.2%}, ≤{cfg.carry_max_pairs} pairs")
    cargs = dict(window_days=cfg.carry_window_days, enter_sum=cfg.carry_enter_sum,
                 exit_sum=cfg.carry_exit_sum, max_pairs=cfg.carry_max_pairs,
                 pair_cost=SPOT_COST + PERP_COST)
    result = bt.carry(funding, days, **cargs)
    show("carry", result)
    mid = len(days) // 2
    show("  half 1: carry", bt.carry(funding, days[:mid], **cargs))
    show("  half 2: carry", bt.carry(funding, days[mid:], **cargs))
    held_days = [(t["exit_day"] - t["entry_day"]) / DAY_MS for t in result.trades]
    if held_days:
        print(f"  median hold {sorted(held_days)[len(held_days) // 2]:.0f} days")
    rates = [r for f in funding.values() for _, r in f]
    positive = sum(1 for r in rates if r > 0) / len(rates)
    print(f"  settlements positive: {positive:.1%}; mean rate {sum(rates) / len(rates):+.5%}"
          f" ({sum(rates) / len(rates) * 3 * 365:+.1%}/yr at 8h)")
    print()
    return 0 if all(math.isfinite(v) for v in (result.total_return,)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
