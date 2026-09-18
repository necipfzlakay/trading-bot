# TRADING-BOT — proje haritası

Otonom kripto al-sat botu. Bybit spot, önce paper/demo, sonra gerçek para ($100).
Plan: `~/.claude/plans/simdi-benim-aklimda-bir-ticklish-micali.md` · Kurulum: `SETUP.md`

```bash
.venv/bin/python -m pytest -q             # 319 test
.venv/bin/ruff check src tests scripts    # lint
```

## Değişmez kurallar

Tercih değil, sistemin doğruluğunu taşıyan sözleşmeler. Değiştirmeden önce ilgili
testi oku — hepsinin kendini koruyan bir testi var.

1. **Sadece kapanmış bar.** Sinyal bar *t* kapanışında, emir *t+1* açılışında.
   Oluşmakta olan bar diske bile yazılmaz (`ParquetStore._normalise_ohlcv`).
   → `test_indicators.py::test_indicator_cannot_see_the_future` (33 indikatör)
2. **Zaman dilimi hizalaması kapanışa göre.** Bir 4h barı, ancak *kapandıktan*
   sonra bir 1h barına görünür. İçerme mantığı 4 saat geleceği okur.
   → `backtest/align.py` · `test_backtest_core.py::test_fast_bar_only_sees_slow_bars_that_have_already_closed`
3. **Bloklar oy verir, indikatörler değil.** |ρ|≥0.8 olanlar tek bloğa düşer.
   → `strategy/conviction.py::_count_blocks`
4. **Bot zararda durmaz, küçülür.** `risk_multiplier` sıfır olamaz; config
   validasyonu böyle bir merdiveni reddeder. → `risk/budget.py`
5. **Stop tetiklendiği yerde dolar**, sonraki açılışta değil. Aksi halde zarar
   olduğundan küçük görünür. → `paper_broker.force_fill_at`
6. **Emirler idempotent.** `client_id = hash(strateji, sembol, bar, aksiyon)`.
   Gönderim ile kayıt arasındaki çökme pozisyonu ikiye katlayamaz.
7. **LLM riski yalnız azaltabilir.** Sinyal üretemez, boyut belirleyemez, stop
   taşıyamaz. Yanıt şeması bunu temsil edilemez kılıyor. → `test_llm_safety.py`
8. **Kaldıraç kapısı.** Kod 1.0'ın üstüne kendiliğinden çıkamaz; kapı geçilse
   bile kullanıcı tavanını aşamaz. → `risk/leverage.py`
9. **Gerçek para ek onay ister.** `TBOT_CONFIRM_LIVE=yes` olmadan başlamaz.
10. **NaN = "fikrim yok", 0 değil.** Isınma nötr oy olarak sayılmaz.
11. **Paper emirleri sonraki barın açılışında dolar**, gönderildiğinde değil.
    Aksi halde paper backtest ile aynı yolu izlemez. → `_drain_paper_fills`
12. **Canlıda stop borsada da durur.** Süreç kapalıyken bellekteki stop hiçbir
    şeyi korumaz. → `_place_protective_stop`
13. **Çekim yetkili API anahtarı reddedilir.** Bot buna hiç ihtiyaç duymaz;
    varlığı sadece risk. → `execution/safety.py`
14. **Tek instance.** İki motor aynı hesapta = her pozisyon iki kez.
    → `engine/lock.py` (flock, çökmede kernel serbest bırakır)
15. **Açık pozisyonun sembolü evrenden düşse bile yönetilir.** Aksi halde stop
    ilerlemez ve çıkış hiç tetiklenmez. → `_refresh_data`
16. **Bar eşikleri zaman dilimine göre.** 3 yıllık haftalık veri tanım gereği
    156 bardır; sabit 200 eşiği her haftalık seriyi reddeder.
17. **Haftalık barlar Pazartesi'ye hizalı**, epoch'a (Perşembe) değil.
    → `clock.timeframe_offset`
18. **Minimum bakiye yapısal.** `risk.min_equity` (ölçülen: 183) altında yeni
    giriş yok. Altında bot test edilen stratejiyi küçültmez, evrenin daha sakin
    bir alt kümesini işler. → `scripts/capital_check.py`
19. **`starting_balance` sadece paper/backtest.** Canlıda bakiye borsadan okunur.
20. **Pozisyon tavanları sadece kısar.** `max_position_notional` / `_pct` /
    `max_risk_amount` — hangisi önce bağlarsa. Tavan bağlasa bile
    `1R = adet × stop_mesafesi` korunur. → `risk/sizing.py`
21. **`SizingResult` keyword-only.** Alan eklemek pozisyonel argümanı sessizce
    yanlış alana bağlamasın diye — bir kez oldu, red sebebi `capped_by`'a düştü.

## Denetim (2026-09-17) — bulunan ve kapatılan

Sistemi baştan sona tarayıp uç durumları test ettim. Bulunanlar:

| # | sorun | ciddiyet | durum |
|---|---|---|---|
| 1 | Paper modda emirler **hiç dolmuyordu** — `process_bar` çağrılmıyordu, `average_price=0`, bakiye değişmiyordu | kritik | düzeltildi |
| 2 | Canlıda **borsa tarafı stop yoktu** — süreç kapalıyken pozisyon korumasız | kritik | düzeltildi |
| 3 | Panel kontrolleri ölüydü (`command.json` yazılıp hiç okunmuyordu) | yüksek | düzeltildi |
| 4 | Komutlar sadece bar kapanışında okunuyordu → 1h barda acil `flatten` 1 saat bekler | yüksek | düzeltildi (~10s) |
| 5 | `counterfactual_r` hiç doldurulmuyordu → `llm_impact.py` event_guard hakkında karar veremezdi | orta | düzeltildi |
| 6 | 5 LLM rolünden 3'ü hiç çağrılmıyordu (post_mortem, report_writer, config_auditor) | orta | düzeltildi |
| 7 | `notify_risk_change` / `notify_degraded` / `daily_summary` hiç çağrılmıyordu | orta | düzeltildi |
| 8 | Çekim yetkili API anahtarı kontrolü yoktu | orta | eklendi |
| 9 | Kayma (slippage) varsayımı hiç ölçülmüyordu | orta | eklendi |
| 10 | Bozuk veriden gelen imkânsız equity sıçraması boyutlandırmaya giriyordu | orta | eklendi |
| 11 | Mac uykusu botu durduruyordu (kripto 7/24) | orta | `caffeinate -i` plist'e eklendi |
| 12 | Ölü kod (`Portfolio.is_full`) | düşük | kaldırıldı |

### İkinci tur (aynı gün)

| # | sorun | ciddiyet | durum |
|---|---|---|---|
| 13 | **Süreç kilidi yoktu** — launchd + elle çalıştırma = aynı hesapta iki bot | kritik | `engine/lock.py` |
| 14 | **Evrenden düşen sembolde açık pozisyon sonsuza kadar yönetilmiyordu** — stop ilerlemez, çıkış tetiklenmez | kritik | düzeltildi |
| 15 | Kısmi dolum (partial fill) işlenmiyordu — bot sahip olmadığı miktarı satmaya çalışırdı | yüksek | düzeltildi |
| 16 | **Haftalık barlar yanlış hizalanıyordu** (epoch Perşembe, borsa Pazartesi) — tüm 1w backfill'i "bozuk" diye reddedildi | yüksek | düzeltildi |
| 17 | Sabit bar eşikleri (200/220/250/300) haftalık veriyi imkânsız kılıyordu | yüksek | zaman dilimi tablosuna bağlandı |
| 18 | `funding` contrarian varsayılıyordu — ölçüm 5/5 ufukta momentum dedi | orta | registry düzeltildi, `scripts/polarity.py` eklendi |
| 19 | `_veto_prices` sınırsız büyüyordu | düşük | 2000 ile sınırlandı |

### Eklenen özellikler

`engine/lock.py` tek instance · `risk/capital.py` hesap büyüklüğü teşhisi ·
`execution/safety.py` anahtar izni + kayma ölçümü + imkânsız equity koruması ·
`scripts/healthcheck.py` dead-man + DB yedeği · `scripts/divergence.py` Kapı 2 ·
`scripts/plateau.py` parametre platosu · `scripts/polarity.py` türev sinyal yönü ·
sermaye dondurma zemini (%50) · emir reddi sayacı (3 red → sembol düşer) ·
log rotasyonu · `caffeinate -i` · `config/slow.yaml` · README.md

Performans ölçüldü, sorun değil: 21 sembol tam tarama 6.5s + 63 REST çağrısı 8s,
1h bar için fazlasıyla yeterli. 1m barda yeniden ölçülmeli.

## Dizin haritası

| Yol | İçerik |
|---|---|
| `core/models.py` | Veri sözleşmesi. R-birimi, MAE/MFE, `NoTradeReason` |
| `core/config.py` | pydantic doğrulama. Geçersiz config = açılışta hata |
| `core/clock.py` | UTC ms, bar sınırları |
| `core/db.py` | SQLite. Bybit demo 7 gün tutar; asıl arşiv bu |
| `data/store.py` `backfill.py` `integrity.py` | Parquet arşiv, resumable çekim, kalite kapısı |
| `indicators/ta.py` | 22 indikatör, dış TA kütüphanesi yok |
| `indicators/registry.py` | Ham → [-1,+1]. DIRECTIONAL (oy) vs GATE (kapı) |
| `indicators/ic.py` | IC **+ tail analizi**. Hangi indikatörün yaşayacağına karar verir |
| `indicators/redundancy.py` | Korelasyon blokları |
| `strategy/regime.py` | İzin katmanı |
| `strategy/setups.py` | Giriş tetikleyicileri + stop yerleşimi |
| `strategy/conviction.py` | Seçicilik motoru, 0-100, dinamik eşik |
| `strategy/exits.py` | 4 katmanlı çıkış, sabit süre yok |
| `risk/budget.py` `sizing.py` `leverage.py` | Sürekli merdiven, stop'tan boyut, promosyon kapısı |
| `execution/broker.py` `paper_broker.py` `ccxt_broker.py` | Tek arayüz, üç uygulama |
| `execution/reconcile.py` | Açılışta mutabakat; anlaşmazlıkta borsa kazanır |
| `engine/runner.py` | Canlı döngü |
| `engine/state.py` `watchdog.py` | Atomik snapshot, devre kesici, degraded mode |
| `backtest/engine.py` `align.py` `metrics.py` | Replay, hizalama, metrikler |
| `backtest/cpcv.py` `dsr.py` `montecarlo.py` `excursion.py` | Kapı 1 aletleri |
| `llm/provider.py` `roles.py` `redact.py` | 5 rol, bütçe, whitelist redaksiyon |
| `web/app.py` | Yerel panel (localhost) |

## Komutlar

```bash
python scripts/backfill.py --years 3 --with-derivatives   # veri (resumable)
python scripts/ic_report.py --timeframe 1d                # indikatör ölçümü
python scripts/backtest.py                                # backtest + rapor
python scripts/cpcv_report.py --trials N                  # KAPI 1 kararı
python scripts/exit_optimizer.py                          # MAE/MFE kalibrasyon
python scripts/run.py --config config/paper.yaml          # canlı motor
python scripts/dashboard.py                               # panel :8787
python scripts/report.py --since 30d                      # durum raporu
python scripts/llm_impact.py                              # LLM karşı-olgusal
```

## Gözlem (Claude buradan bakar)

`runtime/state.json` (anlık durum, atomik yazılır) · `runtime/logs/*.jsonl`
(her satırda `event`/`symbol`/`bar_ts`/`reason`) · `runtime/tbot.db`
(`scans` tablosu **her barda neden işlem yapılmadığını** tutar) ·
`runtime/reports/*.md`

## Ölçülmüş bulgular (2026-09-17, 21 sembol × 3 yıl, 620k bar)

Varsayım değil, bu repodaki veriyle ölçüldü.

- **7 indikatör tek bloğa düştü** (4h, maks |ρ|=0.955): rsi, macd, stoch_rsi,
  roc, bb_percent_b, donchian_pos, vwap_distance. 18 yönlü → 12 bağımsız blok.
- **IC ile tail analizi çelişiyor, tail haklı.** Tüm barlarda IC ≈ −0.02, hiçbir
  t-istatistiği 2'yi geçmiyor. Ama üst desilde güçlü momentum var — ilişki
  monotonik değil. Bot ortada işlem yapmadığı için karar ölçütü tail.
- **Uzun ufuk >> kısa ufuk.** 1d, 20 bar: `price_slope` üst desil fazlalığı
  +%5.97, blok-bootstrap CI [+%4.58, +%12.44]. 4h 12 barda ~%0.8.
- **TREND_UP'ta etki büyüyor** (rsi h=20: +%9.34 fazlalık, %65 isabet).
- **Çıkış zaman dilimi tutuş süresini belirliyor.** Trailing ATR'yi trigger'dan
  (1h) hesaplamak çok haftalık pozisyonu saatler içinde stop'luyordu; 23 işlemin
  hepsi trailing stop'la kapanmıştı. `exits.trail_timeframe: permission` ile
  medyan tutuş 1.5 gün → 3 gün, maks 26 gün.
- **Funding contrarian DEĞİL.** Yaygın kabulün tersi: 5/5 ufukta yüksek funding
  daha *yüksek* getiri öngördü. 1d/20 bar: üst desil +%5.31 vs taban +%2.28,
  CI [+%2.49, +%7.70]. `long_short` ise gerçekten contrarian (5/5).
  `scripts/polarity.py` ile tekrar ölçülebilir — polarite sabit değil.
- **Sermaye kısıtı evreni sessizce değiştiriyor.** $100'da risk-doğru boyut ancak
  stop **%13'ün altındaysa** borsanın $5 minimumunu geçiyor. Volatil semboller
  hiç işleme girmiyor, yani canlı evren backtest evreninden farklı. $300 bu
  sınırı %39'a çıkarıyor. Artık açılışta raporlanıyor (`risk/capital.py`).
- **Uyarı:** örneklem 2023-09..2026-09, taban oran pozitif. Ham getiri değil
  **taban üstü fazlalık** ölçülüyor; CPCV + DSR hâlâ zorunlu.

## Zaman dilimi ekonomisi (ölçüldü)

Gidiş-dönüş maliyet ~%0.3. Tipik bar hareketi ne kadarını yiyor:

| tf | medyan hareket | maliyet / hareket |
|---|---|---|
| 15m | %0.24 | **%124** — maliyet hareketten büyük |
| 30m | %0.34 | %88 |
| 1h | %0.48 | %62 |
| 4h | %0.94 | %32 |
| 1d | %2.58 | %12 |

Düşük zaman diliminde strateji sorunu yok, **aritmetik** sorunu var. Ama maliyet
bar başına değil işlem başına ödeniyor: düşük tf `trigger` olarak bedava giriş
hassasiyeti verir. Belirleyici olan zaman dilimi etiketi değil **tutuş ufku**.

## Kapı 1 durumu: GEÇİLMEDİ

Strateji v1 ölçüldü (`runtime/reports/cpcv.md`):

| kontrol | değer | |
|---|---|---|
| işlem ≥ 50 | 18 | ❌ |
| profit factor ≥ 1.2 | 1.00 | ❌ |
| maks düşüş ≤ %20 | %5.0 | ✅ |
| DSR anlamlı | p=0.508 | ❌ |
| CPCV yollarının çoğu pozitif | %20 | ❌ |
| PBO ≤ 0.5 | 1.00 | ❌ |

### İki yapısal bulgu (v1 → v2)

**1. $100'da ölçülen şey strateji değildi.** Huni ölçümü: taramayı 1.246 aday
geçiyor, **1.215'i (%97.5) `min_notional`'a takılıyor.** Borsanın $5 minimumu,
$0.75 risk bütçesiyle ancak stop %13'ten darsa aşılıyor. Yani sadece dar stop'lu
kurulumlar işleme giriyordu — strateji değil, bir sermaye kısıtı ölçülmüştü.

**1b. Minimum sermaye ölçüldü: $183.** Boyutlandırma zinciri
`1R = bakiye × risk × vol_ölçeği`, `notional = 1R / stop%`, borsa $5 altını
reddediyor. Ters çevirince: hesap ancak `1R/$5.75` kadar geniş stop ifade
edebiliyor. $100'da bu %13 — kurulumların %84'ü geçiyor, %16'sı (volatil olanlar)
sessizce atlanıyor. Volatilite ölçekleyici de aynı sembolleri küçülttüğü için iki
etki birleşiyor. `risk.min_equity` olarak zorlanıyor.

**2. Ölçtüğüm şeyi uygulamamışım.** Tail analizi **kesitsel**: her barda
semboller birbirine karşı sıralanıp üst desil alınıyor. Conviction motoru ise
her sembolü **mutlak bir eşiğe** karşı puanlıyordu. Farklı sorular. Mutlak eşik
kıtlık-bolluk üretiyor; göreli sıralama fırsat sayısını sabit tutuyor.
Düzeltince: 63→116 işlem, PF 0.97→1.34, expectancy −0.004→+0.280R.
`conviction.selection: cross_sectional` artık varsayılan.

Al-tut karşılaştırması: strateji **+%0.95**, BTC al-tut **+%192**.

**3. Volatilite ölçekleyici hiç çalışmamış.** `vol_target_annual: 0.35` bir hisse
portföyü hedefi; evrenin medyan yıllık volatilitesi **%79**. Ölçekleyici sürekli
tabanında (0.4x) sıkışıyor, her pozisyon niyetlenenin %44'ü olarak açılıyor,
sonra min_notional'a takılıyordu. Mekanizma muhafazakâr değil **işlevsizdi**.

0.80'e kalibre edilince sonuçlar **kötüleşti**: PF 1.34→1.16, getiri
+%3.66→+%0.95. Önceki sayının bir kısmı yapaydı — bozuk ölçekleyici farkında
olmadan şanslı bir alt küme seçiyordu. Düzeltme ölçümü güvenilir yaptı.

### İstatistiksel bütçe tükendi

9 denemeden sonra DSR'ın beklenen maks Sharpe eşiği ~1.5; gözlenen 0.24. Bu veri
setinde (21 sembol × 3 yıl) **daha fazla ayar yapıp sonuca güvenmek matematiksel
olarak mümkün değil.** CPCV (%44) ve PBO (0.67) aynı şeyi bağımsız söylüyor.

Kod yanlış demek değil. Aynı veriye kaç kez soru sorulduğunun bedeli var ve
ödendi. Çıkış yolu ayar değil **taze veri**: paper ileri testi (maliyeti sıfır,
ürettiği veri madenlenmemiş), daha fazla sembol/geçmiş, ya da tek seferlik test
edilen yeni hipotez sınıfı.

**Kümülatif deneme: 7** (bkz. `research/hypotheses.md`). Bir sonraki CPCV
koşusunda `--trials 7` geçirin — DSR her denemeyi fiyatlıyor.

Bu bir başarısızlık değil, sistemin çalıştığının kanıtı: altyapı yanlış bir
stratejiyi kârlı göstermek yerine reddetti. Sıradaki iş strateji araştırma
döngüsü — hipotez → backtest → CPCV → kabul/red. **Her denemeyi
`--trials N`'e say**; DSR bunu hesaba katıyor ve her deneme bir sonrakinin
barını yükseltiyor.

## Durum

- [x] Faz 0 — iskelet, config, modeller, DB
- [x] Faz 1 — veri (21 sembol × 3 TF × 3 yıl + türev, 63/63 seri temiz)
- [x] Faz 2 — indikatörler, IC/tail/redundans ölçümü
- [x] Faz 3 — kurulumlar, conviction motoru, backtest motoru
- [x] Faz 4 — CPCV + DSR + PBO + Monte Carlo → **Kapı 1: GEÇİLMEDİ**
- [x] Faz 5 — MAE/MFE çıkış kalibrasyonu
- [x] Faz 6 — risk bütçesi, paper/ccxt broker, runner, watchdog, launchd
- [x] Faz 7 — panel + Telegram
- [x] Faz 8 — LLM rolleri + redaksiyon + etki ölçümü
- [ ] Faz 9 — Bybit demo, 4 hafta (Kapı 1 geçilmeden anlamsız)
- [ ] Faz 10 — canlı $100 (Kapı 3'e bağlı)
