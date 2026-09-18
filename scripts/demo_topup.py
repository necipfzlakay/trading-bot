#!/usr/bin/env python3
"""Request virtual funds on a Bybit demo account.

    python scripts/demo_topup.py --usdt 1000

Bybit rate-limits this to one call per minute. Demo balances are virtual and
cannot be transferred anywhere.
"""

from __future__ import annotations

import argparse

from _bootstrap import setup

from tbot.core.config import load_secrets
from tbot.core.models import Venue
from tbot.execution.ccxt_broker import CCXTBroker


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/demo.yaml")
    parser.add_argument("--usdt", default="1000")
    args = parser.parse_args()

    cfg = setup(args.config)
    secrets = load_secrets()

    if cfg.venue is not Venue.BYBIT_DEMO:
        print(f"Refusing: venue is {cfg.venue.value}, not bybit-demo.")
        return 1
    if not secrets.has_credentials(Venue.BYBIT_DEMO):
        print("BYBIT_DEMO_API_KEY / BYBIT_DEMO_API_SECRET are not set in .env")
        return 1

    broker = CCXTBroker(
        *secrets.credentials_for(Venue.BYBIT_DEMO), Venue.BYBIT_DEMO, cfg.quote_currency
    )
    broker.load_markets()

    print(f"Requesting {args.usdt} virtual USDT…")
    if not broker.top_up_demo({"USDT": args.usdt}):
        print("Top-up failed — Bybit allows one request per minute.")
        return 1

    balance = broker.fetch_balance()
    print("Balance:", {k: round(v, 2) for k, v in balance.items() if v > 0.01})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
