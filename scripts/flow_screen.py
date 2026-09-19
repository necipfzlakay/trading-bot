#!/usr/bin/env python3
"""Screen taker order-flow imbalance for predictive power, before building on it.

    python scripts/flow_screen.py --symbols BTC/USDT,ETH/USDT --months 12

Every indicator this bot uses is derived from price. That is the reason the
short-horizon measurement came back empty: the strongest signal available moves
a 2%/1% barrier hit rate from 31.9% to 34.5% when 43.3% is needed to cover fees.
A signal that is *not* a function of price is the only thing that could change
that answer, and taker imbalance is the cheapest one to test.

**Why Binance and not Bybit.** Bybit's ``/v5/market/recent-trade`` returns at
most 60 trades with no time filter, so there is no history to backfill from.
Binance publishes ``takerBuyBaseVolume`` as the tenth field of every kline, back
to 2017. Different venue, but the question is whether the mechanism exists at
all — and ``research/hypotheses.md`` already counts "more symbols/history from
another exchange" as a legitimate way to widen the sample.

**This screens; it does not trade.** Nothing here touches the engine, the
archive or the account. If the imbalance shows nothing, the short-term idea is
closed on evidence rather than opinion, and no live code was written for it.
"""

from __future__ import annotations

import argparse
import time

import httpx
import numpy as np
import pandas as pd

API = "https://api.binance.com/api/v3/klines"
MAX_PER_REQUEST = 1000

# Round-trip cost on Bybit spot: 0.1% each side plus measured slippage.
ROUND_TRIP_COST = 0.003


def fetch(symbol: str, interval: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    """Pull klines, paging forward until the window is covered."""
    rows: list[list] = []
    cursor = start_ms
    with httpx.Client(timeout=30) as client:
        while cursor < end_ms:
            response = client.get(
                API,
                params={
                    "symbol": symbol.replace("/", ""),
                    "interval": interval,
                    "startTime": cursor,
                    "limit": MAX_PER_REQUEST,
                },
            )
            if response.status_code != 200:
                break
            batch = response.json()
            if not batch:
                break
            rows.extend(batch)
            nxt = int(batch[-1][0]) + 1
            if nxt <= cursor:
                break
            cursor = nxt
            time.sleep(0.12)  # stay well inside the public rate limit

    if not rows:
        return pd.DataFrame()

    frame = pd.DataFrame(
        rows,
        columns=[
            "ts", "open", "high", "low", "close", "volume", "close_ts",
            "quote_volume", "trades", "taker_buy_base", "taker_buy_quote", "ignore",
        ],
    )
    numeric = ["open", "high", "low", "close", "volume", "taker_buy_base", "trades"]
    frame[numeric] = frame[numeric].astype(float)
    frame["ts"] = frame["ts"].astype("int64")
    return frame.drop_duplicates(subset="ts").reset_index(drop=True)


def imbalance(frame: pd.DataFrame) -> pd.Series:
    """Taker buy share, centred: +1 all aggressive buying, -1 all selling.

    Taker volume is the half of the book that *crossed the spread* — someone
    who wanted it now and paid for the privilege. That is the part of volume
    that carries intent, and it is not recoverable from OHLC.
    """
    total = frame["volume"].replace(0.0, np.nan)
    return ((2.0 * frame["taker_buy_base"] - frame["volume"]) / total).clip(-1, 1)


def barrier_outcomes(frame: pd.DataFrame, target: float, stop: float, horizon: int) -> np.ndarray:
    """1 hit the target first, 0 hit the stop first, NaN undecided in the window.

    A bar that touches both counts as the stop (invariant D40): the OHLC cannot
    say which came first, and the optimistic reading is how a backtest beats
    the account it was supposed to predict.
    """
    close = frame["close"].to_numpy()
    high = frame["high"].to_numpy()
    low = frame["low"].to_numpy()
    out = np.full(len(close), np.nan)
    for i in range(len(close) - horizon - 1):
        entry = close[i]
        up, down = entry * (1 + target), entry * (1 - stop)
        window_high = high[i + 1 : i + 1 + horizon]
        window_low = low[i + 1 : i + 1 + horizon]
        first_up = np.argmax(window_high >= up) if (window_high >= up).any() else 10**9
        first_down = np.argmax(window_low <= down) if (window_low <= down).any() else 10**9
        if first_up == first_down == 10**9:
            continue
        out[i] = 1.0 if first_up < first_down else 0.0
    return out


def forward_return(frame: pd.DataFrame, horizon: int) -> np.ndarray:
    close = frame["close"].to_numpy()
    out = np.full(len(close), np.nan)
    out[: len(close) - horizon] = close[horizon:] / close[: len(close) - horizon] - 1.0
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default="BTC/USDT,ETH/USDT,SOL/USDT,XRP/USDT,DOGE/USDT,AVAX/USDT,LINK/USDT,ADA/USDT")
    parser.add_argument("--interval", default="1h")
    parser.add_argument("--months", type=int, default=12)
    parser.add_argument("--horizon", type=int, default=24, help="bars forward")
    parser.add_argument("--target", type=float, default=0.02)
    parser.add_argument("--stop", type=float, default=0.01)
    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    end = int(time.time() * 1000)
    start = end - args.months * 30 * 86_400_000

    frames: dict[str, pd.DataFrame] = {}
    print(f"Fetching {len(symbols)} symbols, {args.months} months of {args.interval}…")
    for symbol in symbols:
        frame = fetch(symbol, args.interval, start, end)
        if frame.empty:
            print(f"  {symbol:12} no data")
            continue
        frames[symbol] = frame
        print(f"  {symbol:12} {len(frame):,} bars")

    if not frames:
        print("nothing fetched")
        return 1

    fair = args.stop / (args.target + args.stop)
    need = (args.stop + ROUND_TRIP_COST) / (args.target + args.stop)

    print(f"\nBarrier +{args.target:.0%} / -{args.stop:.0%}, {args.horizon} bars forward")
    print(f"  fair hit rate with no edge : {fair:.1%}")
    print(f"  needed to cover {ROUND_TRIP_COST:.1%} cost : {need:.1%}\n")

    rows = []
    pooled_signal: list[np.ndarray] = []
    pooled_outcome: list[np.ndarray] = []
    pooled_forward: list[np.ndarray] = []

    for symbol, frame in frames.items():
        signal = imbalance(frame).to_numpy()
        outcome = barrier_outcomes(frame, args.target, args.stop, args.horizon)
        forward = forward_return(frame, args.horizon)
        usable = ~np.isnan(signal) & ~np.isnan(outcome)
        if usable.sum() < 200:
            continue
        pooled_signal.append(signal[usable])
        pooled_outcome.append(outcome[usable])
        both = ~np.isnan(signal) & ~np.isnan(forward)
        pooled_forward.append(np.vstack([signal[both], forward[both]]))

        cut = np.quantile(signal[usable], 0.9)
        top = outcome[usable][signal[usable] >= cut]
        rows.append((symbol, int(usable.sum()), outcome[usable].mean(), len(top), top.mean()))

    print(f"{'symbol':12} {'n':>8} {'base hit':>10} {'top decile n':>13} {'top hit':>9} {'lift':>8}")
    for symbol, n, base, n_top, top in rows:
        print(f"{symbol:12} {n:>8,} {base:>9.1%} {n_top:>13,} {top:>8.1%} {top - base:>+7.1%}")

    signal = np.concatenate(pooled_signal)
    outcome = np.concatenate(pooled_outcome)
    print(f"\nPOOLED  n={len(outcome):,}")
    print(f"  base hit rate            {outcome.mean():.1%}")
    for q in (0.8, 0.9, 0.95, 0.99):
        cut = np.quantile(signal, q)
        sel = outcome[signal >= cut]
        if len(sel) < 50:
            continue
        expectancy = sel.mean() * (args.target - ROUND_TRIP_COST) + (1 - sel.mean()) * (
            -args.stop - ROUND_TRIP_COST
        )
        verdict = "PROFITABLE" if sel.mean() > need else ""
        print(
            f"  above q{q:.2f} ({cut:+.3f})   {sel.mean():.1%}  n={len(sel):,}  "
            f"expectancy {expectancy:+.3%}  {verdict}"
        )

    flow = np.hstack(pooled_forward)
    corr = float(np.corrcoef(flow[0], flow[1])[0, 1])
    print(f"\n  rank-free correlation with {args.horizon}-bar forward return: {corr:+.4f}")
    print("  (for scale: this repo's price indicators sit near -0.02)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
