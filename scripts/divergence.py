#!/usr/bin/env python3
"""Gate 2: does live behaviour match what was backtested?

Runs a backtest over exactly the period the bot has been trading, then compares
it against what actually happened, trade by trade.

    python scripts/divergence.py --since 30d

The comparison matters more than either result on its own. A backtest and a
live run over the same bars should produce nearly the same trades — they share
the strategy code. Where they differ, one of three things is true, and all
three are worth knowing:

* a bug in the live path that the backtest does not exercise
* data the live engine saw differently (a gap, a late bar, a revision)
* execution reality the simulator does not model

Divergence is measured on what the strategy decided (which symbols, when, which
direction) rather than on returns. Two runs can post similar P&L while taking
completely different trades, and that is not agreement.
"""

from __future__ import annotations

import argparse
import json

import pandas as pd
from _bootstrap import indicator_selection, setup, universe

from tbot.backtest.engine import Backtester
from tbot.core.clock import iso, now_ms
from tbot.core.db import Database
from tbot.data.store import ParquetStore

# Gate 2's bar, fixed in advance.
MAX_DIVERGENCE = 0.10


def live_trades(db: Database, since: int) -> pd.DataFrame:
    rows = db.query(
        "SELECT symbol, setup, entry_ts, exit_ts, r_multiple, bars_held, exit_reason "
        "FROM trades WHERE entry_ts >= ? ORDER BY entry_ts",
        (since,),
    )
    return pd.DataFrame(rows)


def compare(live: pd.DataFrame, simulated: pd.DataFrame, tolerance_bars: int) -> dict:
    """Match trades between the two runs and quantify the disagreement.

    Matching is on (symbol, approximate entry time) rather than exact
    timestamps: the live engine enters at the bar it processes, which can sit a
    bar away from the backtest when a feed is late.
    """
    if live.empty and simulated.empty:
        return {"status": "no trades in either run", "divergence": 0.0}

    matched, live_only = [], []
    remaining = simulated.copy()

    for _, trade in live.iterrows():
        window = remaining[
            (remaining["symbol"] == trade["symbol"])
            & ((remaining["entry_ts"] - trade["entry_ts"]).abs() <= tolerance_bars)
        ]
        if window.empty:
            live_only.append(trade)
            continue
        closest = window.iloc[(window["entry_ts"] - trade["entry_ts"]).abs().argsort()].iloc[0]
        remaining = remaining.drop(closest.name)
        matched.append(
            {
                "symbol": trade["symbol"],
                "live_r": float(trade["r_multiple"]),
                "sim_r": float(closest["r_multiple"]),
                "r_gap": float(trade["r_multiple"] - closest["r_multiple"]),
                "live_bars": int(trade["bars_held"]),
                "sim_bars": int(closest["bars_held"]),
                "live_exit": trade["exit_reason"],
                "sim_exit": closest["exit_reason"],
            }
        )

    total = max(len(live), len(simulated))
    divergence = 1.0 - (len(matched) / total) if total else 0.0
    matched_frame = pd.DataFrame(matched)

    return {
        "live_trades": len(live),
        "simulated_trades": len(simulated),
        "matched": len(matched),
        "live_only": len(live_only),
        "simulated_only": len(remaining),
        "divergence": divergence,
        "passes_gate": divergence <= MAX_DIVERGENCE,
        "mean_r_gap": float(matched_frame["r_gap"].mean()) if not matched_frame.empty else 0.0,
        "max_r_gap": float(matched_frame["r_gap"].abs().max()) if not matched_frame.empty else 0.0,
        "exit_disagreements": int((matched_frame["live_exit"] != matched_frame["sim_exit"]).sum())
        if not matched_frame.empty
        else 0,
        "detail": matched_frame,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/base.yaml")
    parser.add_argument("--since", default="30d")
    args = parser.parse_args()

    cfg = setup(args.config)
    db = Database(cfg.db_path)
    days = int(args.since.rstrip("d"))
    since = now_ms() - days * 86_400_000

    live = live_trades(db, since)
    if live.empty:
        print(f"No live trades in the last {days} days — nothing to compare yet.")
        print("Gate 2 needs a live run with trades before it can be evaluated.")
        db.close()
        return 0

    print(f"Replaying the same period through the backtester ({len(live)} live trades)…")
    store = ParquetStore(cfg.data.parquet_dir)
    blocks, selected, decay = indicator_selection(cfg)
    result = Backtester(cfg, blocks, selected, decay).run(
        store, universe(cfg), start_ts=since, end_ts=now_ms()
    )

    simulated = pd.DataFrame(
        [
            {
                "symbol": t.symbol,
                "setup": t.setup,
                "entry_ts": t.entry_ts,
                "exit_ts": t.exit_ts,
                "r_multiple": t.r_multiple,
                "bars_held": t.bars_held,
                "exit_reason": t.exit_reason.value,
            }
            for t in result.trades
        ]
    )

    from tbot.core.clock import tf_ms

    tolerance = tf_ms(cfg.timeframes.trigger) * 2
    report = compare(live, simulated, tolerance)

    detail = report.pop("detail")
    lines = [
        f"# Kapı 2 — backtest ↔ canlı sapması (son {days} gün)",
        "",
        "Backtest ile canlı aynı barlar üzerinde neredeyse aynı işlemleri üretmeli:",
        "strateji kodu ortak. Fark varsa üç şeyden biri doğrudur —",
        "canlı yolda backtest'in uğramadığı bir bug, motorun farklı gördüğü bir veri,",
        "ya da simülatörün modellemediği bir icra gerçeği. Üçü de bilinmeye değer.",
        "",
        f"**Sapma: {report['divergence']:.1%}** (eşik {MAX_DIVERGENCE:.0%}) — "
        f"{'GEÇTİ' if report['passes_gate'] else 'GEÇEMEDİ'}",
        "",
        "| metrik | değer |",
        "|---|---|",
        f"| canlı işlem | {report['live_trades']} |",
        f"| simüle işlem | {report['simulated_trades']} |",
        f"| eşleşen | {report['matched']} |",
        f"| sadece canlıda | {report['live_only']} |",
        f"| sadece simülasyonda | {report['simulated_only']} |",
        f"| ortalama R farkı | {report['mean_r_gap']:+.3f} |",
        f"| en büyük R farkı | {report['max_r_gap']:.3f} |",
        f"| çıkış nedeni uyuşmazlığı | {report['exit_disagreements']} |",
        "",
    ]

    if not detail.empty:
        lines += ["## Eşleşen işlemler", "", detail.round(3).to_markdown(index=False), ""]

    if report["live_only"]:
        lines += [
            "## Sadece canlıda görülen işlemler",
            "",
            "Backtest'in girmediği yerde canlı girdiyse, canlı yol farklı bir şey",
            "görüyor demektir. Önce veriyi, sonra zaman dilimi hizalamasını kontrol et.",
            "",
        ]

    path = cfg.reports_dir / "divergence.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")

    print(
        f"\n  divergence {report['divergence']:.1%}  "
        f"({'PASS' if report['passes_gate'] else 'FAIL'}, threshold {MAX_DIVERGENCE:.0%})"
    )
    print(
        f"  live {report['live_trades']} · simulated {report['simulated_trades']} · "
        f"matched {report['matched']}"
    )
    print(
        f"  mean R gap {report['mean_r_gap']:+.3f} · "
        f"exit disagreements {report['exit_disagreements']}"
    )
    print(f"\n-> {path}")

    (cfg.runtime_dir / "gate2.json").write_text(
        json.dumps({**report, "generated": iso(now_ms())}, indent=2, default=str)
    )
    db.close()
    return 0 if report["passes_gate"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
