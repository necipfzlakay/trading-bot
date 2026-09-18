#!/usr/bin/env python3
"""Status report — the file to read when asking "how is it going?".

    python scripts/report.py --since 30d

Written as markdown so it can be read directly, diffed, or handed to someone
else. Every number comes from the database; nothing is estimated.
"""

from __future__ import annotations

import argparse
import json

import numpy as np
import pandas as pd
from _bootstrap import setup

from tbot.core.clock import iso, now_ms
from tbot.core.db import Database


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/base.yaml")
    parser.add_argument("--since", default="30d")
    args = parser.parse_args()

    cfg = setup(args.config)
    db = Database(cfg.db_path)
    days = int(args.since.rstrip("d"))
    since = now_ms() - days * 86_400_000

    trades = pd.DataFrame(db.query("SELECT * FROM trades WHERE exit_ts >= ?", (since,)))
    equity = pd.DataFrame(db.query("SELECT * FROM equity WHERE ts >= ? ORDER BY ts", (since,)))
    scans = pd.DataFrame(
        db.query(
            "SELECT no_trade_reason, COUNT(*) n FROM scans WHERE bar_ts >= ? "
            "GROUP BY no_trade_reason ORDER BY n DESC",
            (since,),
        )
    )
    events = pd.DataFrame(
        db.query(
            "SELECT ts, level, kind, message FROM events WHERE ts >= ? ORDER BY ts DESC LIMIT 30",
            (since,),
        )
    )
    llm = pd.DataFrame(
        db.query(
            "SELECT role, COUNT(*) calls, SUM(parsed_ok) ok, AVG(latency_ms) latency "
            "FROM llm_calls WHERE ts >= ? GROUP BY role",
            (since,),
        )
    )

    state_path = cfg.state_path
    state = json.loads(state_path.read_text()) if state_path.exists() else {}

    lines = [
        f"# Durum raporu — son {days} gün",
        "",
        f"Üretildi: {iso(now_ms())} UTC",
        "",
        "## Anlık durum",
        "",
        f"- Durum: **{state.get('status', 'bilinmiyor')}**"
        + (f" ({state['degraded_reason']})" if state.get("degraded_reason") else ""),
        f"- Mod: {state.get('mode', '—')} / {state.get('venue', '—')}",
        f"- Equity: **{state.get('equity', 0):.2f}** "
        f"(tepe {state.get('peak_equity', 0):.2f}, düşüş {state.get('drawdown', 0):.1%})",
        f"- Risk çarpanı: {state.get('risk_multiplier', 1):.2f}× · "
        f"conviction eşiği: {state.get('threshold', 0):.0f}",
        f"- Açık pozisyon: {len(state.get('open_positions', []))} · "
        f"uptime: {state.get('uptime_human', '—')} · restart: {state.get('restarts', 0)}",
        f"- İzin verilen kaldıraç: {state.get('leverage_allowed', 1):.1f}×",
        "",
    ]

    if state.get("last_error"):
        lines += [
            f"- Son hata ({iso(state.get('last_error_ts', 0))}): `{state['last_error'][:200]}`",
            "",
        ]

    lines += ["## İşlemler", ""]
    if trades.empty:
        lines += ["_Bu dönemde kapanan işlem yok._", ""]
    else:
        wins = trades[trades["pnl"] > 0]
        gross_win = float(wins["pnl"].sum())
        gross_loss = float(-trades[trades["pnl"] <= 0]["pnl"].sum())
        lines += [
            f"- Kapanan: **{len(trades)}** · kazanma oranı **{len(wins) / len(trades):.1%}**",
            f"- Expectancy: **{trades['r_multiple'].mean():+.3f}R** · "
            f"profit factor: **{gross_win / gross_loss if gross_loss else float('inf'):.2f}**",
            f"- Net P&L: **{trades['pnl'].sum():+.4f}** · komisyon: {trades['fees'].sum():.4f}",
            f"- Ortalama MFE {trades['mfe_r'].mean():+.2f}R · "
            f"ortalama MAE {trades['mae_r'].mean():+.2f}R",
            f"- Ortalama tutuş: {trades['bars_held'].mean():.0f} bar",
            "",
            "### Kurulum bazlı",
            "",
            trades.groupby("setup")
            .agg(
                n=("r_multiple", "size"),
                expectancy=("r_multiple", "mean"),
                win_rate=("pnl", lambda s: float((s > 0).mean())),
            )
            .round(3)
            .to_markdown(),
            "",
            "### Çıkış nedenleri",
            "",
            trades["exit_reason"].value_counts().to_markdown(),
            "",
        ]

    lines += ["## Neden işlem yapılmadı", ""]
    if scans.empty:
        lines += ["_Tarama kaydı yok._", ""]
    else:
        total = int(scans["n"].sum())
        scans["share"] = (scans["n"] / total).round(4)
        lines += [
            f"Toplam {total:,} sembol-bar tarandı.",
            "",
            scans.to_markdown(index=False),
            "",
            "Bu tablo 'bot neden dün hiçbir şey yapmadı' sorusunu tahminle değil,",
            "sorguyla cevaplıyor.",
            "",
        ]

    if not equity.empty:
        eq = equity["equity"].to_numpy()
        peaks = np.maximum.accumulate(eq)
        lines += [
            "## Equity",
            "",
            f"- {eq[0]:.2f} → {eq[-1]:.2f} (**{eq[-1] / eq[0] - 1:+.2%}**)",
            f"- Dönem içi maks düşüş: **{float(np.max(1 - eq / peaks)):.1%}**",
            f"- Kayıt sayısı: {len(equity):,}",
            "",
        ]

    if not llm.empty:
        lines += ["## LLM çağrıları", "", llm.round(1).to_markdown(index=False), ""]

    if not events.empty:
        lines += ["## Olaylar", ""]
        for _, row in events.iterrows():
            lines.append(f"- `{iso(int(row['ts']))}` **{row['kind']}** — {row['message'][:160]}")
        lines.append("")

    path = cfg.reports_dir / "status.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")

    print("\n".join(lines[:26]))
    print(f"\n-> {path}")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
