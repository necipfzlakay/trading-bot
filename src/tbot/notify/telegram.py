"""Telegram notifications and manual controls.

An information channel, not a control loop. The engine never waits for a reply —
the operator's requirement was that the bot approves and rejects on its own, so
commands here are conveniences (pause, flatten, status) rather than gates.

Delivery failures are swallowed by design. A notification service being down is
not a reason to stop trading, and a bot that dies because Telegram is
unreachable would be a worse outcome than one that trades quietly.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from tbot.core.logging import get_logger
from tbot.core.models import Trade

log = get_logger("notify.telegram")

API = "https://api.telegram.org/bot{token}/{method}"


@dataclass(slots=True)
class TelegramNotifier:
    token: str
    chat_id: str
    enabled: bool = True
    timeout: float = 5.0
    # Polling sits on the engine's wait loop, which also watches open
    # positions. It gets a shorter leash than sending does.
    poll_timeout: float = 3.0
    _offset: int = 0

    def send(self, text: str, silent: bool = False) -> bool:
        """Post a message. Never raises — a failed send is logged and dropped."""
        if not self.enabled or not self.token or not self.chat_id:
            return False
        try:
            response = httpx.post(
                API.format(token=self.token, method="sendMessage"),
                json={
                    "chat_id": self.chat_id,
                    "text": text[:4000],
                    "parse_mode": "HTML",
                    "disable_notification": silent,
                },
                timeout=self.timeout,
            )
            return response.status_code == 200
        except httpx.HTTPError as exc:
            log.warning("telegram_send_failed", error=str(exc)[:160])
            return False

    # -- event helpers -----------------------------------------------------

    def notify_entry(
        self,
        symbol: str,
        side: str,
        price: float,
        quantity: float,
        stop: float,
        conviction: float,
        setup: str,
    ) -> None:
        self.send(
            f"🟢 <b>OPEN {symbol}</b>\n"
            f"{side.upper()} {quantity:.6f} @ {price:.6f}\n"
            f"stop {stop:.6f} · conviction {conviction:.0f}\n"
            f"setup: {setup}"
        )

    def notify_exit(self, trade: Trade) -> None:
        icon = "✅" if trade.is_win else "🔴"
        self.send(
            f"{icon} <b>CLOSE {trade.symbol}</b>\n"
            f"{trade.r_multiple:+.2f}R · {trade.pnl:+.4f} {trade.symbol.split('/')[1]}\n"
            f"held {trade.bars_held} bars · {trade.exit_reason.value}\n"
            f"MFE {trade.mfe_r:+.2f}R · MAE {trade.mae_r:+.2f}R"
        )

    def notify_risk_change(self, drawdown: float, multiplier: float, bump: float) -> None:
        self.send(
            f"⚠️ <b>Risk tier changed</b>\n"
            f"drawdown {drawdown:.1%}\n"
            f"size multiplier → {multiplier:.2f}×\n"
            f"conviction bar → +{bump:.0f}\n"
            f"<i>Still trading, smaller and pickier.</i>"
        )

    def notify_degraded(self, reason: str) -> None:
        self.send(
            f"🟡 <b>Degraded mode</b>\n{reason}\n"
            f"<i>Existing positions still managed; no new entries. "
            f"Recovers on its own.</i>"
        )

    def notify_error(self, kind: str, message: str) -> None:
        self.send(f"❌ <b>{kind}</b>\n<code>{message[:600]}</code>")

    def notify_startup(self, mode: str, venue: str, equity: float, symbols: int) -> None:
        self.send(
            f"🤖 <b>Bot started</b>\n"
            f"mode {mode} · venue {venue}\n"
            f"equity {equity:.2f} · scanning {symbols} symbols",
            silent=True,
        )

    def daily_summary(self, stats: dict) -> None:
        self.send(
            f"📊 <b>Daily summary</b>\n"
            f"equity {stats.get('equity', 0):.2f} "
            f"({stats.get('total_return', 0):+.2%})\n"
            f"drawdown {stats.get('drawdown', 0):.1%} · "
            f"risk {stats.get('risk_multiplier', 1):.2f}×\n"
            f"trades {stats.get('trades', 0)} · "
            f"expectancy {stats.get('expectancy_r', 0):+.3f}R\n"
            f"open {stats.get('open_positions', 0)} · "
            f"scanned {stats.get('scanned', 0)}",
            silent=True,
        )

    # -- commands ----------------------------------------------------------

    def poll_commands(self) -> list[str]:
        """Read new commands. Non-blocking; failures return nothing.

        Commands are conveniences. The engine polls when it happens to be
        between bars and carries on regardless of what it finds.
        """
        if not self.enabled or not self.token:
            return []
        try:
            response = httpx.get(
                API.format(token=self.token, method="getUpdates"),
                params={"offset": self._offset, "timeout": 0},
                timeout=self.poll_timeout,
            )
            if response.status_code != 200:
                return []
            updates = response.json().get("result", [])
        except (httpx.HTTPError, ValueError) as exc:
            log.debug("telegram_poll_failed", error=str(exc)[:120])
            return []

        commands = []
        for update in updates:
            self._offset = max(self._offset, int(update["update_id"]) + 1)
            message = update.get("message") or {}
            if str(message.get("chat", {}).get("id")) != str(self.chat_id):
                continue  # ignore anyone who is not the operator
            text = (message.get("text") or "").strip()
            if text.startswith("/"):
                commands.append(text.split()[0].lstrip("/").lower())
        return commands
