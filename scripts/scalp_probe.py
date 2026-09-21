#!/usr/bin/env python3
"""Ask the venue what a one-minute maker scalper needs, before building it.

    python scripts/scalp_probe.py
    python scripts/scalp_probe.py --symbol XPL/USDT:USDT --write research/scalp_venue_facts.md

The scalp engine rests on five venue behaviours that no library flag can vouch
for. This repo has paid for trusting flags before — ``fetchTradingFees`` said
yes and spot said no; ``edit_order`` built an amend that spot refused on every
trail (audit #72). So each one is asked by placing an order:

1. Does a **PostOnly** limit entry accept an **attached** stop-loss (market) and
   take-profit (limit) in the same call? The whole design needs the position to
   be protected from the instant it fills, by the venue, whether or not this
   process is alive (invariant 12).
2. Can that resting entry be cancelled, and does the history say so? The engine
   cancels every entry its TTL outlives, and a cancel that silently fails is
   worse than none (invariant 41).
3. What happens to a PostOnly that would cross the book — rejected, or cancelled
   after acceptance? The engine has to recognise it as "no fill", not an error.
4. When an order with attached TP/SL fills, do both legs appear, and do they go
   away when the position closes? Leftover reduce-only orders would sit on a
   symbol another engine may claim next.
5. How many one-minute kline topics does the **linear** public websocket accept
   in one subscription? Spot hangs silently at eleven (invariant 43).

Plus two read-only answers the capital isolation needs: which wallet field is
"the USDT this account owns", and what the maker fee really is.

The probe symbol must not belong to any perp engine's universe and must hold no
position — the fast engine is live on this account, and a probe order on one of
its symbols would reach its reconciliation. Refuses any venue but
``bybit-demo``, with no override.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from _bootstrap import setup

from tbot.core.config import load_config, load_secrets
from tbot.core.models import Venue
from tbot.execution.ccxt_broker import PerpBroker

PROBE_NOTIONAL_USD = 8.0  # clears the $5 floor, noise against the balance
KLINE_TOPIC_TRIALS = (10, 11, 20)
TOPIC_TIMEOUT_S = 15.0


@dataclass
class Finding:
    question: str
    ok: bool | None
    answer: str
    detail: str = ""

    def render(self) -> str:
        mark = {True: "✅", False: "❌", None: "•"}[self.ok]
        return f"| {self.question} | {mark} {self.answer} |"


@dataclass
class Probe:
    broker: PerpBroker
    symbol: str
    findings: list[Finding] = field(default_factory=list)

    @property
    def bare(self) -> str:
        return self.broker._venue_symbol(self.symbol)

    def record(self, question: str, ok: bool | None, answer: str, detail: str = "") -> None:
        self.findings.append(Finding(question, ok, answer, detail))
        mark = {True: "OK  ", False: "FAIL", None: "--  "}[ok]
        print(f"  {mark} {question}\n       {answer}")
        if detail:
            print(f"       {detail}")

    def v5(self, method: str, params: dict) -> dict:
        return getattr(self.broker.exchange, method)(params)

    def px(self, price: float) -> str:
        return self.broker.exchange.price_to_precision(self.symbol, price)

    def qty(self, price: float) -> str:
        spec = self.broker.market_spec(self.symbol)
        return self.broker.exchange.amount_to_precision(
            self.symbol, max(PROBE_NOTIONAL_USD / price, spec.min_amount)
        )

    def book(self) -> tuple[float, float]:
        rows = self.v5(
            "publicGetV5MarketTickers", {"category": "linear", "symbol": self.bare}
        )["result"]["list"]
        return float(rows[0]["bid1Price"]), float(rows[0]["ask1Price"])

    def history(self, order_id: str) -> dict | None:
        for _ in range(8):
            rows = self.v5(
                "privateGetV5OrderHistory",
                {"category": "linear", "symbol": self.bare, "orderId": order_id},
            )["result"]["list"]
            if rows:
                return rows[0]
            time.sleep(0.75)
        return None

    def realtime(self, **filters) -> list[dict]:
        return self.v5(
            "privateGetV5OrderRealtime", {"category": "linear", "symbol": self.bare, **filters}
        )["result"]["list"]

    def position_size(self) -> float:
        rows = self.v5(
            "privateGetV5PositionList", {"category": "linear", "symbol": self.bare}
        )["result"]["list"]
        return float(rows[0]["size"]) if rows else 0.0

    def create(self, **fields) -> str:
        body = {"category": "linear", "symbol": self.bare, "positionIdx": 0, **fields}
        return str(self.v5("privatePostV5OrderCreate", body)["result"]["orderId"])

    def cancel(self, order_id: str) -> None:
        self.v5(
            "privatePostV5OrderCancel",
            {"category": "linear", "symbol": self.bare, "orderId": order_id},
        )

    # -- the questions -----------------------------------------------------

    def check_resting_entry_with_protection(self) -> None:
        """Questions 1 and 2: rest a PostOnly entry with TP/SL attached, then cancel."""
        bid, _ask = self.book()
        price = bid * 0.97  # far enough below the book that it cannot fill
        order_id = None
        try:
            order_id = self.create(
                side="Buy",
                orderType="Limit",
                qty=self.qty(price),
                price=self.px(price),
                timeInForce="PostOnly",
                orderLinkId=f"tb-probe-rest-{int(time.time())}",
                stopLoss=self.px(price * 0.997),
                takeProfit=self.px(price * 1.003),
                tpslMode="Partial",
                tpOrderType="Limit",
                tpLimitPrice=self.px(price * 1.003),
                slOrderType="Market",
                tpTriggerBy="LastPrice",
                slTriggerBy="LastPrice",
            )
        except Exception as exc:  # noqa: BLE001 — the message is the finding
            self.record(
                "PostOnly limit + attached SL(market) + TP(limit) in one call", False, str(exc)[:200]
            )
            return

        try:
            rows = [r for r in self.realtime(orderId=order_id)]
            row = rows[0] if rows else {}
            self.record(
                "PostOnly limit + attached SL(market) + TP(limit) in one call",
                bool(row),
                f"status={row.get('orderStatus')} tif={row.get('timeInForce')} "
                f"SL={row.get('stopLoss')}/{row.get('slOrderType')} "
                f"TP={row.get('takeProfit')}/{row.get('tpOrderType')} "
                f"tpLimit={row.get('tpLimitPrice')} mode={row.get('tpslMode')}",
                "the entry carries its own protection; the venue arms it on fill",
            )
        finally:
            try:
                self.cancel(order_id)
                state = self.history(order_id) or {}
                self.record(
                    "cancel a resting PostOnly entry, confirmed from history",
                    state.get("orderStatus") == "Cancelled",
                    f"history status={state.get('orderStatus')} "
                    f"reason={state.get('cancelType') or state.get('rejectReason')}",
                )
            except Exception as exc:  # noqa: BLE001
                self.record(
                    "cancel a resting PostOnly entry, confirmed from history",
                    False,
                    str(exc)[:200],
                    f"ORDER {order_id} MAY STILL REST — cancel it by hand",
                )

    def check_crossing_post_only(self) -> None:
        """Question 3: a PostOnly that would take liquidity."""
        _bid, ask = self.book()
        price = ask * 1.01
        try:
            order_id = self.create(
                side="Buy",
                orderType="Limit",
                qty=self.qty(price),
                price=self.px(price),
                timeInForce="PostOnly",
                orderLinkId=f"tb-probe-cross-{int(time.time())}",
            )
        except Exception as exc:  # noqa: BLE001
            self.record("PostOnly that would cross the book", None, f"refused at submit: {str(exc)[:160]}")
            return
        state = self.history(order_id) or {}
        filled = float(state.get("cumExecQty") or 0.0)
        self.record(
            "PostOnly that would cross the book",
            filled == 0.0,
            f"accepted, then status={state.get('orderStatus')} "
            f"reason={state.get('rejectReason') or state.get('cancelType')} filled={filled}",
            "the engine must read this as 'no fill', and release its claim",
        )
        if filled > 0:
            self.close_all()

    def check_filled_entry_arms_protection(self) -> None:
        """Question 4: fill with TP/SL attached, check both legs, close, check cleanup."""
        _bid, ask = self.book()
        amount = self.qty(ask)
        try:
            self.create(
                side="Buy",
                orderType="Market",
                qty=amount,
                orderLinkId=f"tb-probe-fill-{int(time.time())}",
                stopLoss=self.px(ask * 0.99),
                takeProfit=self.px(ask * 1.01),
                tpslMode="Partial",
                tpOrderType="Limit",
                tpLimitPrice=self.px(ask * 1.01),
                slOrderType="Market",
                tpTriggerBy="LastPrice",
                slTriggerBy="LastPrice",
            )
        except Exception as exc:  # noqa: BLE001
            self.record("filled entry arms its attached TP/SL", False, str(exc)[:200])
            return

        try:
            time.sleep(2.0)
            legs = self.realtime(orderFilter="StopOrder") + self.realtime(orderFilter="tpslOrder")
            kinds = sorted({str(r.get("stopOrderType")) for r in legs})
            links = sorted({str(r.get("orderLinkId") or "''") for r in legs})
            self.record(
                "filled entry arms its attached TP/SL",
                len(legs) >= 2,
                f"{len(legs)} resting leg(s): {kinds}; orderLinkId {links}",
                "these carry no client id — PerpBroker.fetch_open_orders must keep "
                "treating them as the position's own protection (audit #80)",
            )
        finally:
            self.close_all()
            time.sleep(2.0)
            left = self.realtime(orderFilter="StopOrder") + self.realtime(orderFilter="tpslOrder")
            self.record(
                "closing the position removes its TP/SL legs",
                not left,
                f"{len(left)} leg(s) left after close"
                + (f": {[r.get('stopOrderType') for r in left]}" if left else ""),
                "" if not left else "leftovers must be cancelled by the engine on every exit",
            )
            for row in left:
                with contextlib.suppress(Exception):
                    self.cancel(str(row["orderId"]))

    def close_all(self) -> None:
        size = self.position_size()
        if size <= 0:
            return
        try:
            self.create(side="Sell", orderType="Market", qty=str(size), reduceOnly=True)
            self.record("close the probe position", True, f"reduce-only market sell {size}")
        except Exception as exc:  # noqa: BLE001
            self.record(
                "close the probe position",
                False,
                str(exc)[:200],
                f"{self.symbol} LEFT OPEN — close it by hand",
            )

    def check_wallet_fields(self) -> None:
        """Which number is 'the USDT this account owns'? Capital isolation needs one."""
        rows = self.v5("privateGetV5AccountWalletBalance", {"accountType": "UNIFIED"})["result"][
            "list"
        ]
        usdt = next((c for c in rows[0].get("coin", []) if c.get("coin") == "USDT"), {})
        fields = (
            "walletBalance",
            "equity",
            "unrealisedPnl",
            "totalPositionIM",
            "totalOrderIM",
            "locked",
            "availableToWithdraw",
        )
        self.record(
            "UTA USDT wallet fields",
            None,
            " · ".join(f"{k}={usdt.get(k) or '—'}" for k in fields),
            "walletBalance is cash plus realised PnL: it excludes unrealised PnL and "
            "includes margin posted for perp positions",
        )

    def check_maker_fee(self) -> None:
        spec = self.broker.market_spec(self.symbol)
        self.record(
            "maker / taker fee (market metadata)",
            None,
            f"maker {spec.maker_fee:.4%} · taker {spec.taker_fee:.4%}",
        )

    def check_kline_topics(self, symbols: list[str]) -> None:
        """Question 5: the one-minute kline topic ceiling on the linear public socket."""
        try:
            import ccxt.pro as ccxtpro
        except ImportError:
            self.record("linear WS kline topic ceiling", None, "ccxt.pro not importable")
            return

        async def trial(n: int) -> tuple[bool, float]:
            exchange = ccxtpro.bybit({"options": {"defaultType": "linear"}})
            started = time.monotonic()
            try:
                await asyncio.wait_for(
                    exchange.watch_ohlcv_for_symbols([[s, "1m"] for s in symbols[:n]]),
                    timeout=TOPIC_TIMEOUT_S,
                )
                return True, time.monotonic() - started
            except Exception:  # noqa: BLE001 — timeout is the answer
                return False, time.monotonic() - started
            finally:
                await exchange.close()

        results = []
        for n in KLINE_TOPIC_TRIALS:
            if n > len(symbols):
                continue
            ok, took = asyncio.run(trial(n))
            results.append(f"{n} topics: {'ok' if ok else 'HUNG'} in {took:.1f}s")
        hung = any("HUNG" in r for r in results)
        self.record(
            "linear WS kline topics per subscription",
            None,
            " · ".join(results),
            "chunk size for scalp/stream.py" + (" — a ceiling exists" if hung else ""),
        )


def perp_universes() -> set[str]:
    """Every symbol any linear engine on this checkout may trade."""
    taken: set[str] = set()
    for path in sorted(Path("config").glob("*.yaml")):
        try:
            cfg = load_config(str(path))
        except Exception:  # noqa: BLE001
            continue
        if cfg.execution.market_type != "linear":
            continue
        universe = cfg.runtime_dir / "universe.json"
        if universe.exists():
            taken |= set(json.loads(universe.read_text())["symbols"])
    return taken


def liquid_linear(broker: PerpBroker, count: int) -> list[str]:
    rows = broker.exchange.publicGetV5MarketTickers({"category": "linear"})["result"]["list"]
    rows = [r for r in rows if str(r.get("symbol", "")).endswith("USDT")]
    rows.sort(key=lambda r: float(r.get("turnover24h") or 0.0), reverse=True)
    return [r["symbol"][:-4] + "/USDT:USDT" for r in rows[:count]]


def render_markdown(probe: Probe) -> str:
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Ölçülmüş borsa gerçekleri — 1 dakikalık maker scalper",
        "",
        f"**Tarih:** {stamp} · **Sembol:** `{probe.symbol}` · **Hesap:** bybit-demo",
        "",
        "Gerçek emirlerle sınandı. `scripts/scalp_probe.py` ile yeniden üretilebilir.",
        "",
        "| deneme | sonuç |",
        "|---|---|",
    ]
    lines += [f.render() for f in probe.findings]
    notes = [f for f in probe.findings if f.detail]
    if notes:
        lines += ["", "## Notlar", ""]
        lines += [f"- **{f.question}** — {f.detail}" for f in notes]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/fast.yaml")
    parser.add_argument("--symbol", default="XPL/USDT:USDT")
    parser.add_argument("--write", default="research/scalp_venue_facts.md")
    parser.add_argument("--no-orders", action="store_true")
    args = parser.parse_args()

    cfg = setup(args.config)
    if cfg.venue is not Venue.BYBIT_DEMO:
        raise SystemExit(f"refusing to probe venue={cfg.venue.value}; bybit-demo only")
    key, secret = load_secrets().credentials_for(cfg.venue)
    if not (key and secret):
        raise SystemExit("no bybit-demo credentials in .env")

    broker = PerpBroker(key, secret, cfg.venue, cfg.quote_currency)
    broker.load_markets()
    probe = Probe(broker, args.symbol)

    if args.symbol in perp_universes():
        raise SystemExit(
            f"{args.symbol} belongs to a running perp engine's universe — probe orders "
            "there would reach its reconciliation. Pick a symbol outside it."
        )
    if probe.position_size() > 0:
        raise SystemExit(f"{args.symbol} already holds a position; refusing to touch it")

    print(f"\nProbing {args.symbol} on {cfg.venue.value} (category=linear)\n")
    probe.check_wallet_fields()
    probe.check_maker_fee()
    probe.check_kline_topics(liquid_linear(broker, max(KLINE_TOPIC_TRIALS)))
    if not args.no_orders:
        probe.check_resting_entry_with_protection()
        probe.check_crossing_post_only()
        probe.check_filled_entry_arms_protection()

    if args.write:
        path = Path(args.write)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_markdown(probe), encoding="utf-8")
        print(f"\nwrote {path}")
    failures = [f for f in probe.findings if f.ok is False]
    print(f"\n{len(probe.findings)} checks, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
