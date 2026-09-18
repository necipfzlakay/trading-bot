# Kullanım kılavuzu

Botu sıfırdan çalıştırmaktan günlük takibe kadar. Komutların hepsi proje
kökünden çalıştırılır.

---

## 0. Bir kerelik kurulum

```bash
cd ~/Documents/PROJECTS/TRADING-BOT
.venv/bin/python -m pytest -q          # 334 test geçmeli
```

Geçmiyorsa devam etme — bir şey bozuk.

API anahtarları ve Telegram için: **[SETUP.md](../SETUP.md)**. Paper modu için
hiçbir anahtar gerekmiyor, hemen başlayabilirsin.

---

## 1. Veriyi çek (ilk sefer ~15 dakika)

```bash
.venv/bin/python scripts/backfill.py --years 3 --with-derivatives
```

Ne yapıyor: evreni hacme göre seçiyor (21 çift), her biri için 3 yıllık 1h/4h/1d
mum + funding/açık pozisyon/long-short verisi indiriyor, sonra bütünlük
kontrolünden geçiriyor.

Beklenen çıktı sonu:
```
Fetched in 876s. Failures: 0
Integrity: 63/63 clean -> runtime/reports/integrity.md
```

**Yarıda kesilirse sorun değil** — tekrar çalıştır, kaldığı yerden devam eder.

Sonradan güncellemek için (hızlı, sadece eksik barları çeker):
```bash
.venv/bin/python scripts/backfill.py
```

Evreni yeniden seçmek için:
```bash
.venv/bin/python scripts/backfill.py --refresh-universe
```

---

## 2. Ölçümleri çalıştır (bir kez, veri değişince tekrar)

```bash
.venv/bin/python scripts/ic_report.py --timeframe 1d    # ~5 dk
.venv/bin/python scripts/polarity.py                    # ~2 dk
```

İlki hangi indikatörün oy vermeye hak kazandığını ölçüyor ve
`runtime/indicator_selection.json` yazıyor — motor bunu okuyor. İkincisi türev
sinyallerin yönünü doğruluyor.

Çalıştırmazsan bot yine çalışır ama her indikatör ayrı oy verir, yani redundans
eliminasyonu devre dışı kalır. Log'da uyarı görürsün.

---

## 3. Backtest et

```bash
.venv/bin/python scripts/backtest.py
```

Çıktı `runtime/reports/backtest.md`. Terminalde özet:

```
trades 116  win 20.7%  PF 1.16  expectancy +0.294R
return +0.95%  maxDD 5.3%  Sharpe 0.24
buy & hold (BTC/USDT): +192.19%  maxDD 53.7%
bootstrap expectancy CI: [-0.12, +0.78]R
```

**Bakılacak satır al-tut karşılaştırması.** Sıfırı geçmek yeterli değil; her
şeyin yükseldiği bir dönemde al-tut'un altında kalan strateji değer üretmemiş.

Belirli tarih aralığı veya sembol için:
```bash
.venv/bin/python scripts/backtest.py --from 2024-01-01 --to 2025-06-01
.venv/bin/python scripts/backtest.py --symbols BTC/USDT,ETH/USDT
```

---

## 4. Kapı 1: sonuç şanstan ayırt edilebiliyor mu

```bash
.venv/bin/python scripts/cpcv_report.py --trials 9
```

`--trials` = şimdiye kadar kaç farklı ayar/strateji denediğin. **Dürüst say** —
Deflated Sharpe bunu fiyatlıyor ve her deneme bir sonrakinin barını yükseltiyor.
Kaç denendiğini `research/hypotheses.md` tutuyor.

```
  [PASS] trades >= 50               116
  [PASS] profit factor >= 1.2       1.34
  [PASS] max drawdown <= 20%        8.8%
  [FAIL] DSR significant            p=1.000
  [FAIL] CPCV majority positive     44.4%
  [FAIL] PBO <= 0.5                 0.67
  VERDICT: FAIL
```

Bu kapı geçilmeden gerçek para koyma. Şu an geçilmiş **değil**.

---

## 5. Paper modda çalıştır

```bash
.venv/bin/python scripts/run.py --config config/paper.yaml
```

Canlı veri, simüle emirler, gerçek komisyon ve kayma. Açılışta gördüklerin:

```
  mode    paper
  venue   bybit-demo  [DEMO (virtual funds)]
  symbols 21
  equity  1000.0 USDT
  bar     1h (setup 4h, permission 1d)
  risk    7.50 USDT/trade · widest usable stop 130.4%
```

Saat başı bar kapanışında uyanıyor, tarıyor, gerekirse işlem açıyor. Durdurmak:
`Ctrl+C`.

**Aynı anda ikinci kopya başlatamazsın** — kilit engelliyor. İki bot aynı hesapta
her pozisyonu ikiye katlar.

---

## 6. Paneli aç

Ayrı bir terminalde:

```bash
.venv/bin/python scripts/dashboard.py
```

→ http://127.0.0.1:8787

Ne göreceksin:
- **Account** — equity, getiri, düşüş, risk çarpanı, conviction eşiği
- **Equity** — sermaye eğrisi
- **Open positions** — açık pozisyonlar, MFE/MAE, kaç bar tutuldu
- **Scan** — *her sembol, her bar*: skor, eşik, ve geçemediyse **neden**
- **Why no trade** — son 24 saatin red sebepleri, sayıyla
- **MAE/MFE** — kapanan işlemlerin saçılımı; kazananlar sol üstte kümeleniyorsa
  stop gereğinden geniş demektir

Panel motorun yazdığı dosyaları okuyor — ikinci bir doğruluk kaynağı yok,
panelle bot çelişemez.

---

## 7. 7/24 çalıştır (launchd)

```bash
./ops/install.sh
```

Üç servis kurar: bot, panel, sağlık kontrolü. `KeepAlive=true` — süreç nasıl
ölürse ölsün geri gelir, girişte otomatik başlar. `caffeinate -i` Mac'in uyku
moduna girmesini engelliyor (kripto 7/24, uyuyan Mac çalışmayan bot demek).

Durum:
```bash
launchctl list | grep tbot
tail -f runtime/logs/bot.jsonl
```

**Durdurmak** (tasarımdaki "elle kapatma" bu):
```bash
launchctl unload -w ~/Library/LaunchAgents/com.tbot.bot.plist
```

---

## 8. Günlük takip

```bash
.venv/bin/python scripts/report.py --since 7d       # durum raporu
.venv/bin/python scripts/healthcheck.py             # sağlıklı mı
```

Sağlık kontrolü zaten 15 dakikada bir otomatik çalışıyor ve **sadece sorun
varsa** Telegram'a yazıyor. Her şey yolundayken susması kasıtlı — 15 dakikada bir
"iyiyim" mesajı, insanı kanalı görmezden gelmeye alıştırır.

Ne kontrol ediyor: `state.json` güncelleniyor mu (süreç *çökmeden takılırsa*
başka hiçbir şey fark etmez), süreç yaşıyor mu, disk doldu mu. Ayrıca veritabanı
yedeği alıyor (`runtime/backups/`, son 14 kopya).

---

## 9. Müdahale

**Panelden** (Pause / Flatten düğmeleri) veya **Telegram'dan**:

| komut | ne yapar |
|---|---|
| `/status` | anlık durum |
| `/pnl` `/positions` | kâr-zarar, açık pozisyonlar |
| `/scan` | tarama tablosu |
| `/pause` | yeni giriş yok, mevcutlar yönetilmeye devam |
| `/resume` | devam |
| `/flatten` | **tüm pozisyonları kapat** |

Komutlar ~10 saniyede işleniyor (bar kapanışını beklemiyor). Hiçbiri senin
cevabını beklemiyor — bot kendi onayını kendi veriyor, sen sadece müdahale
edebiliyorsun.

---

## 10. Bot kendi kendine ne yapar

Müdahale etmen gerekmeyen durumlar:

| durum | bot ne yapar |
|---|---|
| Düşüş %5-10 | pozisyon boyutu 0.75×, conviction eşiği +5 |
| Düşüş %10-15 | 0.50×, eşik +10 |
| Düşüş >%15 | 0.25×, eşik +15 |
| Düşüş >%50 | **yeni giriş yok**, mevcutlar korunur |
| Bakiye < $183 | **yeni giriş yok** (boyutlandırma evreni değiştirir) |
| Üst üste zarar | eşik yükselir, yasak gelmez |
| Veri kopması | degraded mode, mevcutlar stop'la korunur, kendiliğinden toparlar |
| Borsa reddi | 3 red sonrası sembol evrenden düşer |
| Çökme | launchd yeniden başlatır, borsayla mutabakat yapar |
| Saatte 10+ restart | güvenli mod + alarm |

**Bot zararda asla tamamen durmaz** — küçülür ve seçicileşir. Kripto 7/24 ve
ikili kilitler fırsat maliyeti üretir.

---

## 11. Demo'ya geçiş (gerçek emir akışı, sanal para)

Kapı 1 geçildikten sonra. Bybit demo anahtarı gerekiyor (SETUP.md §3).

```bash
.venv/bin/python scripts/demo_topup.py --usdt 1000
.venv/bin/python scripts/run.py --config config/demo.yaml
```

4 hafta koştur, sonra:

```bash
.venv/bin/python scripts/divergence.py --since 30d
```

Backtest ile canlının aynı dönemde aynı işlemleri yapıp yapmadığını ölçüyor.
Sapma %10'un üstündeyse bir yerde bug var — canlı yolda, veride veya
simülatörde.

---

## 12. Gerçek para

Kapı 1 + Kapı 2 + 4 hafta demo geçilmeden **yapma**. Sonra:

```bash
TBOT_CONFIRM_LIVE=yes .venv/bin/python scripts/run.py --config config/live.yaml
```

Environment değişkeni kasıtlı bir bariyer. Ayrıca bot, API anahtarının **çekim
yetkisi varsa başlamayı reddediyor** — bot buna hiç ihtiyaç duymaz ve varlığı
`.env` sızarsa farkı "kötü işlem" ile "hesap boşalır" arasına çevirir.

---

## Ayarları değiştirmek

Hepsi `config/base.yaml`. En çok dokunacakların:

```yaml
risk:
  risk_per_trade: 0.0075        # işlem başına equity'nin yüzdesi (1R)
  max_positions: 3              # aynı anda açık pozisyon
  min_equity: 183.0             # altında yeni giriş yok
  max_position_pct: 0.40        # tek pozisyon equity'nin en fazla %40'ı
  max_position_notional: 0.0    # tek pozisyonda maks USDT (0 = sınırsız)
  max_risk_amount: 0.0          # tek işlemde maks 1R USDT (0 = sınırsız)

conviction:
  base_threshold: 62.0          # seçicilik. yükseltmek = daha az işlem
  selection: cross_sectional    # semboller birbirine karşı sıralanır

universe:
  symbols: []                   # elle liste yazarsan otomatik seçim kapanır
  min_volume_24h_usd: 5000000
```

`risk_per_trade` değiştirirsen minimum bakiyeyi yeniden ölç:
```bash
.venv/bin/python scripts/capital_check.py
```

> **Uyarı:** parametre değiştirip backtest'i iyileştirmek bir *deneme*.
> `research/hypotheses.md`'ye yaz ve sayacı artır. Deflated Sharpe kaç kez
> denendiğini fiyatlıyor — saymazsan kendini kandırırsın.

---

## Sorun giderme

**"empty universe"** → `scripts/backfill.py --refresh-universe`

**"Another engine is already running"** → başka bir kopya çalışıyor.
`launchctl list | grep tbot` veya `pgrep -f scripts/run.py`

**"no_indicator_selection" uyarısı** → `scripts/ic_report.py` çalıştır

**Panel boş** → bot hiç çalışmamış. `runtime/state.json` yoksa henüz veri yok.

**Hiç işlem açılmıyor** → normal olabilir, bot seçici. Kontrol et:
```bash
.venv/bin/python scripts/report.py --since 7d     # "Neden işlem yapılmadı" tablosu
```
`min_notional` çoksa bakiye yetersiz → `scripts/capital_check.py`

**Backtest çok az işlem üretiyor** → huni raporu hangi aşamada kesildiğini
gösteriyor; `scripts/capital_check.py` sermaye kısıtı mı diye bakar.

**Her şey bozuk göründü** → veritabanını geri yükle:
```bash
ls runtime/backups/
cp runtime/backups/tbot-YYYYMMDD-HHMM.db runtime/tbot.db
```

---

## Dosya haritası

| yol | ne var |
|---|---|
| `runtime/state.json` | anlık durum — ilk bakılacak yer |
| `runtime/logs/bot.jsonl` | yapılandırılmış log, her satır bir olay |
| `runtime/tbot.db` | tüm geçmiş. `scans` tablosu her barda **neden işlem yapılmadığını** tutar |
| `runtime/reports/` | backtest, IC, kuyruk, redundans, çıkış, bütünlük raporları |
| `runtime/backups/` | veritabanı yedekleri (son 14) |
| `research/hypotheses.md` | denenen ve denenecek hipotezler + deneme sayacı |
