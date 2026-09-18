#!/usr/bin/env python3
"""Run a backtest and write a full performance report.

    python scripts/backtest.py
    python scripts/backtest.py --from 2024-01-01 --to 2026-01-01 --symbols BTC/USDT,ETH/USDT

The report always includes the buy-and-hold comparison. Beating zero during a
period when the market rose is not evidence of anything.
"""

from __future__ import annotations

import argparse
import json

from _bootstrap import indicator_selection, setup, universe

from tbot.backtest import excursion, metrics, montecarlo
from tbot.backtest.engine import Backtester
from tbot.core.clock import to_ms
from tbot.data.store import ParquetStore


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/base.yaml")
    parser.add_argument("--symbols", default="")
    parser.add_argument("--from", dest="start", default="")
    parser.add_argument("--to", dest="end", default="")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    cfg = setup(args.config)
    store = ParquetStore(cfg.data.parquet_dir)
    symbols = (
        [s.strip() for s in args.symbols.split(",") if s.strip()] if args.symbols else universe(cfg)
    )
    blocks, selected, decay = indicator_selection(cfg)

    bt = Backtester(cfg, blocks, selected, decay)
    result = bt.run(
        store,
        symbols,
        to_ms(args.start) if args.start else None,
        to_ms(args.end) if args.end else None,
    )

    stats = metrics.compute(
        result.trades, result.equity_curve, cfg.starting_balance, cfg.timeframes.trigger
    )
    benchmark = metrics.buy_and_hold(
        store.read_ohlcv(symbols[0], cfg.timeframes.trigger)["close"], cfg.starting_balance
    )
    mc = montecarlo.shuffle_trades(result.trades, cfg.starting_balance)
    shock = montecarlo.cost_shock(result.trades, cfg.starting_balance, extra_bps=10)
    boot = montecarlo.bootstrap_expectancy(result.trades)
    exc = excursion.summarise(result.trades, cfg.exits.initial_stop_atr_mult)

    path = cfg.reports_dir / (args.out or "backtest.md")
    _write(path, cfg, symbols, result, stats, benchmark, mc, shock, boot, exc)

    print(
        f"\ntrades {stats.trades}  win {stats.win_rate:.1%}  PF {stats.profit_factor:.2f}  "
        f"expectancy {stats.expectancy_r:+.3f}R"
    )
    print(
        f"return {stats.total_return:+.2%}  maxDD {stats.max_drawdown:.1%}  "
        f"Sharpe {stats.sharpe:.2f}"
    )
    print(
        f"buy & hold ({symbols[0]}): {benchmark['total_return']:+.2%}  "
        f"maxDD {benchmark['max_drawdown']:.1%}"
    )
    print(
        f"bootstrap expectancy CI: [{boot.get('ci_low', 0):+.3f}, {boot.get('ci_high', 0):+.3f}]R"
    )
    print(f"\n-> {path}")

    (cfg.runtime_dir / "last_backtest.json").write_text(
        json.dumps(
            {
                "metrics": stats.to_dict(),
                "benchmark": benchmark,
                "monte_carlo": mc.summary(),
                "bootstrap": boot,
                "decay_bars": exc["decay_bars"],
            },
            indent=2,
            default=str,
        )
    )
    return 0


def _write(path, cfg, symbols, result, stats, benchmark, mc, shock, boot, exc) -> None:
    beat = stats.total_return > benchmark["total_return"]
    lines = [
        "# Backtest raporu",
        "",
        f"- Sembol: **{len(symbols)}** · bar: **{result.bars_processed:,}**",
        f"- Zaman dilimleri: {cfg.timeframes.all}",
        f"- Maliyet varsayımı: taker {cfg.execution.assumed_taker_fee:.3%}, "
        f"kayma {cfg.execution.slippage_bps:.0f}bps",
        f"- Conviction eşiği: {cfg.conviction.base_threshold}",
        "",
        "## Sonuç",
        "",
        "| metrik | değer |",
        "|---|---|",
        f"| işlem | {stats.trades} |",
        f"| kazanma oranı | {stats.win_rate:.1%} |",
        f"| profit factor | {stats.profit_factor:.2f} |",
        f"| expectancy | {stats.expectancy_r:+.3f}R |",
        f"| toplam getiri | {stats.total_return:+.2%} |",
        f"| CAGR | {stats.cagr:+.2%} |",
        f"| Sharpe | {stats.sharpe:.2f} |",
        f"| Sortino | {stats.sortino:.2f} |",
        f"| maks düşüş | {stats.max_drawdown:.1%} |",
        f"| Calmar | {stats.calmar:.2f} |",
        f"| ort. tutuş | {stats.avg_bars_held:.0f} bar |",
        f"| komisyon yükü | {stats.fee_drag:.1%} (brüt kârın) |",
        f"| maruziyet | {stats.exposure:.1%} |",
        "",
        "## Karşılaştırma ölçütü",
        "",
        f"`{symbols[0]}` al-tut: **{benchmark['total_return']:+.2%}** "
        f"(maks düşüş {benchmark['max_drawdown']:.1%})",
        "",
        f"Strateji al-tut'u **{'GEÇTİ' if beat else 'GEÇEMEDİ'}**. "
        + (
            ""
            if beat
            else "Sıfırı geçmek yeterli değil: her şeyin yükseldiği bir dönemde "
            "al-tut'un altında kalan bir strateji değer üretmemiştir."
        ),
        "",
        "## İstatistiksel dayanıklılık",
        "",
        f"- Bootstrap expectancy: **{boot.get('expectancy_r', 0):+.3f}R**, "
        f"%95 GA [{boot.get('ci_low', 0):+.3f}, {boot.get('ci_high', 0):+.3f}]R "
        f"(n={boot.get('samples', 0)})",
        f"- Monte Carlo (işlem sırası karıştırma): medyan getiri "
        f"{mc.median_return:+.2%}, maks düşüş medyan {mc.median_max_drawdown:.1%}, "
        f"p95 {mc.p95_max_drawdown:.1%}, iflas olasılığı {mc.ruin_probability:.1%}",
        f"- Maliyet şoku (+10bps): {shock['original_pnl']:+.2f} → "
        f"{shock['shocked_pnl']:+.2f} — edge {'dayandı' if shock['survives'] else 'yok oldu'}",
        "",
        "## MAE/MFE — çıkış kalibrasyonu",
        "",
    ]

    per_setup = exc.get("per_setup") or []
    if per_setup:
        import pandas as pd

        lines += [pd.DataFrame(per_setup).to_markdown(index=False), ""]
        lines += [
            "`suggested_stop_r` = kazanan işlemlerin %90'ını kesmeyen en sıkı stop, R cinsinden.",
            "`capture` = gerçekleşen getiri / görülen en iyi noktanın oranı. Düşükse çıkışlar erken.",
            "",
            f"Önerilen ATR çarpanları: `{exc.get('suggested_atr_multiples')}`",
            f"Edge sönme barları: `{exc.get('decay_bars')}`",
            "",
        ]
    else:
        lines += ["_Excursion analizi için en az 10 işlem gerekiyor._", ""]

    if result.trades:
        lines += [
            "## İşlemler",
            "",
            metrics.trade_table(result.trades).to_markdown(index=False),
            "",
        ]

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
