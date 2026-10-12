#!/usr/bin/env python3
"""Every forward test against its own pre-registered kill rule — one page.

    python scripts/scorecard.py

Reads each engine's database and state, nothing else, and applies the rules
written in ``research/hypotheses.md`` *before* the engines ran: H17 (fast),
H18 (scalp, measurement only), H19 carry, H20 trend, H21 xsmom. The verdict
column only ever says what the rule says. "No verdict yet" is an answer, and
it is the one the rules give most of the time: the point of writing this down
is that nobody gets to read a promising week as a result.

Read-only: every database is opened ``mode=ro``, so it is safe while the
engines run.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from _bootstrap import ROOT

from tbot.core.config import load_config

DAY_MS = 86_400_000
WEEK_MS = 7 * DAY_MS
SEED = 29

# H17's 50-trade verdict pools the 47 trades before the 2026-10-12 reset with
# the first ones after it (hypotheses.md, "Havuzlama kararı").
H17_POOL = ROOT / "runtime-fast/backups/pre-reset-20261012-003132.db"
H17_VERDICT_AT = 50


@dataclass(slots=True)
class Line:
    name: str
    facts: list[str]
    verdict: str


def connect(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def state(runtime: Path) -> dict:
    path = runtime / "state.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def bootstrap_ci(values: list[float], simulations: int = 5000) -> tuple[float, float, float]:
    """Mean and 95% interval of the mean, resampling the observations."""
    data = np.asarray(values, dtype="float64")
    rng = np.random.default_rng(SEED)
    means = rng.choice(data, size=(simulations, len(data)), replace=True).mean(axis=1)
    return float(data.mean()), float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def ci_verdict(lo: float, hi: float, *, wholly_below: str, straddles: str, wholly_above: str) -> str:
    if hi < 0:
        return wholly_below
    if lo > 0:
        return wholly_above
    return straddles


def max_drawdown(values: list[float]) -> float:
    peak, worst = -math.inf, 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, 1.0 - value / peak)
    return worst


def daily_closes(conn: sqlite3.Connection) -> list[float]:
    """The engine's equity at the last sample of each UTC day.

    Sampled on the same grid as the benchmark's daily closes, so the two
    drawdowns are measured at the same resolution — a five-minute equity
    curve always shows a deeper trough than a daily one.
    """
    rows = conn.execute("SELECT ts, equity FROM equity ORDER BY ts").fetchall()
    by_day: dict[int, float] = {}
    for row in rows:
        by_day[int(row["ts"]) // DAY_MS] = float(row["equity"])
    return [by_day[d] for d in sorted(by_day)]


def started_ms(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT MIN(started_ts) AS t FROM runs").fetchone()
    return int(row["t"]) if row and row["t"] else None


def benchmark(symbols: list[str], since_ms: int) -> tuple[float, float, int] | None:
    """Equal-weight buy-and-hold of ``symbols`` from the first daily close at or
    after ``since_ms``: (total return, max drawdown on daily closes, days)."""
    curves = []
    for symbol in symbols:
        path = ROOT / "runtime/data/ohlcv" / symbol.replace("/", "_") / "1d.parquet"
        if not path.exists():
            return None
        frame = pd.read_parquet(path)
        close_ts = frame["ts"].astype("int64") + DAY_MS
        series = frame.loc[close_ts >= since_ms, "close"].astype(float).to_numpy()
        if len(series) == 0:
            return None
        curves.append(series / series[0])
    days = min(len(c) for c in curves)
    if days < 2:
        return 0.0, 0.0, days - 1
    value = np.mean([c[:days] for c in curves], axis=0)
    return float(value[-1] - 1.0), max_drawdown(list(value)), days - 1


def frozen(equity: float | None, floor: float) -> str:
    if equity is None:
        return "equity unknown"
    return f"FROZEN (≤ {floor:.0f})" if equity <= floor else f"above the {floor:.0f} floor"


# --------------------------------------------------------------------------
# the hypotheses
# --------------------------------------------------------------------------


def h17() -> Line:
    cfg = load_config("config/fast.yaml")
    live = connect(cfg.db_path)
    pool = connect(H17_POOL)
    before = [float(r["r_multiple"]) for r in pool.execute(
        "SELECT r_multiple FROM trades ORDER BY exit_ts")] if pool else []
    after = [float(r["r_multiple"]) for r in live.execute(
        "SELECT r_multiple FROM trades ORDER BY exit_ts")] if live else []
    pooled = (before + after)[:H17_VERDICT_AT]
    equity = state(cfg.runtime_dir).get("equity")
    facts = [
        f"trades {len(before)} pre-reset + {len(after)} since = {len(before) + len(after)}"
        f" (verdict on the first {H17_VERDICT_AT})",
        f"equity {equity:.2f}" if equity is not None else "equity ?",
    ]
    if len(pooled) < H17_VERDICT_AT:
        verdict = f"no verdict yet ({len(pooled)}/{H17_VERDICT_AT})"
    else:
        mean, lo, hi = bootstrap_ci(pooled)
        facts.append(f"mean {mean:+.3f}R, 95% CI [{lo:+.3f}, {hi:+.3f}]R")
        verdict = ci_verdict(lo, hi, wholly_below="KILL — CI wholly below zero",
                             straddles="extend to 100 trades",
                             wholly_above="move to Gate 1 (CPCV + DSR)")
    if equity is not None and equity <= 850:
        verdict += " · FREEZE (−15% of 1000)"
    return Line("H17 fast", facts, verdict)


def h18() -> Line:
    cfg = load_config("config/scalp.yaml")
    conn = connect(cfg.db_path)
    row = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(pnl),0) p, COALESCE(SUM(fees),0) f "
                       "FROM trades").fetchone() if conn else None
    equity = state(cfg.runtime_dir).get("equity")
    facts = [f"trades {row['n']}, Σpnl {row['p']:+.2f}, fees {row['f']:.2f}" if row else "no db",
             f"equity {equity:.2f}" if equity is not None else "equity ?"]
    kill = cfg.scalp.kill_equity
    verdict = "rejected (2026-10-11); running as measurement only · " + frozen(equity, kill)
    return Line("H18 scalp", facts, verdict)


def h19() -> Line:
    cfg = load_config("config/carry.yaml")
    conn = connect(cfg.db_path)
    st = state(cfg.runtime_dir)
    equity = st.get("equity")
    start = started_ms(conn) if conn else None
    days = (time.time() * 1000 - start) / DAY_MS if start else 0.0
    trades = [dict(r) for r in conn.execute("SELECT symbol, pnl, meta FROM trades")] if conn else []
    for t in trades:
        t["meta"] = json.loads(t["meta"] or "{}")
    shorts = [t for t in trades if t["symbol"].endswith(":USDT")]
    by_symbol = {t["symbol"]: t for t in trades}
    open_pairs = sum(1 for p in st.get("open_positions", []) if str(p.get("symbol", "")).endswith(":USDT"))
    broken = []
    for short in shorts:
        income = float(short["meta"].get("income", 0.0))
        spot = by_symbol.get(short["meta"].get("pair", ""))
        if spot is None:
            continue
        basis = short["pnl"] + spot["pnl"] - income  # price legs and fees, without funding
        if basis < 0 and -basis > income:
            broken.append(f"{spot['symbol']} basis {basis:+.2f} > funding {income:+.2f}")
    opened = len(shorts) + open_pairs
    facts = [f"day {days:.0f} of 90", f"pairs opened {opened} ({open_pairs} open)",
             f"equity {equity:.2f}" if equity is not None else "equity ?"]
    if broken:
        verdict = "MODEL WRONG — freeze: " + "; ".join(broken)
    elif opened == 0 and days >= 90:
        verdict = "KILL — no pair in 90 days: no carry in this universe"
    else:
        verdict = f"no verdict yet · {frozen(equity, cfg.rebalance.freeze_equity)}"
    return Line("H19 carry", facts, verdict)


def weekly(name: str, config: str, symbols: list[str] | None) -> Line:
    cfg = load_config(config)
    conn = connect(cfg.db_path)
    st = state(cfg.runtime_dir)
    equity = st.get("equity")
    capital = cfg.risk.max_capital or 1000.0
    start = started_ms(conn) if conn else None
    weeks = (time.time() * 1000 - start) / WEEK_MS if start else 0.0
    curve = daily_closes(conn) if conn else []
    facts = [f"week {weeks:.1f} of 26"]
    if equity is not None:
        facts.append(f"return {equity / capital - 1:+.2%}, maxDD {max_drawdown([capital, *curve]):.1%}")
    bench = benchmark(symbols or cfg.universe.symbols, start) if start else None
    if bench:
        facts.append(f"buy & hold {bench[0]:+.2%}, maxDD {bench[1]:.1%} ({bench[2]} closes)")
    verdict = (f"no verdict yet ({weeks:.0f}/26 weeks)" if weeks < 26
               else "26 weeks: compare direction with buy & hold (return and drawdown)")
    if name.startswith("H21") and conn:
        rets = [float(r["pnl"]) / (float(r["entry_price"]) * float(r["quantity"]))
                for r in conn.execute("SELECT pnl, entry_price, quantity FROM trades")]
        facts.append(f"closed trades {len(rets)}/50")
        if len(rets) >= 50:
            mean, lo, hi = bootstrap_ci(rets)
            facts.append(f"per trade {mean:+.2%}, 95% CI [{lo:+.2%}, {hi:+.2%}]")
            if hi < 0:
                verdict = "KILL — per-trade CI wholly below zero"
    verdict += f" · {frozen(equity, cfg.rebalance.freeze_equity)}"
    return Line(name, facts, verdict)


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    trend_cfg = load_config("config/trend.yaml")
    spot = json.loads((ROOT / "runtime/universe.json").read_text())["symbols"]
    lines = [
        h17(),
        h18(),
        h19(),
        weekly("H20 trend", "config/trend.yaml", list(trend_cfg.rebalance.trend_symbols)),
        weekly("H21 xsmom", "config/xsmom.yaml", spot),
    ]
    print(f"\nscorecard · {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}"
          " · rules: research/hypotheses.md\n")
    for line in lines:
        print(f"  {line.name}")
        for fact in line.facts:
            print(f"      {fact}")
        print(f"   →  {line.verdict}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
