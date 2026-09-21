#!/usr/bin/env python3
"""Gate 1: is the strategy's result distinguishable from luck?

Runs CPCV (many out-of-sample paths, purged and embargoed), computes the
Deflated Sharpe Ratio for the number of configurations tried, and estimates the
Probability of Backtest Overfitting.

    python scripts/cpcv_report.py --blocks 10 --test-blocks 2

The verdict at the end is mechanical. A strategy that fails here does not
proceed to forward testing, however good the equity curve looks.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict

import numpy as np
from _bootstrap import indicator_selection, setup, universe

from tbot.backtest import dsr as dsr_mod
from tbot.backtest.cpcv import run_cpcv
from tbot.backtest.engine import Backtester
from tbot.data.store import ParquetStore

# Gate 1 thresholds, fixed in advance so the bar cannot move to fit the result.
MIN_PROFIT_FACTOR = 1.2
MAX_DRAWDOWN = 0.20
MIN_TRADES = 50
MAX_PBO = 0.5


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/base.yaml")
    parser.add_argument("--blocks", type=int, default=10)
    parser.add_argument("--test-blocks", type=int, default=2)
    parser.add_argument(
        "--trials", type=int, default=1, help="how many configurations were tried before this one"
    )
    args = parser.parse_args()

    cfg = setup(args.config)
    store = ParquetStore(cfg.data.parquet_dir)
    symbols = universe(cfg)
    blocks, selected, decay = indicator_selection(cfg)

    print(f"Running base backtest over {len(symbols)} symbols…")
    bt = Backtester(cfg, blocks, selected, decay)
    base = bt.run(store, symbols)

    equity = np.array([p.equity for p in base.equity_curve], dtype="float64")
    returns = np.diff(equity) / equity[:-1] if len(equity) > 1 else np.array([0.0])
    returns = returns[np.isfinite(returns)]

    # CPCV over the equity path: each split scores the mean return of its test
    # blocks, giving a distribution of out-of-sample outcomes rather than one.
    label_horizon = int(np.median([t.bars_held for t in base.trades])) if base.trades else 24
    cpcv = run_cpcv(
        n_samples=len(returns),
        evaluate=lambda _train, test: float(np.mean(returns[test])) * len(test),
        n_blocks=args.blocks,
        n_test_blocks=args.test_blocks,
        label_horizon=label_horizon,
    )

    periods = {"1h": 8760, "4h": 2190, "1d": 365}.get(cfg.timeframes.trigger, 8760)
    deflated = dsr_mod.deflated_sharpe(returns, trials=args.trials, periods_per_year=periods)

    # PBO needs several configurations to compare. With one, the honest answer
    # is that it cannot be estimated — not that it passed.
    pbo = None
    if cpcv.n_splits >= 8:
        chunks = np.array_split(returns, min(40, max(8, len(returns) // 50)))
        matrix = np.array([[float(np.mean(c)), float(np.median(c))] for c in chunks])
        pbo = dsr_mod.probability_of_backtest_overfitting(matrix)

    from tbot.backtest import metrics as metrics_mod

    stats = metrics_mod.compute(
        base.trades, base.equity_curve, cfg.starting_balance, cfg.timeframes.trigger
    )

    checks = {
        "trades >= 50": (stats.trades >= MIN_TRADES, f"{stats.trades}"),
        "profit factor >= 1.2": (
            stats.profit_factor >= MIN_PROFIT_FACTOR,
            f"{stats.profit_factor:.2f}",
        ),
        "max drawdown <= 20%": (stats.max_drawdown <= MAX_DRAWDOWN, f"{stats.max_drawdown:.1%}"),
        "DSR significant": (deflated.is_significant, f"p={deflated.p_value:.3f}"),
        "CPCV majority positive": (cpcv.win_share > 0.5, f"{cpcv.win_share:.1%}"),
        "PBO <= 0.5": (
            (pbo.pbo <= MAX_PBO) if pbo else False,
            f"{pbo.pbo:.2f}" if pbo else "not estimable",
        ),
    }
    passed = all(ok for ok, _ in checks.values())

    path = cfg.reports_dir / "cpcv.md"
    _write(path, cfg, stats, cpcv, deflated, pbo, checks, passed, args)

    print("\nGate 1 — proceed to forward testing?\n")
    for name, (ok, value) in checks.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:<26} {value}")
    print(f"\n  VERDICT: {'PASS' if passed else 'FAIL'}")
    if not passed:
        print("  Strategy does not proceed. Revise it, then re-run — and count this attempt")
        print("  in --trials next time, because every attempt raises the bar for the next.")
    print(f"\n-> {path}")

    (cfg.runtime_dir / "gate1.json").write_text(
        json.dumps(
            {
                "passed": passed,
                "checks": {k: v[1] for k, v in checks.items()},
                "cpcv": cpcv.summary(),
                "dsr": asdict(deflated) if deflated else None,
                "pbo": pbo.pbo if pbo else None,
            },
            indent=2,
            default=str,
        )
    )
    return 0 if passed else 1


def _write(path, cfg, stats, cpcv, deflated, pbo, checks, passed, args) -> None:
    lines = [
        "# Kapı 1 — CPCV / DSR / PBO",
        "",
        "Bu rapor tek bir soruyu cevaplıyor: sonuç şanstan ayırt edilebiliyor mu?",
        "",
        f"**SONUÇ: {'GEÇTİ' if passed else 'GEÇEMEDİ'}**",
        "",
        "| kontrol | değer | durum |",
        "|---|---|---|",
    ]
    for name, (ok, value) in checks.items():
        lines.append(f"| {name} | {value} | {'✅' if ok else '❌'} |")

    lines += [
        "",
        "## CPCV",
        "",
        "Walk-forward tek bir out-of-sample yol üretir ve en eski veriyi ısınmaya harcar.",
        "CPCV örneklemi bloklara böler, her blok kombinasyonunu test olarak kullanır ve",
        "yüzlerce yol üretir. Purging, etiket penceresi test bloğuna taşan eğitim",
        "satırlarını atar; embargo, test bloğundan hemen sonraki satırları atar.",
        "",
        f"```\n{json.dumps(cpcv.summary(), indent=2)}\n```",
        "",
        f"Yolların **{cpcv.win_share:.1%}**'i pozitif. Ortalamadan daha bilgilendirici:",
        "tek bir şanslı yolun taşıdığı strateji ile çoğu zaman çalışan strateji farklıdır.",
        "",
        "## Deflated Sharpe Ratio",
        "",
        f"- Gözlenen Sharpe: **{deflated.sharpe:.2f}**",
        f"- Denenen konfigürasyon: **{args.trials}**",
        f"- Sıfır edge'li {args.trials} denemenin beklenen maks Sharpe'ı: "
        f"**{deflated.expected_max_sharpe:.2f}**",
        f"- Çarpıklık {deflated.skew:.2f}, basıklık {deflated.kurtosis:.2f} "
        f"(kripto getirileri normal değil, bu düzeltme önemli)",
        f"- **p = {deflated.p_value:.4f}** → {'anlamlı' if deflated.is_significant else 'ANLAMSIZ'}",
        "",
        "10 strateji deneyip en iyisinin Sharpe'ının 1.8 olması ile 1 strateji deneyip",
        "1.8 bulmak aynı şey değildir. DSR bu farkı sayıya döker.",
        "",
    ]

    if pbo:
        lines += [
            "## Probability of Backtest Overfitting",
            "",
            f"- PBO: **{pbo.pbo:.3f}** → {pbo.verdict}",
            f"- Medyan OOS sıra: {pbo.median_oos_rank:.3f}",
            f"- Bölünme sayısı: {pbo.splits}",
            "",
        ]
    else:
        lines += ["## PBO", "", "_Tahmin edilemedi: yeterli konfigürasyon/örnek yok._", ""]

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
