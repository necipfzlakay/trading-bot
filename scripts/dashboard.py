#!/usr/bin/env python3
"""Serve the local dashboard.

    python scripts/dashboard.py

Binds to localhost only. It reads the same files and tables the engine writes,
so it cannot disagree with the bot about what is happening.
"""

from __future__ import annotations

import argparse

import uvicorn
from _bootstrap import setup

from tbot.web.app import create_app


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/base.yaml")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()

    cfg = setup(args.config)
    port = args.port or cfg.web.port

    print(f"\n  dashboard  http://{cfg.web.host}:{port}\n")
    uvicorn.run(create_app(cfg), host=cfg.web.host, port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
