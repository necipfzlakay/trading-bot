# Ölçülmüş borsa gerçekleri — linear perpetual

**Tarih:** 2026-09-19 22:17 UTC · **Sembol:** `SOL/USDT:USDT` · **Hesap:** bybit-demo

Varsayım değil, ccxt bayrağı değil — gerçek emirlerle sınandı.
`scripts/perp_probe.py` ile yeniden üretilebilir.

| deneme | sonuç |
|---|---|
| linear private auth on api-demo.bybit.com | ✅ position/list returned 1 row(s) |
| account type | • unifiedMarginStatus=3 marginMode=ISOLATED_MARGIN (UTA) |
| account margin mode is ISOLATED_MARGIN | ✅ ISOLATED_MARGIN |
| position mode | • positionIdx=0 (one-way) tradeMode=0 (cross) |
| set_margin_mode('isolated', symbol) | ✅ accepted |
| account-level setMarginMode(ISOLATED_MARGIN) | • retCode=0 Request accepted |
| set_leverage(1) | ✅ already at this leverage (110043) |
| account fee tier (linear) | • unreadable: bybit {"retCode":10001,"retMsg":"","result":{},"retExtInfo":{},"time":1789856242989} |
| $300 @ 1.2% risk, 2.6% stop → $138 notional | • 400/400 linear symbols expressible (100%) |
| open a probe position | ✅ market buy 0.1 SOL/USDT:USDT @ ~110.2700 |
| stop-loss AND take-profit on one position | ✅ accepted — the venue can hold both |
| native trailing stop | ✅ accepted |
| open position reports isolated margin | ❌ tradeMode=0 (cross) leverage=1 liqPrice=none |
| close the probe position | ✅ reduce-only market sell filled |
| 1x short liquidation distance | ✅ entry 110.27 liq 219.44 → +99.0% away |
| close the short probe | ✅ reduce-only market buy filled |
| does a perp trade move the spot-side balance | • YES, shared wallet: {'USDT': -0.0242594} |

## Notlar

- **account-level setMarginMode(ISOLATED_MARGIN)** — accepted at the account level; per-position tradeMode is the real test
- **stop-loss AND take-profit on one position** — target can live at the venue, unlike spot (invariant 39)
- **1x short liquidation distance** — positionIM 11.04 vs notional 11.03 (100% collateralised); at 3x the same short liquidates near +33%
- **does a perp trade move the spot-side balance** — UTA pools spot and derivatives; two engines draw on one balance
