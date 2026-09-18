#!/usr/bin/env python3
"""Parameter plateau test: is the best setting a plateau or a spike?

    python scripts/plateau.py --param exits.initial_stop_atr_mult --values 1.5,2.0,2.5,3.0
    python scripts/plateau.py --sweep

A parameter that works only at one exact value, with worse results either side,
has not found anything — it has fitted noise. A real effect degrades gracefully:
neighbouring values give similar results, because the underlying behaviour does
not care about the third decimal place.

So the test is not "which value is best" but "does the neighbourhood agree".
Reported as the ratio of the neighbourhood's mean to the peak: near 1.0 means a
plateau, near 0 means a spike standing alone.

**Every run here is a trial.** Pass the cumulative count to
``scripts/cpcv_report.py --trials N`` afterwards — the Deflated Sharpe Ratio
exists precisely to price in how many times the data was asked.
"""

from __future__ import annotations

import argparse
import json

import numpy as np
import pandas as pd
from _bootstrap import indicator_selection, setup, universe

from tbot.backtest import metrics as metrics_mod
from tbot.backtest.engine import Backtester
from tbot.data.store import ParquetStore

DEFAULT_SWEEPS = {
    "exits.initial_stop_atr_mult": [1.0, 1.5, 2.0, 2.5, 3.0],
    "exits.trailing_atr_mult": [1.5, 2.0, 2.5, 3.0, 3.5],
    "conviction.base_threshold": [54.0, 58.0, 62.0, 66.0, 70.0],
    "risk.risk_per_trade": [0.005, 0.0075, 0.01, 0.015],
}


def apply_override(cfg, path: str, value):
    section, field = path.split(".", 1)
    setattr(getattr(cfg, section), field, value)
    return cfg


def evaluate(cfg, store, symbols, blocks, selected, decay) -> dict:
    result = Backtester(cfg, blocks, selected, decay).run(store, symbols)
    stats = metrics_mod.compute(
        result.trades, result.equity_curve, cfg.starting_balance, cfg.timeframes.trigger
    )
    return {
        "trades": stats.trades,
        "expectancy_r": stats.expectancy_r,
        "profit_factor": stats.profit_factor if np.isfinite(stats.profit_factor) else 0.0,
        "total_return": stats.total_return,
        "max_drawdown": stats.max_drawdown,
        "sharpe": stats.sharpe,
    }


def plateau_ratio(scores: list[float]) -> tuple[float, str]:
    """How much of the peak the rest of the neighbourhood retains.

    1.0 means every tested value did as well as the best (a true plateau).
    Near 0 means one value stands alone — the signature of a fit to noise.

    When every value is negative the question does not apply: there is no peak
    to have a plateau around, only a choice of how much to lose. Saying "spike"
    there would be misleading, so it gets its own verdict.
    """
    finite = [s for s in scores if np.isfinite(s)]
    if len(finite) < 2:
        return 0.0, "not enough values"

    peak = max(finite)
    if peak <= 0:
        # Raising the bar only reduces exposure to a losing process. Best is
        # least-bad, which is not the same thing as good.
        return 0.0, "no positive value — the parameter is choosing how much to lose"

    others = [s for s in finite if s != peak]
    ratio = float(np.mean(others) / peak) if others else 0.0
    verdict = "plateau" if ratio > 0.6 else "fragile" if ratio > 0.3 else "spike — fitted noise"
    return ratio, verdict


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/base.yaml")
    parser.add_argument("--param", default="")
    parser.add_argument("--values", default="")
    parser.add_argument("--sweep", action="store_true")
    parser.add_argument("--metric", default="expectancy_r")
    args = parser.parse_args()

    base_cfg = setup(args.config)
    store = ParquetStore(base_cfg.data.parquet_dir)
    symbols = universe(base_cfg)
    blocks, selected, decay = indicator_selection(base_cfg)

    if args.sweep:
        sweeps = DEFAULT_SWEEPS
    elif args.param:
        values = [float(v) for v in args.values.split(",") if v]
        sweeps = {args.param: values or DEFAULT_SWEEPS.get(args.param, [])}
    else:
        parser.error("pass --param with --values, or --sweep")

    all_rows: list[dict] = []
    verdicts: dict[str, dict] = {}
    trials = 0

    for param, values in sweeps.items():
        print(f"\n{param}")
        rows = []
        for value in values:
            cfg = setup(args.config)
            apply_override(cfg, param, value)
            stats = evaluate(cfg, store, symbols, blocks, selected, decay)
            trials += 1
            rows.append({"param": param, "value": value, **stats})
            print(
                f"  {value:<8} trades={stats['trades']:<4} "
                f"expectancy={stats['expectancy_r']:+.4f}R  "
                f"PF={stats['profit_factor']:.2f}  return={stats['total_return']:+.2%}"
            )

        scores = [r[args.metric] for r in rows]
        ratio, verdict = plateau_ratio(scores)
        best = max(rows, key=lambda r: r[args.metric])
        verdicts[param] = {
            "best_value": best["value"],
            "best_metric": best[args.metric],
            "plateau_ratio": round(ratio, 3),
            "verdict": verdict,
        }
        print(
            f"  -> best {best['value']} · plateau ratio {ratio:.2f} · {verdicts[param]['verdict']}"
        )
        all_rows.extend(rows)

    frame = pd.DataFrame(all_rows)
    lines = [
        "# Parametre platosu testi",
        "",
        "Tek bir değerde çalışıp komşularında çöken parametre bir şey bulmamıştır —",
        "gürültüye oturmuştur. Gerçek bir etki nazikçe bozulur: komşu değerler benzer",
        "sonuç verir, çünkü altta yatan davranış üçüncü ondalığı umursamaz.",
        "",
        f"Bu koşuda **{trials} deneme** yapıldı. Bunları kümülatif olarak sayıp",
        "`scripts/cpcv_report.py --trials N`'e geçirin: Deflated Sharpe tam da veriye",
        "kaç kez soru sorulduğunu fiyatlamak için var.",
        "",
        "| parametre | en iyi | plato oranı | yorum |",
        "|---|---|---|---|",
    ]
    for param, verdict in verdicts.items():
        lines.append(
            f"| `{param}` | {verdict['best_value']} | {verdict['plateau_ratio']} | "
            f"{verdict['verdict']} |"
        )
    lines += ["", "## Tam sonuçlar", "", frame.round(4).to_markdown(index=False), ""]

    path = base_cfg.reports_dir / "plateau.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")

    (base_cfg.runtime_dir / "plateau.json").write_text(
        json.dumps({"trials": trials, "verdicts": verdicts}, indent=2, default=str)
    )

    print(f"\n{trials} trials run. Remember to pass them to cpcv_report.py --trials")
    print(f"-> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
