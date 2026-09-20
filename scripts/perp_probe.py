#!/usr/bin/env python3
"""Ask the venue what linear perpetuals actually allow, before designing on it.

    python scripts/perp_probe.py
    python scripts/perp_probe.py --symbol SOL/USDT:USDT --write research/perp_venue_facts.md

This repo has paid twice for trusting a capability flag. ``fetchTradingFees``
reported support and Bybit **spot** refused it. The whole exchange-side
take-profit design in plan A2 was built on a ccxt flag and died on first
contact: a resting stop locks the base asset, so the reduce-only take-profit
beside it comes back ``170131``. Both were discovered by placing one order.

So this places orders. Small ones, on the demo account, cleaned up at the end.
Everything it prints is something the venue said, not something a library
claimed on the venue's behalf.

**It never places a spot order.** The spot side of this account carries the
running engine's positions; this probe only ever addresses ``category=linear``.
It does not follow that spot is unaffected — the probe measures that too, and
the answer is no: on a Unified Trading Account the perp fee is deducted from
the same USDT the spot engine sizes against.

Refuses any venue but ``bybit-demo``, with no override flag — same contract as
``scripts/venue_reset.py``. A probe that places throwaway orders has no
legitimate live-money counterpart.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from _bootstrap import setup

from tbot.core.config import load_secrets
from tbot.core.models import Venue
from tbot.execution.ccxt_broker import CCXTBroker

# Small enough to be noise against a $1000 demo balance, large enough to clear
# the $5 notional floor every linear symbol enforces.
PROBE_NOTIONAL_USD = 12.0


@dataclass
class Finding:
    """One question put to the venue, and what came back."""

    question: str
    ok: bool | None  # None when the answer is informational rather than pass/fail
    answer: str
    detail: str = ""

    def render(self) -> str:
        mark = {True: "✅", False: "❌", None: "•"}[self.ok]
        line = f"| {self.question} | {mark} {self.answer} |"
        return line


@dataclass
class Probe:
    broker: CCXTBroker
    symbol: str
    findings: list[Finding] = field(default_factory=list)

    # -- helpers -----------------------------------------------------------

    def record(self, question: str, ok: bool | None, answer: str, detail: str = "") -> Finding:
        finding = Finding(question, ok, answer, detail)
        self.findings.append(finding)
        mark = {True: "OK  ", False: "FAIL", None: "--  "}[ok]
        print(f"  {mark} {question}\n       {answer}")
        if detail:
            print(f"       {detail}")
        return finding

    @property
    def bare(self) -> str:
        """``SOL/USDT:USDT`` -> ``SOLUSDT``, the form the v5 endpoints want."""
        return self.symbol.split(":")[0].replace("/", "")

    def _v5(self, method: str, params: dict) -> dict:
        return getattr(self.broker.exchange, method)(params)

    # -- the questions -----------------------------------------------------

    def check_auth(self) -> bool:
        """Does a private linear call authenticate at all on the demo host?"""
        try:
            response = self._v5(
                "privateGetV5PositionList", {"category": "linear", "symbol": self.bare}
            )
            rows = (response.get("result") or {}).get("list") or []
            self.record(
                "linear private auth on api-demo.bybit.com",
                True,
                f"position/list returned {len(rows)} row(s)",
            )
            return True
        except Exception as exc:  # noqa: BLE001 — the message is the finding
            self.record("linear private auth on api-demo.bybit.com", False, str(exc)[:200])
            return False

    def check_account_mode(self) -> None:
        """UTA or classic, and what that implies for collateral sharing."""
        try:
            info = (self._v5("privateGetV5AccountInfo", {}).get("result") or {})
        except Exception as exc:  # noqa: BLE001
            self.record("account type (UTA vs classic)", None, f"unreadable: {str(exc)[:120]}")
            return

        unified = str(info.get("unifiedMarginStatus", ""))
        margin_mode = str(info.get("marginMode", ""))
        # unifiedMarginStatus: 1 classic, 3/4 UTA1.0/2.0, 5/6 UTA pro
        is_uta = unified not in ("", "1")
        self.record(
            "account type",
            None,
            f"unifiedMarginStatus={unified} marginMode={margin_mode}"
            f" ({'UTA' if is_uta else 'classic'})",
        )

        # This is the safety-critical one. On a UTA account in cross mode the
        # spot balance backs the derivative position, so a liquidation here can
        # reach the other engine's capital. Isolated confines the loss to the
        # position's own margin.
        isolated = margin_mode == "ISOLATED_MARGIN"
        self.record(
            "account margin mode is ISOLATED_MARGIN",
            isolated,
            margin_mode or "unreported",
            "" if isolated else "cross margin lets a perp liquidation reach the spot balance",
        )

    def check_position_mode(self) -> None:
        """One-way or hedge. Decides whether orders need ``positionIdx``."""
        try:
            rows = (
                self._v5(
                    "privateGetV5PositionList", {"category": "linear", "symbol": self.bare}
                ).get("result")
                or {}
            ).get("list") or []
        except Exception as exc:  # noqa: BLE001
            self.record("position mode", None, f"unreadable: {str(exc)[:120]}")
            return
        if not rows:
            self.record("position mode", None, "no position row to read it from")
            return
        idx = rows[0].get("positionIdx")
        trade_mode = rows[0].get("tradeMode")
        self.record(
            "position mode",
            None,
            f"positionIdx={idx} ({'one-way' if str(idx) == '0' else 'hedge'})"
            f" tradeMode={trade_mode} ({'isolated' if str(trade_mode) == '1' else 'cross'})",
        )

    def check_leverage(self, leverage: float = 1.0) -> None:
        """Can the engine pin leverage, and is the ceiling what we think?"""
        try:
            self.broker.exchange.set_leverage(leverage, self.symbol)
            self.record(f"set_leverage({leverage:g})", True, "accepted")
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            # 110043 is "leverage not modified" — already at this value, which
            # is a pass, not a failure. Treating it as an error would make the
            # engine refuse to start on a correctly configured account.
            already = "110043" in message or "not modified" in message.lower()
            self.record(
                f"set_leverage({leverage:g})",
                bool(already),
                "already at this leverage (110043)" if already else message[:180],
            )

    def check_margin_mode_switch(self) -> None:
        """Can isolated be set per symbol, if the account is not already there?"""
        try:
            self.broker.exchange.set_margin_mode("isolated", self.symbol, {"leverage": 1})
            self.record("set_margin_mode('isolated', symbol)", True, "accepted")
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            already = "110026" in message or "not modified" in message.lower()
            self.record(
                "set_margin_mode('isolated', symbol)",
                True if already else None,
                "already isolated (110026)" if already else message[:180],
                "" if already else "may be account-level on UTA rather than per-symbol",
            )

    def check_fees(self) -> None:
        """The fee the account is actually charged, not the market default."""
        try:
            rows = (
                self._v5("privateGetV5AccountFeeRate", {"category": "linear", "symbol": self.bare})
                .get("result")
                or {}
            ).get("list") or []
        except Exception as exc:  # noqa: BLE001
            self.record("account fee tier (linear)", None, f"unreadable: {str(exc)[:120]}")
            return
        if not rows:
            self.record("account fee tier (linear)", None, "no row returned")
            return
        taker = float(rows[0].get("takerFeeRate", 0) or 0)
        maker = float(rows[0].get("makerFeeRate", 0) or 0)
        # Maker-in / taker-out is the realistic round trip: a stop is a market
        # order by nature, so the exit can never be a maker fill.
        realistic = maker + taker
        self.record(
            "account fee tier (linear)",
            None,
            f"taker {taker:.4%} maker {maker:.4%}"
            f" → round trip {2 * taker:.3%} taker-taker, {realistic:.3%} maker-in/taker-out",
        )

    def check_min_sizes(self, budget: float, risk_pct: float, stop_pct: float) -> None:
        """Which symbols a $budget account can express at a risk-correct size."""
        self.broker.load_markets()
        one_r = budget * risk_pct
        notional = one_r / stop_pct
        markets = self.broker.exchange.markets
        linear = [
            s
            for s, m in markets.items()
            if m.get("linear") and m.get("active") and s.endswith(":USDT")
        ]
        try:
            tickers = self.broker.exchange.fetch_tickers(linear[:400])
        except Exception:  # noqa: BLE001 — informational only
            tickers = {}

        expressible, blocked = 0, []
        for symbol in linear:
            market = markets[symbol]
            min_amount = float(market["limits"]["amount"]["min"] or 0)
            last = float((tickers.get(symbol) or {}).get("last") or 0)
            if last <= 0:
                continue
            floor = max(min_amount * last, float(market["limits"].get("cost", {}).get("min") or 0))
            if floor <= notional:
                expressible += 1
            else:
                blocked.append((symbol, floor))

        blocked.sort(key=lambda x: -x[1])
        total = expressible + len(blocked)
        self.record(
            f"${budget:g} @ {risk_pct:.1%} risk, {stop_pct:.1%} stop → ${notional:.0f} notional",
            None,
            f"{expressible}/{total} linear symbols expressible"
            f" ({expressible / total:.0%})" if total else "no symbols priced",
            "widest floors: "
            + ", ".join(f"{s.split('/')[0]} ${f:.0f}" for s, f in blocked[:5])
            if blocked
            else "",
        )

    def check_stop_and_target_together(self) -> None:
        """The question that reshaped the spot design. Does perp allow both?

        On spot the answer was no: a resting stop locks the base asset and the
        second reduce-only order is refused ``170131``. That is why the target
        lives in the engine (invariant 39). Perpetuals do not work by locking
        an asset, so this may come back differently — and if it does, the exit
        design improves materially, because the target survives the process
        being down.
        """
        spec = self.broker.market_spec(self.symbol)
        ticker = self.broker.exchange.fetch_ticker(self.symbol)
        last = float(ticker["last"])
        amount = self.broker.exchange.amount_to_precision(
            self.symbol, max(PROBE_NOTIONAL_USD / last, spec.min_amount)
        )

        opened = False
        try:
            self.broker.exchange.create_order(
                self.symbol, "market", "buy", float(amount), None, {"category": "linear"}
            )
            opened = True
            self.record(
                "open a probe position", True, f"market buy {amount} {self.symbol} @ ~{last:.4f}"
            )
        except Exception as exc:  # noqa: BLE001
            self.record("open a probe position", False, str(exc)[:200])
            return

        try:
            # Both legs in one call. Spot refused this outright with
            # "InvalidOrder: only supports contract" — which, read literally,
            # says contracts are exactly where it should work.
            try:
                self._v5(
                    "privatePostV5PositionTradingStop",
                    {
                        "category": "linear",
                        "symbol": self.bare,
                        "stopLoss": self.broker.exchange.price_to_precision(
                            self.symbol, last * 0.95
                        ),
                        "takeProfit": self.broker.exchange.price_to_precision(
                            self.symbol, last * 1.05
                        ),
                        "positionIdx": 0,
                    },
                )
                self.record(
                    "stop-loss AND take-profit on one position",
                    True,
                    "accepted — the venue can hold both",
                    "target can live at the venue, unlike spot (invariant 39)",
                )
            except Exception as exc:  # noqa: BLE001
                self.record("stop-loss AND take-profit on one position", False, str(exc)[:200])

            # Native trailing. Spot refused; contracts advertise support.
            try:
                self._v5(
                    "privatePostV5PositionTradingStop",
                    {
                        "category": "linear",
                        "symbol": self.bare,
                        "trailingStop": self.broker.exchange.price_to_precision(
                            self.symbol, last * 0.03
                        ),
                        "positionIdx": 0,
                    },
                )
                self.record("native trailing stop", True, "accepted")
            except Exception as exc:  # noqa: BLE001
                self.record("native trailing stop", False, str(exc)[:200])

            # What the position actually reports back — the ground truth for
            # tradeMode (isolated vs cross) with a real position open.
            try:
                rows = (
                    self._v5(
                        "privateGetV5PositionList", {"category": "linear", "symbol": self.bare}
                    ).get("result")
                    or {}
                ).get("list") or []
                if rows:
                    row = rows[0]
                    mode = "isolated" if str(row.get("tradeMode")) == "1" else "cross"
                    self.record(
                        "open position reports isolated margin",
                        mode == "isolated",
                        f"tradeMode={row.get('tradeMode')} ({mode})"
                        f" leverage={row.get('leverage')}"
                        f" liqPrice={row.get('liqPrice') or 'none'}",
                        "UTA reports cross regardless of the account flag. The"
                        " protection that actually binds is leverage: at 1.0 the"
                        " position is fully collateralised (see the short"
                        " liquidation check), so isolation stops being the"
                        " load-bearing control.",
                    )
            except Exception as exc:  # noqa: BLE001
                self.record("open position reports isolated margin", None, str(exc)[:160])
        finally:
            if opened:
                self._close(float(amount))

    def _close(self, amount: float) -> None:
        """Always leave the account as it was found."""
        try:
            self.broker.exchange.create_order(
                self.symbol,
                "market",
                "sell",
                amount,
                None,
                {"category": "linear", "reduceOnly": True},
            )
            self.record("close the probe position", True, "reduce-only market sell filled")
        except Exception as exc:  # noqa: BLE001
            self.record(
                "close the probe position",
                False,
                str(exc)[:200],
                "LEFT OPEN — close it by hand before running the engine",
            )

    def check_wallet_sharing(self, before: dict[str, float]) -> None:
        """Does a perpetual trade move the same balance the spot engine uses?

        The hoped-for answer was no — that spot balances and derivative margin
        are separate ledgers, which would have made engine isolation free. The
        measured answer is yes: on a Unified Trading Account the perp fee comes
        out of the very USDT the spot engine sizes against. Isolation has to be
        built rather than assumed, and the hard budget cap is load-bearing.
        """
        after = self._spot_balances()
        moved = {
            coin: round(value - before.get(coin, 0.0), 8)
            for coin, value in after.items()
            if abs(value - before.get(coin, 0.0)) > 1e-9
        }
        self.record(
            "does a perp trade move the spot-side balance",
            None,
            f"YES, shared wallet: {moved}" if moved else "no movement observed",
            "UTA pools spot and derivatives; two engines draw on one balance"
            if moved
            else "",
        )

    def check_account_margin_switch(self) -> None:
        """Account-level isolated margin: accepted, but is it effective?

        ``set_margin_mode`` on the symbol returns success and leaves
        ``tradeMode`` at 0. That is the ``fetchTradingFees`` failure mode all
        over again — accepted is not the same as effective — so the account
        endpoint is asked separately and the position is re-read afterwards.
        """
        try:
            response = self._v5("privatePostV5AccountSetMarginMode", {
                "setMarginMode": "ISOLATED_MARGIN"
            })
            self.record(
                "account-level setMarginMode(ISOLATED_MARGIN)",
                None,
                f"retCode={response.get('retCode')} {response.get('retMsg')}",
                "accepted at the account level; per-position tradeMode is the real test",
            )
        except Exception as exc:  # noqa: BLE001
            self.record("account-level setMarginMode(ISOLATED_MARGIN)", None, str(exc)[:180])

    def check_short_liquidation(self) -> None:
        """How far away is liquidation on a 1x short? This is the safety number.

        A 1x long cannot really be liquidated — the loss is bounded by the
        notional. A short is not symmetric: price has no ceiling, so the
        liquidation distance is what actually bounds the damage, and it is the
        number that decides whether leverage above 1.0 is safe to use.
        """
        spec = self.broker.market_spec(self.symbol)
        last = float(self.broker.exchange.fetch_ticker(self.symbol)["last"])
        amount = self.broker.exchange.amount_to_precision(
            self.symbol, max(PROBE_NOTIONAL_USD / last, spec.min_amount)
        )
        try:
            self.broker.exchange.create_order(
                self.symbol, "market", "sell", float(amount), None, {"category": "linear"}
            )
        except Exception as exc:  # noqa: BLE001
            self.record("1x short liquidation distance", False, str(exc)[:200])
            return

        try:
            rows = (
                self._v5(
                    "privateGetV5PositionList", {"category": "linear", "symbol": self.bare}
                ).get("result")
                or {}
            ).get("list") or []
            row = next((r for r in rows if float(r.get("size") or 0) > 0), None)
            if row is None:
                self.record("1x short liquidation distance", None, "no position row came back")
                return
            entry = float(row.get("avgPrice") or 0)
            liq = row.get("liqPrice")
            margin = float(row.get("positionIM") or 0)
            notional = entry * float(row.get("size") or 0)
            if liq and str(liq) not in ("", "0") and entry > 0:
                distance = float(liq) / entry - 1.0
                self.record(
                    "1x short liquidation distance",
                    True,
                    f"entry {entry:g} liq {liq} → {distance:+.1%} away",
                    f"positionIM {margin:.2f} vs notional {notional:.2f}"
                    f" ({margin / notional:.0%} collateralised);"
                    f" at 3x the same short liquidates near {distance / 3:+.0%}",
                )
            else:
                self.record(
                    "1x short liquidation distance", None, f"liqPrice unreported ({liq!r})"
                )
        finally:
            try:
                self.broker.exchange.create_order(
                    self.symbol,
                    "market",
                    "buy",
                    float(amount),
                    None,
                    {"category": "linear", "reduceOnly": True},
                )
                self.record("close the short probe", True, "reduce-only market buy filled")
            except Exception as exc:  # noqa: BLE001
                self.record(
                    "close the short probe",
                    False,
                    str(exc)[:200],
                    "LEFT OPEN — close it by hand before running the engine",
                )

    def _spot_balances(self) -> dict[str, float]:
        try:
            raw = self.broker.exchange.fetch_balance({"type": "spot"})
            return {k: float(v) for k, v in (raw.get("total") or {}).items() if v}
        except Exception:  # noqa: BLE001
            return {}


def render_markdown(probe: Probe, symbol: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Ölçülmüş borsa gerçekleri — linear perpetual",
        "",
        f"**Tarih:** {stamp} · **Sembol:** `{symbol}` · **Hesap:** bybit-demo",
        "",
        "Varsayım değil, ccxt bayrağı değil — gerçek emirlerle sınandı.",
        "`scripts/perp_probe.py` ile yeniden üretilebilir.",
        "",
        "| deneme | sonuç |",
        "|---|---|",
    ]
    lines.extend(f.render() for f in probe.findings)
    lines.append("")
    details = [f for f in probe.findings if f.detail]
    if details:
        lines.append("## Notlar")
        lines.append("")
        lines.extend(f"- **{f.question}** — {f.detail}" for f in details)
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/demo.yaml")
    parser.add_argument("--symbol", default="SOL/USDT:USDT", help="linear perp to probe")
    parser.add_argument("--budget", type=float, default=300.0)
    parser.add_argument("--risk", type=float, default=0.012)
    parser.add_argument("--stop", type=float, default=0.026, help="stop distance as a fraction")
    parser.add_argument("--write", default="research/perp_venue_facts.md")
    parser.add_argument("--no-orders", action="store_true", help="read-only checks only")
    args = parser.parse_args()

    cfg = setup(args.config)
    if cfg.venue is not Venue.BYBIT_DEMO:
        raise SystemExit(
            f"refusing to probe venue={cfg.venue.value}. This script places throwaway "
            "orders and only ever runs against bybit-demo."
        )

    secrets = load_secrets()
    key, secret = secrets.credentials_for(cfg.venue)
    if not (key and secret):
        raise SystemExit("no bybit-demo credentials in .env")

    broker = CCXTBroker(key, secret, cfg.venue, cfg.quote_currency, default_type="linear")
    probe = Probe(broker, args.symbol)

    print(f"\nProbing {args.symbol} on {cfg.venue.value} (category=linear)\n")
    spot_before = probe._spot_balances()

    if not probe.check_auth():
        print("\nauthentication failed — nothing further can be measured")
        return 1

    probe.check_account_mode()
    probe.check_position_mode()
    probe.check_margin_mode_switch()
    probe.check_account_margin_switch()
    probe.check_leverage(1.0)
    probe.check_fees()
    probe.check_min_sizes(args.budget, args.risk, args.stop)

    if args.no_orders:
        print("\n--no-orders: skipping the order-placing checks")
    else:
        probe.check_stop_and_target_together()
        probe.check_short_liquidation()
        probe.check_wallet_sharing(spot_before)

    if args.write:
        path = Path(args.write)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_markdown(probe, args.symbol), encoding="utf-8")
        print(f"\nwrote {path}")

    failures = [f for f in probe.findings if f.ok is False]
    print(f"\n{len(probe.findings)} checks, {len(failures)} failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
