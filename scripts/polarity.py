#!/usr/bin/env python3
"""Which direction does a derivative signal actually point?

    python scripts/polarity.py

Contrarian readings of funding and crowding are received wisdom: high funding
means crowded longs, crowded longs get squeezed. Received wisdom is a hypothesis,
and this measures it instead of inheriting it.

For each signal, the top and bottom deciles are compared against the base rate
across horizons, with a block bootstrap interval. The output says whether the
data supports a contrarian or a momentum reading — and whether the registry's
current ``contrarian`` flag agrees.

Re-runnable by design. Polarity is not a constant: a sustained bear phase could
flip funding back to contrarian, and the only way to know is to measure again.
"""

from __future__ import annotations

import argparse
import json

import pandas as pd
from _bootstrap import setup, universe

from tbot.data.store import ParquetStore
from tbot.indicators import ta
from tbot.indicators.ic import forward_returns, tail_analysis
from tbot.indicators.registry import REGISTRY

# (registry name, archive column, how to normalise it)
SIGNALS = {
    "funding": ("funding_rate", lambda s: ta.normalize_zscore(s, 90)),
    "long_short": ("long_short_ratio", lambda s: ta.normalize_zscore(s, 90)),
    "oi_change": ("open_interest", lambda s: ta.normalize_zscore(ta.oi_change(s, 24), 90)),
}


def measure(store, symbols, timeframe, horizons, column, normalise, cost) -> list[dict]:
    rows = []
    for horizon in horizons:
        frames = []
        for symbol in symbols:
            df = store.read_with_derivatives(symbol, timeframe)
            if len(df) < 400 or column not in df.columns:
                continue
            if df[column].notna().sum() < 200:
                continue
            frames.append(
                pd.DataFrame(
                    {"sig": normalise(df[column]), "fwd": forward_returns(df["close"], horizon)}
                ).dropna()
            )
        if not frames:
            continue

        data = pd.concat(frames, ignore_index=True)
        result = tail_analysis(data["sig"], data["fwd"], horizon, round_trip_cost=cost)
        if result is None:
            continue

        top, bottom, base, hit, effective, ci_low, ci_high = result
        rows.append(
            {
                "timeframe": timeframe,
                "horizon": horizon,
                "high_decile": top,
                "low_decile": bottom,
                "base": base,
                "spread": top - bottom,
                "reading": "momentum" if top > bottom else "contrarian",
                "hit_rate": hit,
                "effective_n": effective,
                "ci_low": ci_low,
                "ci_high": ci_high,
            }
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/base.yaml")
    args = parser.parse_args()

    cfg = setup(args.config)
    store = ParquetStore(cfg.data.parquet_dir)
    symbols = universe(cfg)
    cost = cfg.execution.assumed_taker_fee * 2 + cfg.execution.slippage_bps / 10_000 * 2

    all_rows: list[dict] = []
    verdicts: dict[str, dict] = {}

    for name, (column, normalise) in SIGNALS.items():
        spec = REGISTRY.get(name)
        if spec is None:
            continue

        rows = []
        for timeframe, horizons in (("1d", [5, 10, 20]), ("4h", [24, 42])):
            rows.extend(measure(store, symbols, timeframe, horizons, column, normalise, cost))
        if not rows:
            continue

        for row in rows:
            row["signal"] = name
        all_rows.extend(rows)

        momentum = sum(1 for r in rows if r["reading"] == "momentum")
        measured = "momentum" if momentum > len(rows) / 2 else "contrarian"
        # contrarian=True means the registry flips the sign, i.e. it assumes a
        # contrarian reading.
        assumed = "contrarian" if spec.contrarian else "momentum"

        verdicts[name] = {
            "measured": measured,
            "assumed": assumed,
            "agrees": measured == assumed,
            "consistency": f"{max(momentum, len(rows) - momentum)}/{len(rows)} horizons agree",
        }

        flag = "OK " if measured == assumed else "MISMATCH"
        print(
            f"  [{flag}] {name:<12} measured={measured:<11} registry assumes={assumed:<11} "
            f"({verdicts[name]['consistency']})"
        )

    frame = pd.DataFrame(all_rows)
    lines = [
        "# Türev sinyal polaritesi",
        "",
        "Funding ve kalabalıklık için contrarian okuma yaygın kabul: yüksek funding =",
        "kalabalık long = squeeze. Yaygın kabul bir hipotezdir; bu rapor onu miras",
        "almak yerine ölçer.",
        "",
        "`reading` sütunu veriden çıkan yön. `agrees` sütunu registry'deki",
        "`contrarian` bayrağının bu ölçümle uyuşup uyuşmadığı.",
        "",
        "Polarite sabit değil: uzun bir ayı fazı funding'i contrarian'a geri",
        "çevirebilir. Bu yüzden script tek seferlik değil, tekrar çalıştırılabilir.",
        "",
        "| sinyal | ölçülen | varsayılan | uyuşuyor mu | tutarlılık |",
        "|---|---|---|---|---|",
    ]
    for name, verdict in verdicts.items():
        lines.append(
            f"| `{name}` | {verdict['measured']} | {verdict['assumed']} | "
            f"{'✅' if verdict['agrees'] else '❌'} | {verdict['consistency']} |"
        )
    lines += ["", "## Ölçümler", "", frame.round(4).to_markdown(index=False), ""]

    path = cfg.reports_dir / "polarity.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    (cfg.runtime_dir / "polarity.json").write_text(json.dumps(verdicts, indent=2))

    print(f"\n-> {path}")
    return 0 if all(v["agrees"] for v in verdicts.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
