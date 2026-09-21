#!/usr/bin/env python3
"""Who owns which perpetual on the shared account.

    python scripts/claims.py                     # list every claim
    python scripts/claims.py --release SOL/USDT:USDT

Engines release their own claims once the venue shows the symbol flat. This is
for the case they cannot: an engine that will never start again, or a claim the
operator has checked by hand. Releasing a claim whose symbol still carries size
lets another engine open into it, and the venue nets the two — so ``--release``
refuses unless the venue confirms the symbol is flat, and ``--force`` says the
operator has confirmed it instead.
"""

from __future__ import annotations

import argparse

from _bootstrap import setup

from tbot.core.clock import iso
from tbot.core.config import load_secrets
from tbot.engine.claims import SymbolClaims
from tbot.execution.ccxt_broker import PerpBroker


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/fast.yaml")
    parser.add_argument("--release", metavar="SYMBOL")
    parser.add_argument("--force", action="store_true", help="skip the venue flat check")
    args = parser.parse_args()

    cfg = setup(args.config)
    claims = SymbolClaims(cfg.engine.claims_path, owner="operator")

    if args.release:
        if not args.force:
            key, secret = load_secrets().credentials_for(cfg.venue)
            held = PerpBroker(key, secret, cfg.venue, cfg.quote_currency).fetch_positions()
            if held is None:
                raise SystemExit("venue positions unreadable — refusing; use --force if checked")
            if float(held.get(args.release, 0.0)) > 0.0:
                raise SystemExit(
                    f"{args.release} still holds {held[args.release]} at the venue — "
                    "releasing it would let another engine net into it"
                )
        owner = claims.force_release(args.release)
        print(f"released {args.release} (was {owner or 'unclaimed'})")
        return 0

    rows = claims.all()
    if not rows:
        print("no claims")
        return 0
    print(f"{'symbol':22}{'owner':8}{'since':22}reason")
    for symbol, owner, ts, reason in rows:
        print(f"{symbol:22}{owner:8}{iso(ts):22}{reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
