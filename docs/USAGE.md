# Kullanım kılavuzu

Botu sıfırdan çalıştırmaktan günlük takibe kadar. Komutların hepsi proje
kökünden çalıştırılır.

---

## 0. Kurulum — tek komut

```bash
cd ~/Documents/PROJECTS/TRADING-BOT
./ops/pre-install.sh
```

Aşağıdaki §1-§4'ün tamamını sırayla çalıştırır ve **her adımın gerçekten işe
yarayıp yaramadığını kontrol eder** — sadece hata vermemesini değil. İlk koşu
veri indirdiği için ~20 dakika sürer.

Ne yapıyor:

| adım | kontrol |
|---|---|
| Ön kontrol | işletim sistemi, python ≥3.11, disk, Bybit erişimi |
| Sanal ortam | `.venv` kur, bağımlılıkları yükle |
| `.env` | yoksa örnekten üret, `WEB_TOKEN` rastgele oluştur |
| Testler | 334 test + lint |
| Config | `config/*.yaml` hepsini yükleyip doğrula |
| Veri | 3 yıl 1h/4h/1d + türev serileri |
| Bütünlük | 63/63 seri temiz mi |
| Ölçüm | IC + kuyruk analizi, türev polaritesi |
| Backtest | al-tut karşılaştırmasıyla |
| Kapı 1 | CPCV + DSR + PBO |
| Sermaye | minimum bakiye ölçümü |
| Duman testi | motorun açılış yolunu kilide kadar yürüt |

**Yarıda kesilirse tekrar çalıştır** — biten adımlar atlanır, veri indirmesi
kaldığı yerden devam eder.

```bash
./ops/pre-install.sh --check          # hiçbir şeyi değiştirme, sadece doğrula
./ops/pre-install.sh --fast           # 3 yıl yerine 1 yıl veri (hızlı deneme)
./ops/pre-install.sh --skip-data      # mevcut arşivi kullan
./ops/pre-install.sh --force          # biten adımları da tekrar çalıştır
```

Sonunda üç şey yazar: ne bitti, **sana ne kaldı** (Bybit anahtarı, Telegram, LLM
— hiçbiri paper modu engellemiyor) ve Kapı 1 kararı.

Neyi **yapmıyor**: botu başlatmıyor, launchd kurmuyor, git'e pushlamıyor, gerçek
paraya dokunmuyor. Bunların hepsi ayrı ve kasıtlı kararlar.

API anahtarları ve Telegram için: **[SETUP.md](../SETUP.md)**. Paper modu için
hiçbir anahtar gerekmiyor.

---

> §1-§4 aşağıda tek tek anlatılıyor. `pre-install.sh` zaten çalıştıysan bunları
> tekrar yapmana gerek yok — burada **ne olduğunu anlamak** ve tek bir adımı
> yeniden çalıştırmak için duruyorlar.

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
trades 116  win 20.7%  PF 1.20  expectancy +0.339R
return +4.73%  maxDD 11.6%  Sharpe 0.22
buy & hold (BTC/USDT): +194.89%  maxDD 53.7%
bootstrap expectancy CI: [-0.100, +0.846]R
```

Sayılar arşiv uzadıkça kayar; buradakiler tam olarak tutmayabilir. Sabit olan
karşılaştırma.

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
  [FAIL] profit factor >= 1.2       1.20
  [PASS] max drawdown <= 20%        11.6%
  [FAIL] DSR significant            p=1.000
  [PASS] CPCV majority positive     55.6%
  [FAIL] PBO <= 0.5                 1.00
  VERDICT: FAIL
```

Aynı koşuyu `--trials 1` ile yaparsan DSR p=0.352 çıkıyor, yani "anlamlıya
yakın". Tek fark dürüstlük. `pre-install.sh` sayıyı `research/hypotheses.md`'den
kendisi okuyor, böylece unutulamıyor.

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

## 7. 7/24 çalıştır — elle python yok

```bash
./ops/bot.sh start paper      # veya demo, veya live
```

Tek komut: moda göre launchd agent'larını **üretir**, kurar, yükler ve gerçekten
kalktığını doğrular. Üç servis: bot, panel, sağlık kontrolü.

| komut | ne yapar |
|---|---|
| `./ops/bot.sh start <mod>` | ön kontrol + kur + başlat + doğrula |
| `./ops/bot.sh stop` | hepsini durdur (tasarımdaki "elle kapatma") |
| `./ops/bot.sh restart` | kurulu modu koruyarak yeniden başlat |
| `./ops/bot.sh status` | ne kurulu, ne çalışıyor, ne yapıyor |
| `./ops/bot.sh logs -f` | canlı olay akışı |
| `./ops/bot.sh mode` | şu an hangi mod kurulu |

### Üç mod

| mod | ne | anahtar |
|---|---|---|
| `paper` | canlı fiyat, simüle dolum | gerekmiyor |
| `demo` | gerçek emir akışı, sanal para | `BYBIT_DEMO_*` |
| `live` | **gerçek para** | `BYBIT_LIVE_*` + Kapı 1 + elle onay |

### Kurmadan önceki ön kontrol

Bu kısım göründüğünden önemli. launchd `KeepAlive=true` ile çalışıyor: başlayamayan
bir yapılandırma **bir kez** değil, 30 saniyede bir sonsuza kadar başarısız olur ve
API kotasını yakar. Bu yüzden agent kurulmadan önce kontrol edilebilecek her şey
kontrol ediliyor:

- config yükleniyor mu, `assert_runnable` geçiyor mu
- başka bir motor kilidi tutuyor mu
- evren boş mu, indikatör seçimi var mı
- broker kurulabiliyor mu (demo/live'da bu *kimlik doğrulama* testidir)
- **API anahtarının çekim yetkisi var mı** — varsa reddediliyor
- canlıda bakiye `min_equity`'nin altında mı

Sonra yükleniyor ve motorun `state.json`'a *doğru modda* "running" yazması
bekleniyor. 90 saniyede yazmazsa agent geri kaldırılıyor — çalışmayan bir
restart döngüsünü açık bırakmak, durmuş olmaktan kötü.

### Gerçek para iki kapıdan geçiyor

```bash
./ops/bot.sh start live
```

1. **Kapı 1 geçilmiş olmalı.** Geçilmediyse script reddediyor. Şu an geçilmedi.
2. **Onay cümlesini elle yazman gerekiyor.** Bayrakla geçilemiyor — bayrak bir
   script'in yanlışlıkla geçebileceği şeydir.

`TBOT_CONFIRM_LIVE=yes` plist'e yazılıyor, yani o bariyer agent kurulu olduğu
sürece kalıcı hale geliyor. Onay ekranı bunu açıkça söylüyor.

### Kod değiştirdiğinde

`KeepAlive` süreci **çökünce** geri getirir, **dosya değişince getirmez.** Python
kodu bir kez, açılışta yükleniyor. Bir şey düzelttiysen:

```bash
./ops/bot.sh restart
```

Unutması kolay ve sessiz: bot mutlu şekilde çalışmaya devam eder, sadece eski
kodu çalıştırır. `./ops/bot.sh status` bunu artık söylüyor:

```
! source files are newer than the running engine — it is executing old code
  ./ops/bot.sh restart
```

`restart` kurulu modu koruyor — `demo`daysan `demo`da kalıyor.

Config (`config/*.yaml`) değişikliği de aynı: restart gerekiyor. İstisna panel
düğmeleri ve Telegram komutları — onlar `command.json` üzerinden ~10 saniyede
işleniyor, yeniden başlatma istemiyor.

### Neden hâlâ ayakta

`KeepAlive=true` — süreç nasıl ölürse ölsün geri gelir, girişte otomatik başlar.
`caffeinate -i` Mac'in uyku moduna girmesini engelliyor (kripto 7/24, uyuyan Mac
çalışmayan bot demek).

Sağlık agent'ı 15 dakikada bir *takılmış* motoru arıyor — çökmüş motor gürültülü,
launchd onu geri getirir; takılmış motor sessiz: süreç yaşıyor, kimse yeniden
başlatmıyor, pozisyonlar yönetilmiyor ve her şey yolunda görünüyor.

> Durdurunca açık pozisyonlar **borsa tarafındaki stop'larıyla korunmaya devam
> eder** — o stop'lar Bybit'te duruyor, bu süreçte değil. Kapatmak istiyorsan
> önce `/flatten`.

---

## 8. Günlük takip

```bash
./ops/bot.sh status                                 # en hızlı bakış
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

## 11b. Baştan başlamak

Hesap ve yerel geçmiş dağıldıysa:

```bash
./ops/reset.sh --usdt 1000
```

Botu durduruyor, Bybit demo'da tüm emirleri (koruyucu stop'lar dahil) iptal
ediyor, tüm varlıkları USDT'ye satıyor, bakiyeyi hedefe çekiyor, sonra yerel
işlem geçmişini siliyor. Veritabanını silmeden önce `runtime/backups/`'a
yedekliyor.

**Bakiye ayarı gerçekten Bybit tarafında.** `/v5/account/demo-apply-money`
(`adjustType` 0 ekler, 1 azaltır). Yerelden ayarlanamaz — değişmez kural 19:
canlı/demo modda `starting_balance` yok sayılır, bakiye borsadan okunur.

Varsayılan olarak **korunanlar**: `runtime/data/` (3 yıllık arşiv),
`indicator_selection.json`, `universe.json`, `reports/`. Onlarda kirli bir şey
yok ve yeniden üretmek ~20 dakika.

```bash
./ops/reset.sh --local-only     # borsaya dokunma
./ops/reset.sh --venue-only     # yerel geçmişi koru
./ops/reset.sh --all            # arşiv ve ölçümler dahil her şey
```

`RESET` yazmanı istiyor. Gerçek parada **çalışmıyor** — venue tarafı
`bybit-demo` dışında her şeyi reddediyor ve bunu değiştiren bir bayrak yok.

Minimum notional altındaki toz bakiye satılamaz, raporda `left:` diye görünür.
Zararsız.

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
`./ops/bot.sh status` — kurulu agent mı, elle başlatılmış bir süreç mi olduğunu söyler

**"no_indicator_selection" uyarısı** → `scripts/ic_report.py` çalıştır

**Panel boş** → bot hiç çalışmamış. `runtime/state.json` yoksa henüz veri yok.

**Hiç işlem açılmıyor** → normal olabilir, bot seçici. Kontrol et:
```bash
.venv/bin/python scripts/report.py --since 7d     # "Neden işlem yapılmadı" tablosu
```
`min_notional` çoksa bakiye yetersiz → `scripts/capital_check.py`

**Backtest çok az işlem üretiyor** → huni raporu hangi aşamada kesildiğini
gösteriyor; `scripts/capital_check.py` sermaye kısıtı mı diye bakar.

**Bot başlamıyor / sürekli yeniden başlıyor** → `bot.sh start` bunu zaten
engelliyor, ama elle yüklendiyse:
```bash
./ops/bot.sh status
tail -20 runtime/logs/launchd.err.log
./ops/bot.sh stop && ./ops/bot.sh start paper
```

**`pre-install.sh` bir adımda patladı** → her adımın tam çıktısı duruyor:
```bash
ls runtime/logs/preinstall-*.log
```
Düzelttikten sonra tekrar çalıştır; biten adımlar atlanır.

**`pre-install.sh` bir adımı hep atlıyor** → biten sayılıyor. Zorla:
```bash
./ops/pre-install.sh --force
```
Adım listesi `runtime/.preinstall.state` dosyasında; bir satırı silmek o adımı
tekrar çalıştırır.

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
| `runtime/logs/preinstall-*.log` | kurulum koşusunun adım adım tam çıktısı |
| `runtime/.preinstall.state` | hangi kurulum adımının bittiği |
| `runtime/logs/launchd.err.log` | launchd'nin yakaladığı çökme çıktısı |
