# Ölçülmüş borsa gerçekleri — 1 dakikalık maker scalper

**Tarih:** 2026-09-21 09:31 UTC · **Sembol:** `XPL/USDT:USDT` · **Hesap:** bybit-demo

Gerçek emirlerle sınandı. `scripts/scalp_probe.py` ile yeniden üretilebilir.

| deneme | sonuç |
|---|---|
| UTA USDT wallet fields | • walletBalance=316.12965963 · equity=316.12965963 · unrealisedPnl=0 · totalPositionIM=0 · totalOrderIM=0 · locked=0 · availableToWithdraw=— |
| maker / taker fee (market metadata) | • maker 0.0100% · taker 0.0600% |
| linear WS kline topics per subscription | • 10 topics: ok in 2.2s · 11 topics: ok in 2.5s · 20 topics: ok in 2.2s |
| PostOnly limit + attached SL(market) + TP(limit) in one call | ✅ status=New tif=PostOnly SL=0.09448/None TP=0.09504/None tpLimit=0.09504 mode=Partial |
| cancel a resting PostOnly entry, confirmed from history | ✅ history status=Cancelled reason=CancelByUser |
| PostOnly that would cross the book | ✅ accepted, then status=None reason=None filled=0.0 |
| filled entry arms its attached TP/SL | ✅ 2 resting leg(s): ['PartialStopLoss', 'PartialTakeProfit']; orderLinkId ["''"] |
| close the probe position | ✅ reduce-only market sell 81.0 |
| closing the position removes its TP/SL legs | ✅ 0 leg(s) left after close |

## Notlar

- **UTA USDT wallet fields** — walletBalance is cash plus realised PnL: it excludes unrealised PnL and includes margin posted for perp positions
- **linear WS kline topics per subscription** — chunk size for scalp/stream.py
- **PostOnly limit + attached SL(market) + TP(limit) in one call** — the entry carries its own protection; the venue arms it on fill
- **PostOnly that would cross the book** — the engine must read this as 'no fill', and release its claim
- **filled entry arms its attached TP/SL** — these carry no client id — PerpBroker.fetch_open_orders must keep treating them as the position's own protection (audit #80)
