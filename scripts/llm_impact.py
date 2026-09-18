#!/usr/bin/env python3
"""Did the LLM roles earn their place?

Measures each role against its counterfactual. The event guard is the only role
that can affect trading, and it can only block entries — so the question is
concrete: what would the vetoed trades have done?

    python scripts/llm_impact.py --since 90d

A role that cannot show a net benefit gets switched off. Keeping it because it
sounds useful is how a system accumulates components nobody can justify.
"""

from __future__ import annotations

import argparse

import pandas as pd
from _bootstrap import setup

from tbot.core.clock import now_ms
from tbot.core.db import Database


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/base.yaml")
    parser.add_argument("--since", default="90d")
    args = parser.parse_args()

    cfg = setup(args.config)
    db = Database(cfg.db_path)
    days = int(args.since.rstrip("d"))
    since = now_ms() - days * 86_400_000

    calls = pd.DataFrame(db.query("SELECT * FROM llm_calls WHERE ts >= ?", (since,)))
    if calls.empty:
        print("No LLM calls recorded in this window.")
        return 0

    summary = (
        calls.groupby("role")
        .agg(
            calls=("id", "size"),
            parsed_ok=("parsed_ok", "sum"),
            avg_latency_ms=("latency_ms", "mean"),
            errors=("error", lambda s: int(s.notna().sum())),
        )
        .reset_index()
    )
    summary["success_rate"] = (summary["parsed_ok"] / summary["calls"]).round(3)

    vetoes = calls[
        (calls["role"] == "event_guard") & calls["decision"].str.contains("True", na=False)
    ]
    measured = vetoes[vetoes["counterfactual_r"].notna()]

    lines = [
        f"# LLM etki raporu — son {days} gün",
        "",
        "Her rol kendi karşı-olgusuna karşı ölçülüyor. Faydası gösterilemeyen rol kapatılır.",
        "",
        "## Çağrı istatistikleri",
        "",
        summary.round(1).to_markdown(index=False),
        "",
        "## Olay muhafızı — karşı-olgusal",
        "",
        f"- Veto sayısı: **{len(vetoes)}**",
        f"- Ölçülebilen (sonucu bilinen): **{len(measured)}**",
        "",
    ]

    if len(measured) >= 10:
        avoided = float(measured["counterfactual_r"].sum())
        mean_r = float(measured["counterfactual_r"].mean())
        helped = avoided < 0  # blocked trades that would have lost = value added
        lines += [
            f"- Veto edilen işlemlerin toplam sonucu olurdu: **{avoided:+.2f}R**",
            f"- İşlem başına ortalama: **{mean_r:+.3f}R**",
            "",
            f"**Karar: rol {'KALIYOR' if helped else 'KAPATILMALI'}** — "
            + (
                "veto edilen işlemler toplamda zarar edecekti, yani rol risk azalttı."
                if helped
                else "veto edilen işlemler toplamda kâr edecekti, yani rol fırsat maliyeti üretti."
            ),
            "",
        ]
    else:
        lines += [
            f"_Karşı-olgusal karar için en az 10 ölçülmüş veto gerekiyor "
            f"(şu an {len(measured)}). O zamana kadar rol denemede kalır._",
            "",
            f"Deneme süresi politikası: {cfg.llm.event_guard_min_trial_days} gün.",
            "",
        ]

    failures = calls[calls["error"].notna()]
    if not failures.empty:
        lines += [
            "## Hatalar",
            "",
            failures["error"].value_counts().head(10).to_markdown(),
            "",
            "LLM hatası işlem akışını etkilemez: sağlayıcı erişilemezse roller sessizce",
            "devre dışı kalır ve sistem tam normal çalışmaya devam eder.",
            "",
        ]

    path = cfg.reports_dir / "llm_impact.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")

    print(summary.round(1).to_string(index=False))
    print(f"\nvetoes: {len(vetoes)} (measured: {len(measured)})")
    print(f"-> {path}")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
