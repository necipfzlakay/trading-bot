#!/usr/bin/env python3
"""Measure which indicators actually predict anything, and which are duplicates.

Produces two reports that together decide what the conviction engine is allowed
to use:

* ``runtime/reports/ic.md`` — Information Coefficient per indicator, per regime,
  per horizon, with stability and cross-symbol consistency
* ``runtime/reports/redundancy.md`` — correlation blocks: which indicators are
  measuring the same thing and therefore share a single vote

    python scripts/ic_report.py
    python scripts/ic_report.py --timeframe 4h --by-regime
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402

from tbot.core.config import load_config  # noqa: E402
from tbot.core.logging import configure, get_logger  # noqa: E402
from tbot.data.store import ParquetStore  # noqa: E402
from tbot.indicators import ic as ic_mod  # noqa: E402
from tbot.indicators import redundancy as red_mod  # noqa: E402
from tbot.indicators.registry import REGISTRY  # noqa: E402
from tbot.strategy.regime import classify_series  # noqa: E402


def load_universe(cfg) -> list[str]:
    path = cfg.runtime_dir / "universe.json"
    if cfg.universe.symbols:
        return cfg.universe.symbols
    if path.exists():
        return json.loads(path.read_text())["symbols"]
    return ParquetStore(cfg.data.parquet_dir).symbols()


def write_ic_report(agg: pd.DataFrame, raw: pd.DataFrame, cfg, path: Path) -> None:
    min_ic, min_ir = cfg.indicators.min_abs_ic, cfg.indicators.min_ic_ir
    selected = ic_mod.select_indicators(agg, min_ic, min_ir)

    lines = [
        "# Information Coefficient raporu",
        "",
        "IC = indikatör sinyali ile ileriye dönük getiri arasındaki sıra korelasyonu.",
        "Bu rapor 'hangi indikatör işe yarıyor' sorusunun varsayım değil, ölçüm cevabı.",
        "",
        f"- Sembol: **{raw['symbol'].nunique()}**",
        f"- İndikatör: **{agg['indicator'].nunique()}**",
        f"- Eşikler: `|IC| >= {min_ic}`, `|IC-IR| >= {min_ir}`, tutarlılık >= 0.4",
        "",
        "`consistency` = semboller arasında IC işaretinin uyuşma oranı. Düşükse ortalama",
        "bir etkiyi değil, birbirini götüren sonuçları gösteriyor demektir.",
        "",
        "`t_stat` örtüşen pencereler için düzeltilmiş: 24 barlık getiriyi her bar örneklemek",
        "aynı geleceği 24 kez saydırır, etkin örneklem buna göre küçültülür.",
        "",
        "## Genel (tüm rejimler)",
        "",
    ]

    overall = agg[agg["regime"] == "all"].sort_values("ic", key=abs, ascending=False)
    cols = [
        "indicator",
        "category",
        "role",
        "horizon",
        "ic",
        "ic_ir",
        "t_stat",
        "consistency",
        "symbols",
    ]
    lines += [overall[cols].round(4).to_markdown(index=False), ""]

    lines += ["## Rejim bazlı — yönlü indikatörler", ""]
    for regime in ("trend_up", "trend_down", "range", "high_vol"):
        subset = agg[(agg["regime"] == regime) & (agg["role"] == "directional")]
        if subset.empty:
            continue
        best = subset.sort_values("ic", key=abs, ascending=False).head(12)
        lines += [f"### {regime}", "", best[cols].round(4).to_markdown(index=False), ""]

    lines += [
        "## Seçim sonucu",
        "",
        "Eşikleri geçen indikatörler conviction skoruna girer; geçemeyenler ne kadar",
        "popüler olursa olsun girmez.",
        "",
    ]
    for regime, names in sorted(selected.items()):
        rejected = sorted(set(REGISTRY) - set(names))
        lines += [
            f"- **{regime}** — geçen ({len(names)}): {', '.join(names) if names else '_yok_'}",
            f"  - elenen: {', '.join(rejected[:18])}",
        ]
    lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def write_tail_report(tails: pd.DataFrame, cfg, path: Path, cost: float) -> None:
    """Conditional returns in the extremes — what the bot will actually trade."""
    tradeable = tails[tails["tradeable"]]
    cols = [
        "indicator",
        "category",
        "horizon",
        "top_decile",
        "base",
        "top_excess",
        "hit_rate",
        "ci_low",
        "ci_high",
        "effective",
        "tradeable",
    ]

    lines = [
        "# Kuyruk (tail) analizi — botun gerçekten alacağı işlemler",
        "",
        "IC tüm barlardaki monotonik ilişkiyi ölçer. Bu bot dağılımın ortasında asla",
        "işlem yapmaz; sadece uçları bekler. Bu yüzden ortada düz, uçlarda güçlü olan bir",
        "indikatör IC'de sıfıra yakın (hatta negatif) çıkarken stratejinin tam da aradığı",
        "şey olabilir. Nitekim bu veride öyle oldu.",
        "",
        f"- Varsayılan gidiş-dönüş maliyet: **{cost:.2%}**",
        "- `top_excess` = üst desil getirisi − taban oran. Ham getiri değil **fazlalık**",
        "  ölçülüyor: her şeyin yükseldiği bir dönemde ham %4 hiçbir şey ifade etmez.",
        "- `ci_low`/`ci_high` = **blok** bootstrap %95 güven aralığı, maliyet düşülmüş.",
        "  Blok uzunluğu = ufuk; tek tek barları yeniden örneklemek örtüşen pencereleri",
        "  bağımsız sayar ve aralığı kat kat dar gösterir.",
        "- `effective` = örtüşme için düzeltilmiş etkin örneklem.",
        "",
        f"**Ticarete uygun bulunan** (fazlalık > 0 ve ci_low > 0): **{len(tradeable)}/{len(tails)}**",
        "",
        "## Tüm rejimler",
        "",
    ]
    allr = tails[tails["regime"] == "all"].sort_values("top_excess", ascending=False)
    lines += [allr[cols].round(4).to_markdown(index=False), ""]

    for regime in ("trend_up", "range", "trend_down", "high_vol"):
        subset = tails[tails["regime"] == regime]
        if subset.empty:
            continue
        best = subset.sort_values("top_excess", ascending=False).head(12)
        lines += [f"## {regime}", "", best[cols].round(4).to_markdown(index=False), ""]

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def write_redundancy_report(corr: pd.DataFrame, blocks, path: Path, threshold: float) -> None:
    summary = red_mod.summarise(blocks)
    leakage = red_mod.cross_category_leakage(blocks)
    collapsed = [b for b in blocks if b.is_collapsed]

    lines = [
        "# Redundans raporu",
        "",
        f"Eşik: `|ρ| >= {threshold}` olan indikatörler tek oy bloğuna düşer.",
        "",
        f"- İndikatör: **{len(corr.columns) if not corr.empty else 0}**",
        f"- Bağımsız oy bloğu: **{len(blocks)}**",
        f"- Birleşen blok: **{len(collapsed)}**",
        "",
        "Traderların en sık hatası korelasyonlu indikatörleri ayrı onay saymak.",
        "RSI + StochRSI + ROC aynı yönde ise bu 3 onay değil, 1 okumanın 3 kopyası.",
        "",
        "## Oy blokları",
        "",
        summary.to_markdown(index=False) if not summary.empty else "_blok yok_",
        "",
    ]

    if not leakage.empty:
        lines += [
            "## Kategori sızıntısı",
            "",
            "Kategori bir ön kabuldü, korelasyon ölçüm. Aşağıdaki bloklar kategori",
            "sınırını aşıyor — yani taksonomi orada yanılıyor.",
            "",
            leakage.to_markdown(index=False),
            "",
        ]

    if not corr.empty:
        lines += ["## Korelasyon matrisi", "", corr.round(2).to_markdown(), ""]

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/base.yaml")
    parser.add_argument("--timeframe", default="")
    parser.add_argument("--symbols", default="")
    args = parser.parse_args()

    cfg = load_config(args.config)
    configure(cfg.log_dir, level=cfg.log_level)
    log = get_logger("ic_report")

    timeframe = args.timeframe or cfg.timeframes.setup
    symbols = (
        [s.strip() for s in args.symbols.split(",") if s.strip()]
        if args.symbols
        else load_universe(cfg)
    )
    store = ParquetStore(cfg.data.parquet_dir)
    symbols = [s for s in symbols if store.ohlcv_path(s, timeframe).exists()]

    if not symbols:
        log.error("no_data", hint="run scripts/backfill.py first")
        return 1

    print(
        f"IC analysis: {len(symbols)} symbols, {timeframe}, horizons {cfg.indicators.ic_horizons}"
    )

    raw = ic_mod.analyse_universe(
        store,
        symbols,
        timeframe,
        cfg.indicators.ic_horizons,
        regime_timeframe=cfg.timeframes.permission,
        enabled=cfg.indicators.enabled or None,
    )
    if raw.empty:
        log.error("ic_empty")
        return 1

    agg = ic_mod.aggregate(raw)
    write_ic_report(agg, raw, cfg, cfg.reports_dir / "ic.md")

    # Tail analysis carries more weight than the IC for this system, because the
    # engine only ever acts on extremes. Horizons are scaled to the timeframe so
    # they cover comparable calendar spans.
    cost = cfg.execution.assumed_taker_fee * 2 + cfg.execution.slippage_bps / 10_000 * 2
    tail_horizons = [h * 2 for h in cfg.indicators.ic_horizons] + [42]
    tails = ic_mod.analyse_tails(
        store,
        symbols,
        timeframe,
        sorted(set(tail_horizons)),
        round_trip_cost=cost,
        enabled=cfg.indicators.enabled or None,
        regime_timeframe=cfg.timeframes.permission,
    )
    if not tails.empty:
        write_tail_report(tails, cfg, cfg.reports_dir / "tails.md", cost)

    # Block representatives are chosen by measured IC, so redundancy needs the
    # IC pass to have run first.
    overall = agg[(agg["regime"] == "all")]
    ic_scores = overall.groupby("indicator")["ic"].apply(lambda s: s.abs().max()).to_dict()

    regime_labels = {}
    for symbol in symbols:
        fast = store.read_ohlcv(symbol, timeframe)
        slow = store.read_ohlcv(symbol, cfg.timeframes.permission)
        if len(fast) < 300 or len(slow) < 250:
            continue
        regime_labels[symbol] = ic_mod._map_regime_down(
            classify_series(slow), slow["ts"], fast["ts"]
        )

    corr, blocks = red_mod.analyse_universe(
        store,
        symbols,
        timeframe,
        cfg.indicators.redundancy_threshold,
        enabled=cfg.indicators.enabled or None,
        ic_scores=ic_scores,
    )
    write_redundancy_report(
        corr, blocks, cfg.reports_dir / "redundancy.md", cfg.indicators.redundancy_threshold
    )

    by_regime = red_mod.analyse_by_regime(
        store,
        symbols,
        timeframe,
        regime_labels,
        cfg.indicators.redundancy_threshold,
        enabled=cfg.indicators.enabled or None,
        ic_scores=ic_scores,
    )
    selection = {
        "timeframe": timeframe,
        "round_trip_cost": cost,
        # Tail selection is authoritative; the IC selection is kept alongside it
        # because the disagreement between the two is itself a finding.
        "tail_selected": ic_mod.select_by_tails(tails) if not tails.empty else {},
        "ic_selected": ic_mod.select_indicators(
            agg, cfg.indicators.min_abs_ic, cfg.indicators.min_ic_ir
        ),
        "blocks": {
            regime: [
                {"members": b.members, "representative": b.representative} for b in regime_blocks
            ]
            for regime, regime_blocks in by_regime.items()
        },
    }
    (cfg.runtime_dir / "indicator_selection.json").write_text(json.dumps(selection, indent=2))

    print(f"\n-> {cfg.reports_dir / 'ic.md'}")
    print(f"-> {cfg.reports_dir / 'tails.md'}")
    print(f"-> {cfg.reports_dir / 'redundancy.md'}")
    print(f"-> {cfg.runtime_dir / 'indicator_selection.json'}")
    if not tails.empty:
        best = tails[tails["regime"] == "all"].nlargest(5, "top_excess")
        print(f"\nTop decile excess over base (cost {cost:.2%} round trip):")
        for _, row in best.iterrows():
            flag = "OK " if row["tradeable"] else "no "
            print(
                f"  {flag} {row['indicator']:<14} h={int(row['horizon']):>2}  "
                f"excess={row['top_excess']:+.2%}  hit={row['hit_rate']:.0%}  "
                f"CI=[{row['ci_low']:+.2%}, {row['ci_high']:+.2%}]"
            )

    print(
        f"\nVoting blocks (all regimes pooled): {len(blocks)} from {len(corr.columns)} indicators"
    )
    for b in blocks:
        if b.is_collapsed:
            print(
                f"  block {b.block_id}: {b.members} -> {b.representative} (max |rho|={b.max_internal_corr})"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
