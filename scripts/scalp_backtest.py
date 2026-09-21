#!/usr/bin/env python3
"""H18 over stored one-minute bars — run once, before the engine, not tuned.

    python scripts/scalp_backtest.py
    python scripts/scalp_backtest.py --to 2026-09-21T10:00:00

The pre-registration (research/hypotheses.md, H18) fixes every parameter and
the expected result before this runs. If the result disagrees with it, the
fill model is questioned before any parameter is — and a result that looks
good is questioned hardest (audits #47, #49).

``--to`` pins the archive so a rerun measures the same bars (invariant 45).
"""

from __future__ import annotations

import argparse
from pathlib import Path

from _bootstrap import setup, universe

from tbot.core.clock import iso, now_ms, to_ms
from tbot.data.store import ParquetStore
from tbot.scalp.backtest import run, summarise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/scalp.yaml")
    parser.add_argument("--days", type=float, default=30.0)
    parser.add_argument("--to", default="", help="ISO end time; default now")
    parser.add_argument("--write", default="", help="report path; default runtime-scalp/reports")
    args = parser.parse_args()

    cfg = setup(args.config)
    end = to_ms(args.to) if args.to else now_ms()
    start = end - int(args.days * 86_400_000)
    store = ParquetStore(cfg.data.parquet_dir)

    frames = {}
    for symbol in universe(cfg):
        frame = store.read_ohlcv(symbol, "1m", start_ts=start, end_ts=end)
        if len(frame) > 1_000:
            frames[symbol] = frame.reset_index(drop=True)
    if not frames:
        print("no 1m data — run: scripts/backfill.py --config config/scalp.yaml --tf 1m")
        return 1

    result = run(cfg, frames)
    arms = sorted({t.arm for t in result.trades} | set(result.triggers))
    stats = {arm: summarise([t for t in result.trades if t.arm == arm]) for arm in arms}
    stats["all"] = summarise(result.trades)
    hours = result.minutes / 60

    lines = [
        "# H18 — 1 dakikalık maker scalper, backtest",
        "",
        f"**Aralık:** {iso(start)} → {iso(end)} · **Sembol:** {len(frames)} · "
        f"**Parametreler:** ön-kayıtlı, ayarsız (research/hypotheses.md)",
        "",
        "Dolum modeli (bkz. `src/tbot/scalp/backtest.py`): giriş sinyal barının "
        "kapanışında limit, **yalnız sonraki bar fiyatı geçerse** dolar; stop hedeften "
        "önce; dolum barında hedef yok; hedef maker, stop/zaman stop'u taker + kayma.",
        "",
        "| kol | tetik | dolmadı | işlem | isabet | işlem başına net | %95 CI | PF | PnL ($1000) | TP / SL / süre |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for arm in [*arms, "all"]:
        s = stats[arm]
        triggers = sum(result.triggers.values()) if arm == "all" else result.triggers.get(arm, 0)
        unfilled = sum(result.unfilled.values()) if arm == "all" else result.unfilled.get(arm, 0)
        if not s.get("trades"):
            lines.append(f"| {arm} | {triggers} | {unfilled} | 0 | — | — | — | — | — | — |")
            continue
        lines.append(
            f"| {arm} | {triggers} | {unfilled} | {s['trades']} | {s['win_rate']:.1%} | "
            f"{s['expectancy_pct']:+.3f}% | [{s['ci_low_pct']:+.3f}%, {s['ci_high_pct']:+.3f}%] | "
            f"{s['profit_factor']:.2f} | {s['pnl']:+.2f} | "
            f"{s['take_profit_share']:.0%} / {s['stop_share']:.0%} / {s['time_stop_share']:.0%} |"
        )
    total = stats["all"]
    if total.get("trades"):
        lines += [
            "",
            f"İşlem sıklığı: **{total['trades'] / hours:.1f}/saat** "
            f"({total['trades'] / (hours / 24):.0f}/gün). Toplam komisyon ${total['fees']:.2f}.",
        ]
    if result.skipped:
        lines += ["", f"Portföy kuralı yüzünden atlanan: {result.skipped}"]
    report = "\n".join(lines) + "\n"

    print("\n" + report)
    path = Path(args.write) if args.write else cfg.reports_dir / "scalp_backtest.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report, encoding="utf-8")
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
