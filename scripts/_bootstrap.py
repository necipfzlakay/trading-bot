"""Shared CLI setup: path, config, logging, and the artefacts the engine needs."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tbot.core.config import Config, load_config, load_secrets  # noqa: E402
from tbot.core.logging import configure  # noqa: E402
from tbot.data.store import ParquetStore  # noqa: E402


def setup(config_path: str = "config/base.yaml") -> Config:
    cfg = load_config(config_path)
    configure(cfg.log_dir, level=cfg.log_level)
    return cfg


def universe(cfg: Config) -> list[str]:
    """Symbols to trade, in priority order: config, saved universe, then archive."""
    if cfg.universe.symbols:
        return cfg.universe.symbols
    path = cfg.runtime_dir / "universe.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))["symbols"]
    return ParquetStore(cfg.data.parquet_dir).symbols()


def indicator_selection(cfg: Config) -> tuple[dict[str, int], dict[str, list[str]], dict[str, int]]:
    """Load what the IC and redundancy study decided.

    Missing file means the study has not run: the engine then treats every
    indicator as its own block and lets them all vote, which is the honest
    fallback — no measurement, no filtering.
    """
    path = cfg.runtime_dir / "indicator_selection.json"
    if not path.exists():
        return {}, {}, {}

    data = json.loads(path.read_text(encoding="utf-8"))
    blocks: dict[str, int] = {}
    seen: list[list[str]] = []
    for regime_blocks in data.get("blocks", {}).values():
        for block in regime_blocks:
            members = block["members"]
            if members in seen:
                continue
            index = len(seen)
            seen.append(members)
            for member in members:
                blocks.setdefault(member, index)

    selected = data.get("tail_selected") or data.get("ic_selected") or {}
    decay = data.get("edge_decay_bars", {})
    return blocks, selected, decay


__all__ = ["ROOT", "setup", "universe", "indicator_selection", "load_secrets", "ParquetStore"]
