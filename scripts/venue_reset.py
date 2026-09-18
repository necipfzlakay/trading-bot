#!/usr/bin/env python3
"""Return a Bybit **demo** account to a clean, known state.

    python scripts/venue_reset.py --usdt 1000

Cancels every resting order, sells every non-quote holding back to the quote
currency, then adjusts the quote balance to the target.

Refuses to run against real money, unconditionally and with no override flag.
Selling an account's holdings is not something that should ever be one typo
away, and a demo reset has no legitimate live counterpart.
"""

from __future__ import annotations

import argparse
import time

from _bootstrap import setup

from tbot.core.config import load_secrets
from tbot.core.models import Venue
from tbot.execution.ccxt_broker import CCXTBroker

# Bybit rate-limits the demo funding endpoint to one call per minute.
FUNDING_COOLDOWN_S = 62


def _balances(broker: CCXTBroker, kind: str = "total") -> dict[str, float]:
    raw = broker.exchange.fetch_balance()
    return {k: float(v) for k, v in (raw.get(kind) or {}).items() if v}


def cancel_all_orders(broker: CCXTBroker, symbols: list[str]) -> int:
    """Cancel resting orders, protective stops included.

    Stops must go first: on spot they reserve the base asset, so a holding with
    a live stop cannot be sold until the stop is released.
    """
    cancelled = 0
    for symbol in symbols:
        for order_filter in ("Order", "tpslOrder", "StopOrder"):
            try:
                response = broker.exchange.privateGetV5OrderRealtime(
                    {
                        "category": "spot",
                        "symbol": symbol.replace("/", ""),
                        "orderFilter": order_filter,
                    }
                )
            except Exception as exc:  # noqa: BLE001 — a venue error must not stop the sweep
                print(f"  ! {symbol} {order_filter}: {str(exc)[:80]}")
                continue

            for order in (response.get("result") or {}).get("list") or []:
                order_id = str(order.get("orderId") or "")
                if not order_id:
                    continue
                try:
                    broker.exchange.cancel_order(id=order_id, symbol=symbol)
                    print(f"  cancelled {symbol} {order_filter} {order_id[-8:]}")
                    cancelled += 1
                except Exception as exc:  # noqa: BLE001
                    print(f"  ! cancel {order_id[-8:]}: {str(exc)[:80]}")
    return cancelled


def liquidate(broker: CCXTBroker, quote: str) -> tuple[int, list[str]]:
    """Market-sell every non-quote balance. Returns (sold, dust left behind)."""
    sold, dust = 0, []

    for coin, amount in sorted(_balances(broker).items()):
        if coin == quote or amount <= 0:
            continue

        symbol = f"{coin}/{quote}"
        try:
            spec = broker.market_spec(symbol)
        except Exception:  # noqa: BLE001 — not every holding has a quote pair
            dust.append(f"{coin} {amount:g} (no {symbol} market)")
            continue

        # Sell only what is free: anything still reserved means a cancel did
        # not take, and asking to sell it would just be rejected.
        free = _balances(broker, "free").get(coin, 0.0)
        if free < amount:
            print(f"  ! {coin}: {amount:g} held but only {free:g} free")
        quantity = free

        try:
            price = float(broker.exchange.fetch_ticker(symbol)["last"])
        except Exception as exc:  # noqa: BLE001
            dust.append(f"{coin} {quantity:g} (no price: {str(exc)[:40]})")
            continue

        if spec.min_notional and quantity * price < spec.min_notional:
            # Below the exchange minimum there is no way to sell it at all.
            dust.append(f"{coin} {quantity:g} (~{quantity * price:.2f} {quote}, below minimum)")
            continue

        try:
            amount_str = broker.exchange.amount_to_precision(symbol, quantity)
            broker.exchange.create_order(
                symbol=symbol, type="market", side="sell", amount=float(amount_str)
            )
            print(f"  sold {amount_str} {coin} (~{quantity * price:.2f} {quote})")
            sold += 1
        except Exception as exc:  # noqa: BLE001
            dust.append(f"{coin} {quantity:g} (sell refused: {str(exc)[:60]})")

    return sold, dust


def set_quote_balance(broker: CCXTBroker, quote: str, target: float) -> float:
    """Move the quote balance toward ``target``. Returns what it actually became.

    ``adjustType`` 0 adds virtual funds and 1 removes them. Bybit does not
    always move the exact amount requested, so the result is read back and
    reported rather than assumed.
    """
    current = _balances(broker).get(quote, 0.0)
    delta = target - current
    if abs(delta) < 1.0:
        print(f"  {quote} already {current:.2f}, no adjustment needed")
        return current

    adjust_type = 0 if delta > 0 else 1
    verb = "adding" if delta > 0 else "removing"
    print(f"  {verb} {abs(delta):.2f} {quote} (have {current:.2f}, want {target:.2f})")

    try:
        broker.exchange.privatePostV5AccountDemoApplyMoney(
            {
                "adjustType": adjust_type,
                "utaDemoApplyMoney": [{"coin": quote, "amountStr": f"{abs(delta):.2f}"}],
            }
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  ! adjustment refused: {str(exc)[:120]}")
        return current

    time.sleep(4)
    actual = _balances(broker).get(quote, 0.0)
    if abs(actual - target) > max(1.0, target * 0.01):
        print(f"  ! landed on {actual:.2f}, not {target:.2f}")
        print(f"    Bybit rate-limits this to one call per {FUNDING_COOLDOWN_S}s; re-run to correct.")
    return actual


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/demo.yaml")
    parser.add_argument("--usdt", type=float, default=1000.0, help="balance to end up with")
    parser.add_argument("--keep-balance", action="store_true", help="do not adjust the balance")
    args = parser.parse_args()

    cfg = setup(args.config)
    secrets = load_secrets()

    if cfg.venue is not Venue.BYBIT_DEMO:
        print(f"REFUSED: venue is {cfg.venue.value}. This only ever runs against bybit-demo.")
        return 1
    if not secrets.has_credentials(Venue.BYBIT_DEMO):
        print("BYBIT_DEMO_API_KEY / BYBIT_DEMO_API_SECRET are not set in .env")
        return 1

    broker = CCXTBroker(
        *secrets.credentials_for(Venue.BYBIT_DEMO), Venue.BYBIT_DEMO, cfg.quote_currency
    )
    broker.load_markets()
    quote = cfg.quote_currency

    print("\nBefore:")
    for coin, amount in sorted(_balances(broker).items()):
        print(f"  {coin:8} {amount:,.4f}")

    # Every coin held, plus the configured universe: a holding can exist for a
    # symbol the universe no longer contains, and its stop still needs cancelling.
    held = [f"{c}/{quote}" for c in _balances(broker) if c != quote]
    symbols = sorted(set(held) | set(cfg.universe.symbols or []))

    print("\nCancelling resting orders…")
    print(f"  {cancel_all_orders(broker, symbols)} cancelled")

    print("\nSelling holdings…")
    sold, dust = liquidate(broker, quote)
    print(f"  {sold} sold")
    for item in dust:
        print(f"  left: {item}")

    if not args.keep_balance:
        print(f"\nSetting {quote} to {args.usdt:,.2f}…")
        time.sleep(3)  # let the sale proceeds settle before reading the balance
        set_quote_balance(broker, quote, args.usdt)

    print("\nAfter:")
    for coin, amount in sorted(_balances(broker).items()):
        print(f"  {coin:8} {amount:,.4f}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
