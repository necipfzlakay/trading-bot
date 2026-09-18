#!/usr/bin/env python3
"""Calibrate stops and trails from what trades actually did.

Reads closed trades — from the live database, or from a fresh backtest — and
reports the MAE/MFE distributions that say where the stop should be and how much
of each move is being left behind.

    python scripts/exit_optimizer.py --source db --since 90d
    python scripts/exit_optimizer.py --source backtest

Nothing is applied automatically. The suggestions go into the report and into
``runtime/exit_calibration.json``; changing config is a decision, not a side
effect of running a script.
"""

from __future__ import annotations

import argparse
import json

import pandas as pd
from _bootstrap import indicator_selection, setup, universe

from tbot.backtest import excursion
from tbot.backtest.engine import Backtester
from tbot.core.clock import now_ms
from tbot.core.db import Database
from tbot.core.models import ExitReason, Regime, Side, Trade
from tbot.data.store import ParquetStore


def load_from_db(cfg, since_ms: int) -> list[Trade]:
    rows = Database(cfg.db_path).query(
        "SELECT * FROM trades WHERE exit_ts >= ? ORDER BY exit_ts", (since_ms,)
    )
    return [
        Trade(
            symbol=r["symbol"],
            side=Side(r["side"]),
            setup=r["setup"],
            entry_ts=r["entry_ts"],
            exit_ts=r["exit_ts"],
            entry_price=r["entry_price"],
            exit_price=r["exit_price"],
            quantity=r["quantity"],
            pnl=r["pnl"],
            fees=r["fees"],
            r_multiple=r["r_multiple"],
            mfe_r=r["mfe_r"],
            mae_r=r["mae_r"],
            bars_held=r["bars_held"],
            exit_reason=ExitReason(r["exit_reason"]),
            regime_at_entry=Regime(r["regime_at_entry"]),
            conviction_at_entry=r["conviction_at_entry"],
            leverage=r["leverage"],
        )
        for r in rows
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/base.yaml")
    parser.add_argument("--source", choices=["db", "backtest"], default="backtest")
    parser.add_argument("--since", default="365d")
    args = parser.parse_args()

    cfg = setup(args.config)

    if args.source == "db":
        days = int(args.since.rstrip("d"))
        trades = load_from_db(cfg, now_ms() - days * 86_400_000)
    else:
        store = ParquetStore(cfg.data.parquet_dir)
        blocks, selected, decay = indicator_selection(cfg)
        trades = Backtester(cfg, blocks, selected, decay).run(store, universe(cfg)).trades

    if len(trades) < 10:
        print(f"Only {len(trades)} trades — excursion analysis needs at least 10.")
        print("Suggestions from a handful of trades would be noise wearing a decimal point.")
        return 1

    result = excursion.summarise(trades, cfg.exits.initial_stop_atr_mult)
    per_setup = pd.DataFrame(result["per_setup"])
    decay_table = pd.DataFrame(result["edge_decay"])

    path = cfg.reports_dir / "exits.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "# Çıkış kalibrasyonu (MAE / MFE)",
                "",
                f"Kaynak: **{args.source}** · işlem: **{len(trades)}**",
                "",
                "Sabit tutuş süresi koymuyoruz. Bunun yerine stop ve trailing seviyeleri",
                "geçmiş işlemlerin gerçekte ne yaptığından çıkarılıyor.",
                "",
                "- **MAE**: işlem kapanana kadar gördüğü en kötü nokta. *Kazananların* MAE",
                "  dağılımı, kazananları kesmeyen en sıkı stopu verir. Kazananlar -0.4R'yi",
                "  geçmiyorsa 1R'lik stop 2.5 kat fazla geniştir — her zarar boşuna iki katı.",
                "- **MFE**: görülen en iyi nokta. Ortalama MFE ile gerçekleşen getiri arasındaki",
                "  fark, masada bırakılan paradır.",
                "",
                "## Kurulum bazlı",
                "",
                per_setup.to_markdown(index=False) if not per_setup.empty else "_veri yok_",
                "",
                "## Edge sönmesi (sabit süre limitinin yerine geçen ölçüm)",
                "",
                "Tutuş süresine göre koşullu getiri. Ortalama R'nin tepe yaptığı bar sayısı,",
                "o kurulumun edge'inin sönmeye başladığı yerdir — takvimden değil, veriden.",
                "",
                decay_table.to_markdown(index=False) if not decay_table.empty else "_veri yok_",
                "",
                "## Öneriler",
                "",
                f"- ATR çarpanları: `{result['suggested_atr_multiples']}`",
                f"- Edge sönme barları: `{result['decay_bars']}`",
                "",
                f"Mevcut ayar: `initial_stop_atr_mult={cfg.exits.initial_stop_atr_mult}`, "
                f"`trailing_atr_mult={cfg.exits.trailing_atr_mult}`",
                "",
                "Bunlar otomatik uygulanmaz. Config değişikliği bir karardır.",
                "",
            ]
        ),
        encoding="utf-8",
    )

    (cfg.runtime_dir / "exit_calibration.json").write_text(
        json.dumps(result, indent=2, default=str), encoding="utf-8"
    )

    print(f"\n{len(trades)} trades analysed")
    if not per_setup.empty:
        print(per_setup.to_string(index=False))
    print(f"\nsuggested ATR multiples: {result['suggested_atr_multiples']}")
    print(f"edge decay bars:          {result['decay_bars']}")
    print(f"\n-> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
