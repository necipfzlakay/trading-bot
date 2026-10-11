# Araştırma hipotezleri

Strateji araştırma döngüsünün defteri. Her satır bir hipotez; kabul edilmesi için
backtest + CPCV'den geçmesi gerekiyor. **Denenen her hipotezi sayın** —
`scripts/cpcv_report.py --trials N` bunu Deflated Sharpe'a giriyor ve her deneme
bir sonrakinin barını yükseltiyor.

## Durum

| # | hipotez | kaynak | durum | deneme |
|---|---|---|---|---|
| 1 | Momentum üst desili trend rejiminde 20 gün tutulursa taban oranı geçer | tail analizi | test edildi, **red** (n=18, PF ~1.0) | 1 |
| 2 | Funding contrarian okunmalı (yüksek funding = squeeze) | yaygın kabul | **red** — 5/5 ufukta momentum çıktı, registry düzeltildi | 0 (ölçüm, strateji denemesi değil) |
| 3 | long_short contrarian okunmalı | yaygın kabul | **doğrulandı** — 5/5 ufukta contrarian | 0 |
| 4 | Yavaş zaman dilimi (1w/1d/4h) ölçümle daha uyumlu | tail analizi | **sonuçsuz** — 3 işlem, test edilemez | 1 |
| 5 | Conviction eşiği taraması (54/58/62/66/70) | plato testi | **red** — her eşikte expectancy negatif; eşik sadece kayba maruziyeti azaltıyor | 5 |

| 6 | $100'da min_notional adayların çoğunu kesiyor, ölçülen şey strateji değil | huni ölçümü | **doğrulandı** — 1.246 adayın 1.215'i (%97.5) boyutlandırmada ölüyor | 1 |
| 7 | Motor kesitsel değil mutlak eşik kullanıyor; tail analizi kesitseldi | mimari denetim | **doğrulandı ve düzeltildi** — 63→116 işlem, PF 0.97→1.34, expectancy −0.004→+0.280R | 1 |

| 8 | Volatilite ölçekleyici çalışıyor | varsayım | **red — mekanizma hiç çalışmamış.** Hedef %35 (hisse portföyü değeri), evren medyanı %79. Ölçekleyici sürekli tabanda, her pozisyon niyetlenenin %44'ü. Düzeltildi (0.80) | 0 (tamir, deneme değil) |

| 9 | Conviction-çöküş çıkışı canlıda ve backtest'te **hiç tetiklenmiyordu** (`conviction_now=None` sabit) | mimari denetim | **doğrulandı; ilk tamir NO-OP çıktı, ikinci tamir çalıştı, sonuç REDDEDİLDİ** — aşağıya bakın | 1 |
| 11 | Conviction-çöküş çıkışı (eşik 32) sonucu iyileştirir | belgelenen 4. katman | **test edildi, REDDEDİLDİ** — PF 1.23→1.12, expectancy +0.179→+0.100R, yakalama %8.7→%5.6. Kapatıldı (`collapse_threshold: 0.0`) | (9'daki 1'e dahil) |

| 10 | Kırılım seviyesine önceden kurulu emir bırakmak, bar kapanışını beklemekten iyidir | bar-arası körlük | **test edildi, REDDEDİLDİ** — sonuç iyi göründü ama 5 işlemden geliyor, 2'si %92'sini taşıyor. Mekanizma kuruldu, varsayılan **kapalı** | 1 |

| 12 | Kısmi kâr alma (+1R/+1.5R/+2R'de %50 sat) sonucu iyileştirir | MFE/yakalama ölçümü | **test edildi, REDDEDİLDİ** — üç seviyede de PF ve getiri düştü. Düşüşü azaltıyor (%15.8→%12.8) ama getiriyi de azaltıyor | 3 |
| 13 | `risk_per_trade` 0.75%→1.2% (operatör pozisyon boyutu istedi) | tercih | **uygulandı** — maks düşüş %11.9→%15.8. Sharpe ~ölçek-bağımsız olduğu için DSR'a etkisi küçük | 1 |

| 14 | Kurulum kapısı gereksiz — kesitsel sıralama tek başına yeter (açık hipotez #4) | mimari denetim | **test edildi, REDDEDİLDİ** — 3.6x işlem (103→367) ama Kapı 1'de 5/6 → **2/6**, PBO 0.33 → **0.67** | 1 |
| 15 | Taker akış dengesizliği (CVD) kısa vadede edge taşır | fiyattan bağımsız tek kaynak | **taranıp REDDEDİLDİ** — üst desil lift +0.8 puan, gereken +13. Strateji koşulmadı | 0 (indikatör taraması) |
| 16 | Perp ücret katmanı ve short tarafı kısa vadeli aritmetiği değiştirir | operatör sorusu | **tarandı** — maliyet %0.30→%0.07, short tabanı long'dan +2.1 puan iyi. Açık %8.8 puandan **%1.8 puana** indi, ama kapanmadı | 0 (bariyer taraması, strateji koşulmadı) |
| 17 | **Kesitsel conviction skoru perp'te SHORT tarafında %0.07 maliyeti karşılar** | H16'nın bıraktığı açık | **ileri test, 47/50 işlem** — ara CI [−0.58, −0.10]R, tamamen sıfırın altında; yargı 50'de (havuzlu) | 1 |
| 18a | **1m sert hareketi takip (momentum), maker giriş, perp** | operatör: "dakikada 10 işlem açabilen bot" | **REDDEDİLDİ** — 2182 işlem, −%0.095/işlem, komisyon öncesi bile negatif | 1 |
| 18b | **1m sert hareketi sönümleme (dönüş), aynı tetik, aynı motor** | 18a ile rastgele bölüşülmüş A/B kolu | **REDDEDİLDİ** — 2151 işlem, −%0.074/işlem, brüt ≈ 0 | 1 |

**Kümülatif deneme: 19.** Bir sonraki CPCV koşusunda `--trials 19` geçirin.

## 18. ve 19. deneme: H18a / H18b — ÖN-KAYIT (backtest ve canlı koşudan önce yazıldı)

**Tarih:** 2026-09-21 · **Motor:** `config/scalp.yaml` (`engine.kind: scalp`) · **Tahsis:** $1000

### Neden soruluyor

Operatör, dakikada ~10 işlem açabilecek kapasitede, 1 dakikalık barlarla çalışan
yüksek riskli üçüncü bir motor istedi. Değişmez 53 ve 58 kısa ufku üç ölçümle
kapatmıştı; 1m hiç ölçülmemişti. Kurmadan önce ölçüldü (Bybit perp public 1m,
BTC/ETH/SOL/XRP/DOGE/SUI × 7 gün × 10.080 bar):

| | değer |
|---|---|
| medyan \|1m getiri\| | %0.026 (BTC) – %0.070 (SUI) |
| gidiş-dönüş maliyet | maker giriş + taker çıkış %0.07 → hareketin **%100–270'i** |
| koşulsuz bariyerler (+0.1/−0.1 … +1.0/−0.5) | isabet yazı-turaya yapışık, **hepsi ≈ −%0.07/işlem** |
| tetik sonrası takip, +0.3/−0.3 | isabet **%57.8**, başabaş %61.7 (taker çıkışla), n=218 |
| tetik sonrası sönümleme, +0.3/−0.3 | isabet %41.7–52.1, n=117–218 |

### Hipotez

> Kapanmış 1m barlarda 5 barlık getiri 2.5·σ₆₀·√5'i aştığında, **(a)** hareket yönünde
> **(b)** tersine PostOnly maker girişle açılan, %0.3 limit TP / %0.3 market SL /
> 15 dakika zaman stop'lu pozisyon, Bybit linear perp'te gidiş-dönüş maliyetini karşılar.

Kol ataması `blake2b(symbol|bar_ts) % 2` — sonuçtan bağımsız, aynı olay popülasyonu
iki kola bölünür. Temiz A/B; iki kol aynı olayda zıt yön istediği için sembol
paylaşmaları zaten imkânsız.

### Beklenen sonuç (ön-kayıt)

Maker TP ile başabaş isabet ≈ **%56.9** (kazanç 0.3−0.02, kayıp 0.3+0.07). Takip kolunun
7 günlük nokta tahmini (%57.8) başabaşın üstünde ama **gürültü içinde** (n=218, SE≈3.3
puan) ve maker girişin **ters seçimi** (limit, fiyat girişin aleyhine gittiğinde dolar)
bu tahminde yok. Beklenti:

| kol | işlem başına net |
|---|---|
| H18a momentum | **−%0.02 ile −%0.05** |
| H18b dönüş | **−%0.05 ile −%0.10** |

İkisi de negatif. Bir kolun pozitif çıkması **şüpheyle** karşılanır (denetim #47/#49):
önce dolum modeli, sonra rejim (trendli hafta?), sonra şans.

### Tam şartname (serbest parametre araması YOK)

`trigger_bars 5 · sigma_window 60 · trigger_sigma 2.5 · take_profit_pct 0.003 ·
stop_loss_pct 0.003 · time_stop_bars 15 · entry_ttl_seconds 20 · max_positions 8 ·
notional = tahsis/8 · 1x`. Evren: en likit 40 linear perp, ≥$20M hacim, ≤3bps spread,
≥90 gün geçmiş, açılışta bir kez sabitlenir (değişmez 69).

### Canlı koşu notları

**2026-09-21 12:15–12:40 UTC (sıfırlama öncesi, sayılmaz).** 9 kapanmış işlem:
4 TP, 5 stop, ≈ −$1.1. Ölçülenler: maker giriş dolum oranı **~%33** (sert hareketten
hemen sonra en iyi seviyeye katılan PostOnly'lerin çoğu `EC_PostOnlyWillTakeLiquidity`
ile anında iptal; backtest ~%92 varsaymıştı), market stop kayması −4.6 / +5.8 bps,
TradFi perp'ler `110123` (sözleşme onayı) ile işlem yapılamaz. Kayıtlar protokol
yeniden başlatmasında silindi; yedek `runtime-scalp/backups/pre-reset-20260921-155301.db`.
**H18 sayımı 2026-09-21 15:53 UTC sıfırlamasından başlar**; parametreler aynı.

**2026-09-22 06:24 UTC — PROTOKOL YENİDEN BAŞLATMA (ikinci).** Operatör kararıyla hesap
ve üç motorun kayıtları yeniden sıfırlandı. Sebep sermaye muhasebesi: restart açık
pozisyonun P&L'ini iki kez sayıyordu ve drawdown'u unutturuyordu (denetim #94, #96);
ayrıca bir çıkış tahminle yazılmıştı (#95). Strateji parametreleri **değişmedi**; değişen
yalnız çıkış dolumunu bekleme süresi (5 yoklama → 60 sn), yani kaydın doğruluğu.
Sıfırlama öncesi koşu (12:57 UTC → 06:24 UTC, ~17 saat): **214 işlem** (momentum 103,
dönüş 111), 100 TP / 105 stop / 9 zaman stop'u, borsaya göre **−$19.26**. Bu not
sonuçlar görüldükten sonra yazıldı — sıfırlama kararı sonuçtan değil muhasebe
hatalarından geldi, ama bu ayrım okuyucuya bırakılmamalı. Dört işlemin çıkışı tahminle
yazılmıştır (#92'de 3, #95'te 1); havuzlanacaksa bunlar önce borsanın execution
listesinden düzeltilmeli. Havuzlama kararı operatörde. Yedek
`runtime-scalp/backups/pre-reset-20260922-092458.db`. **H18 sayımı bu andan başlar.**

**2026-10-11 — SONUÇ (sıfırlama öncesi, 2026-09-22 06:24 → 2026-10-11 20:55 UTC, 470 saat).**
4333 işlem (1717 TP / 1835 stop / 781 zaman), Σpnl **−$432.73**, bildirilen komisyon
**$394.45** — kaybın %91'i. Kol başına, işlem başına net getiri, bootstrap %95 CI:

| kol | n | net | ilk 300 işlem | komisyon öncesi |
|---|---|---|---|---|
| H18a momentum | 2182 | −%0.095 [−0.107, −0.082] | −%0.066 [−0.101, −0.030] | **−%0.019** [−0.031, −0.007] |
| H18b dönüş | 2151 | −%0.074 [−0.087, −0.062] | −%0.072 [−0.107, −0.037] | +%0.003 [−0.010, +0.015] |

**İki kol da 300. işlemde ön-kayıtlı kapatma kuralını tetiklemişti** (CI tamamen sıfırın
altında) — kural uygulanmadı, çünkü sonuç kimse bakmadan birikti. Beklentiyle uyumlu:
H18a −%0.02/−%0.05 bekleniyordu, −%0.095 çıktı (maker girişin ters seçimi: komisyon
*öncesi* bile negatif); H18b −%0.05/−%0.10 bekleniyordu, −%0.074. Dönüşün brüt edge'i
sıfır, momentumun brüt edge'i **negatif**. **H18 REDDEDİLDİ**, iki kol da.

**Operatör kararı: motor durmuyor.** 2026-10-12 sıfırlamasından sonra aynı parametrelerle
koşmaya devam ediyor. Bu **yeni bir hipotez denemesi değil** ve deneme sayacını
artırmıyor: H18'in cevabı yukarıda, sonraki koşu yalnız borsa gerçeklerini (dolum
oranı, gerçek komisyon, TP'nin taker dolması) daha uzun pencerede ölçüyor. Sonraki
veriden "H18 aslında pozitifmiş" okunacaksa önce bu paragraf okunmalı: aynı hipotezi
reddedildikten sonra tekrar sormak, DSR'ın fiyatladığı şeyin ta kendisi.
Yedek: `runtime-scalp/backups/pre-reset-20261012-*.db`.

### Kill kriterleri (koşmadan önce yazıldı)

| durum | aksiyon |
|---|---|
| equity ≤ $500 | **motor yeni giriş açmaz** (kodda: `scalp.kill_equity`) |
| bir kolda < 100 kapanmış işlem | hiçbir yargı yok |
| 300 işlemde beklentinin bootstrap %95 CI'si tamamen sıfırın altında | o kolu **kapat**, buraya yaz |
| CI sıfırı içeriyor | 600 işleme uzat |
| CI tamamen sıfırın üstünde | Kapı 1'e taşı (`--trials 19`), dolum modelini canlıyla karşılaştır |

### Protokol

1. `scripts/scalp_backtest.py` son 30 gün 1m üzerinde **bir kez**, bu parametrelerle,
   ayar yapılmadan koşar. Sonuç buraya yazılır; beklentiyle çelişirse parametre
   değil **dolum modeli** sorgulanır.
2. Canlı koşu backtest'ten bağımsız olarak başlar — backtest bir kapı değil, bir
   karşılaştırma noktası (değişmez 59: aynı `signal.evaluate`).
3. fast ile sembol paylaşımı kiralamayla (ilk açan sahiplenir). Kiralı sembolde
   kaçan tetikler `scans`'e `claimed_by_other_engine` ile yazılır; analiz bu
   satırlar dahil ve hariç iki kez yapılır.

### Backtest sonucu (2026-09-21, tek koşu, ayarsız, `--to 2026-09-21T10:15:00`)

40 sembol × 30 gün × 43.234 bar. `runtime-scalp/reports/scalp_backtest.md`.

| kol | tetik | işlem | isabet | işlem başına net | %95 CI | PF |
|---|---|---|---|---|---|---|
| H18a momentum | 18.219 | 7.231 | %36.6 | **−%0.132** | [−0.140, −0.125] | 0.42 |
| H18b dönüş | 17.963 | 6.847 | %36.2 | **−%0.136** | [−0.144, −0.129] | 0.41 |

19.6 işlem/saat. $1000 tahsiste 30 günde −$2.365 (öldürme eşiği ~6. günde devreye
girerdi). **Yön ön-kayıtla aynı, büyüklük ~2 kat kötü.**

Protokol gereği parametre değil **dolum modeli** sorgulandı — yalnız duyarlılık,
strateji sabit:

| dolum varsayımı | net | PF |
|---|---|---|
| ön-kayıtlı | −%0.134 | 0.41 |
| + market çıkışta kayma yok | −%0.101 | 0.49 |
| + dolum barında stop yok | −%0.089 | 0.53 |
| + ters seçim yok (her tetik kapanıştan dolar) | **−%0.068** | 0.62 |

Her kötümserlik kaldırılınca bile kalan −%0.068 bir işlemin maliyetine eşit — 1m
REST ölçümünün koşulsuz bariyerlerde bulduğu sayı. **Kaybı model değil edge
yokluğu üretiyor.** Takip kolunun 7 günlük %57.8'i 30 günde tutmadı (koşulsuz
isabet %43.2, gereken ~%57). İki kol arasında fark yok: aynı tetiğin iki yönü de
maliyet kadar kaybediyor — yani tetik yönle ilgili bir bilgi taşımıyor.

Canlı koşu ön-kayıt gereği yine de başlıyor: backtest bir kapı değil, canlıdaki
gerçek dolum, gerçek kayma ve gerçek ters seçimle karşılaştırılacak nokta. Beklenen
yanma ≈ $3.3/saat; $500 öldürme eşiği bunu sınırlıyor.

## 17. deneme: H17 — ÖN-KAYIT (sonuç görülmeden yazıldı)

**Tarih:** 2026-09-19 · **Motor:** `config/fast.yaml` · **Branch:** `feat/capital-recycling`

Bu bölüm **motor çalıştırılmadan önce** yazıldı. Sebebi denetim #47 ve #49: kendi
tamirimin ölçümüne başkasınınki kadar şüpheyle bakmadığım için bir kez yanlış
atıf yaptım. Beklentiyi önceden yazmak, sonucu gördükten sonra hikâyeyi ona
uydurmayı imkânsız kılıyor.

### Hipotez

> Bybit linear perpetual'da, kesitsel conviction skorunun **alt** ucunu short'layıp
> üst ucunu long'lamak, %0.07'lik gidiş-dönüş maliyetini karşılayacak edge taşır.

### Neden bu soru soruluyor

Kısa vadeli al-sat üç ölçümle kapatılmıştı (değişmez kural 53). Operatörün
"konuşmadığımız bir şey var mıydı" sorusu iki varsayımı kırdı:

| varsayım | ölçüm |
|---|---|
| maliyet %0.30 | spot'ta öyle; **perp'te taker %0.06 / maker %0.01**, gerçekçi tur **%0.07** |
| sadece long ölçüldü | short tabanı her ufukta long'dan **+1.4 ila +2.2 puan** iyi |

H=24, +2%/−1% bariyerde (21 sembol × 3 yıl, D40 kötümser):

```
LONG  base   %31.8   |  SHORT base   %33.9   |  adil %33.3  |  gereken %35.7
```

Açık **%8.8 puandan %1.8 puana** indi. Dokümanda kayıtlı en iyi sinyal lifti
**+2.6 puan**. Yani aritmetik olarak ilk kez geçilebilir bir açık var.

### Beklenen sonuç (ön-kayıt)

**NEGATİF. İşlem başına ≈ −0,05%.**

Gerekçe: arşivde o +1.8 puanlık lifti **bulamadım**. Denenen ve başarısız olan:

- 24 barlık `price_slope` alt desili + short: %32.6 — short tabanından (%33.9) **kötü**
- volatilite koşullandırması: her ATR desilinde düz (%30.7–%32.8)
- ATR'ye ölçeklenmiş bariyer: %30.4, adil oranın altında
- daha kısa bar (5m/15m): gereken isabet %48.9 / %38.0'a çıkıyor, elimizdeki ~%33

Bulunan tek pozitif hücre (1h, +3%/−1.5%, 48 bar, short: %35.2 vs gereken %34.9)
iki sebeple sayılmıyor: **0.71 sigma**, ve örneklem ayı piyasası (12 sembolün
medyanı −%51.5, 11'i düşmüş).

### Karşı-hipotez, aynı anda ön-kayıtlı

**Kâr yalnızca düşen rejim işlemlerinden geliyorsa bu edge değil, yön bahsidir.**
Rejim ayrımı bunu zaten gösterdi:

| giriş öncesi 1 haftalık getiri | isabet | edge |
|---|---|---|
| düşen (<−5%) | %37.11 | **+2.23%** |
| yatay (±5%) | %33.15 | −1.74% |
| yükselen (>+5%) | %34.15 | −0.74% |

Bu yüzden her işlemle birlikte **giriş rejimi ve giriş öncesi 1 haftalık sembol
getirisi** kaydediliyor. Ayrım yapılamazsa sonuç yorumlanamaz.

### Tam şartname (serbest parametre araması YOK)

| | |
|---|---|
| evren | linear perp, 24s hacimde üst 30, `min_history_days: 540` |
| zaman dilimleri | 1d izin / 4h kurulum / 1h tetik — ana motorla **aynı** |
| yön | kesitsel sıralamanın üstü long, altı short |
| kurulumlar | long 3 + **aynalanmış short 3** (kapı kaldırılmadı — değişmez kural 52) |
| çıkış | hedef 2×ATR, stop 1×ATR, **48 bar sert zaman stop'u** |
| boyut | $300 sert tavan, %1.2 risk, **1x kaldıraç** |
| eşitlik | aynı barda hedef ve stop → stop (değişmez kural 40) |

### Kill kriterleri (koşmadan önce yazıldı)

| durum | aksiyon |
|---|---|
| < 50 kapanmış işlem | **hiçbir yargı yok** — hiçbir istatistik bu sayıda anlamlı değil |
| 50 işlemde beklenti bootstrap %95 CI'si tamamen sıfırın altında | **kapat** ve buraya yaz |
| CI sıfırı içeriyor | 100 işleme uzat |
| CI tamamen sıfırın üstünde | Kapı 1'e taşı (CPCV + DSR, `--trials 17`) |
| kâr yalnızca düşen rejimden | **yön bahsi**, edge değil — kapat |
| $300 tahsisinde −%15 | dondur: yeni giriş yok, mevcutlar yönetilir |

### Protokol notları (ileri test sırasında, sonuca bakılmadan yazıldı)

**2026-09-21 — ilk işlemin kaydı yanlış, veri düzeltmesi operatörde.** SOL/USDT:USDT
long, borsada TP'den kapandı. Defter iki hatayla yazdı: giriş istenen fiyattan
(112.09, gerçek **112.58** — denetim #79) ve çıkış, borsa zaten kapattıktan 57 sn
sonra motorun gönderdiği ve `110017` ile **reddedilen** kendi emrinden (denetim
#81/#82). Kayıtlı +1.135R / +$2.18; borsanın söylediği **+0.71R / +$1.63**. Kill
kriterleri işlem sayısı ve CI üstünden işlediği için bu işlem ya düzeltilerek ya
da dışlanarak sayılacak — yanlış haliyle değil.

**2026-09-21 15:53 UTC — PROTOKOL YENİDEN BAŞLATMA.** Operatör kararıyla hesap ve üç
motorun kayıtları sıfırlandı (borsa düzleştirildi, USDT 3000, her motora $1000).
Sebep: ilk işlemin kaydı yanlıştı (yukarıda), sermaye muhasebesi paylaşılan
cüzdanda bozuktu (denetim #84) ve fast'in $300'ü hiç fonlanmamıştı. Değişen:
**tahsis $300 → $1000**; kill kriterindeki "−%15" artık $1000 tahsise göre. Sıfırlama
öncesi 2 işlem (SOL TP, açık SOL — sıfırlamada piyasadan kapatıldı) **analize
girmez**; yedek `runtime-fast/backups/pre-reset-20260921-155301.db`. Strateji
parametreleri değişmedi; H17 bu andan itibaren sayılır.

**2026-09-21 — altyapı değişikliği: sembol kiralama.** Aynı hesapta ikinci bir perp
motoru (scalp) başlıyor ve one-way mod sembol başına tek pozisyon tutuyor. fast
artık başka motorun tuttuğu sembolü atlıyor (`claimed_by_other_engine`). Bu,
stratejinin kuralını değil hangi olayların ona *ulaşabildiğini* değiştirir. Etkisi
ölçülebilir kalsın diye her atlama `scans`'e sebebiyle yazılıyor; analiz, bu
satırlar hariç tutularak ve dahil edilerek iki kez yapılacak.

**2026-09-22 06:24 UTC — PROTOKOL YENİDEN BAŞLATMA (ikinci).** Hesap ve üç motorun
kayıtları yeniden sıfırlandı (denetim #94, #96: restart sermaye muhasebesi). fast bu
aralıkta 17 barda **hiç işlem açmadı** (772 satır `below_threshold`, 103 `no_setup`),
yani kaybolan H17 verisi yok. Strateji parametreleri değişmedi; H17 bu andan
itibaren sayılır. Yedek `runtime-fast/backups/pre-reset-20260922-092458.db`.

**2026-10-11 — 47 işlemde ara durum (kural 50'de yargı istiyor, yargı değil).**
2026-09-22 06:24 → 2026-10-11 20:10 UTC: 47 işlem, Σpnl −$121.71, ortalama
**−0.349R**, bootstrap %95 CI **[−0.583, −0.095]R** (işlem başına getiri −%1.32,
CI [−2.18, −0.41]). Long 32 / short 15, ikisi de ≈ −0.34R; rejim ayrımı aynı
(trend_up 32 → −0.355R, trend_down 15 → −0.334R) — yani kâr düşen rejimde de yok.
Ön-kayıtlı beklenti −%0.05 idi; gözlenen çok daha kötü.

**Havuzlama kararı (sonraki işlemler görülmeden yazıldı):** 2026-10-12 sıfırlaması
yalnız muhasebeyi sıfırlıyor; strateji parametreleri **değişmedi**, bu aralıkta
yapılan düzeltmeler (#98 delist, #99–#101 açılış muhasebesi, #102 short stop etiketi)
hiçbir giriş ya da çıkış kararına dokunmuyor. Bu yüzden 50 işlemlik yargı **bu 47
işlem + sıfırlama sonrası ilk 3 işlem** üzerinden verilir; 47'nin ham verisi
`runtime-fast/backups/pre-reset-20261012-*.db`'de. Kural değişmedi: CI tamamen sıfırın
altındaysa **kapat**. 3 işlemin bu CI'yi sıfırın üstüne taşıması pratikte imkânsız;
bunu şimdi yazıyorum ki 50. işlemde "biraz daha bekleyelim" denmesin.

**Etiket düzeltmesi (#102).** `_close_from_venue_fill` stop'un trail edip etmediğini
`current <= initial` ile okuyordu — short için ters. 11 short `initial_stop` yazılmış,
en iyisi −0.31R: bunların bir kısmı aslında trailing stop'tu. R ve pnl doğru; yalnız
`exit_reason` dağılımı short'ta yanlış.

### Neden bu bir deneme sayılıyor ama veri madenlenmiş değil

Hipotezin **seçimi** arşive bakarak yapıldı, o yüzden DSR'a bir deneme olarak
giriyor (16 → 17). Ama **testi** ileriye dönük, daha önce hiç sorgulanmamış veri
üzerinde: farklı enstrüman (perp), farklı evren (30 kontrat, 12'si spot evreninde
yok), farklı yön (short hiç işlem görmedi). `hypotheses.md`'nin "taze veri" çıkış
yolu tam olarak bu.

## 14. deneme: kurulum kapısı gerçekten gerekliymiş

Açık hipotez #4 aylardır bekliyordu: kuyruk analizi "üst desil" demişti, "üst
desil VE bir kurulum tetiklensin" dememişti. Ve kapı ezici bir filtre — skoru
geçen 17.909 adayın 17.749'u (%99,1) burada ölüyor. Kaldırılınca:

| | kurulum şart | kapı kapalı |
|---|---|---|
| işlem | 103 | **367** |
| işlem/ay | 2,9 | **10,2** |
| kazanma oranı | %17,5 | %22,6 |
| expectancy | +0,195R | +0,158R |
| profit factor | **1,19** | 1,05 |
| getiri | **+%6,04** | +%0,77 |
| maks düşüş | **%15,8** | %20,9 |
| maruziyet | %29,3 | %63,4 |

Kapı 1 kararı acımasız:

| kontrol | kurulum şart | kapı kapalı |
|---|---|---|
| işlem ≥ 50 | ✅ 113 | ✅ 382 |
| PF ≥ 1.2 | ✅ 1.42 | ❌ 1.18 |
| maks düşüş ≤ %20 | ✅ %11.6 | ❌ %20.9 |
| DSR | ❌ | ❌ |
| CPCV çoğunluk + | ✅ %55.6 | ✅ %60.0 |
| PBO ≤ 0.5 | ✅ **0.33** | ❌ **0.67** |
| | **5/6** | **2/6** |

PBO'nun 0.33'ten 0.67'ye fırlaması belirleyici: kapı olmadan, içeride en iyi
görünen konfigürasyon dışarıda medyanın altında kalıyor — klasik aşırı uyum
imzası.

**Bir çelişki ve dürüst yorumu.** Kapı kapalıyken `ranked_entry` işlemleri
ort. +0,214R yaparken kurulum tetiklenenler −0,356R yaptı. Bu ilk bakışta
"kapı zararlı" gibi okunuyor. Ama iki koşunun örneklemi karşılaştırılabilir
değil: kapı kapalıyken maruziyet %63'e çıkıyor, portföy sürekli dolu oluyor
ve kurulum tetikleyen adaylar farklı anlarda/hiç giremiyor. Kapı 1 bütünsel
kararı veriyor ve o karar net.

**Sonuç: kapı kalıyor.** Ve kullanıcının "günde en az bir işlem" hedefi bu
yoldan karşılanamıyor — ayda 2,9'dan 10,2'ye çıkmak mümkün, ama bedeli
stratejinin ölçülmüş kalitesi.

## 15. Akış verisi taraması: kısa vade kapandı

Faz 3'ün tek gerçek umudu fiyattan türemeyen bir sinyaldi. Taker alım/satım
dengesizliği (`scripts/flow_screen.py`, Binance kline 10. alan, 8 sembol ×
12 ay × 63.027 gözlem):

| | isabet |
|---|---|
| taban | %29,9 |
| imbalance üst %20 | %30,8 |
| imbalance üst %10 | %30,7 |
| imbalance üst %5 | %30,1 |
| imbalance üst %1 | **%28,4** ← tabandan kötü |
| **gereken** | **%43,3** |

Lift **+0,8 puan**, gereken **+13 puan**. Üstelik monotonik değil: en uç
yüzdelik tabandan kötü. 24 bar ileri getiriyle korelasyon **+0,011**.

Fiyat indikatörlerinin en iyisi (+2,6 puan) bundan daha güçlü. Yani elimizdeki
tek fiyat-dışı bilgi kaynağı da bu ufukta boş.

**Kısa vadeli al-sat fikri, kanıtla kapandı.** Deneme sayılmadı çünkü strateji
koşulmadı — bu bir indikatör taraması, funding polaritesiyle aynı kategori.

## 12. deneme: kısmi kâr alma — neden reddedildi

Hedeflediği sorun gerçekti ve **onu çözdü**: tam 0.00R'de biten işlem 31'den
3'e düştü (1.0R seviyesinde). Ama toplam sonuç kötüleşti.

| | kapalı | 1.0R | 1.5R | 2.0R |
|---|---|---|---|---|
| profit factor | **1.19** | 1.12 | 1.07 | 1.04 |
| expectancy | **+0.195R** | +0.099R | +0.109R | +0.078R |
| getiri | **+%6.04** | +%2.60 | +%1.15 | −%0.23 |
| Sharpe | **0.23** | 0.15 | 0.09 | 0.03 |
| maks düşüş | %15.8 | **%12.8** | %12.8 | %13.8 |
| 0.00R biten | 31 | **3** | 11 | 22 |

Sebep tek bir tabloda görünüyor (1.0R seviyesi):

| | işlem | toplam pnl (kapalı) | toplam pnl (1.0R) | fark |
|---|---|---|---|---|
| MFE 3R+ görenler | 21 | **+460.58** | +352.07 | **−108.51** |
| diğerleri | 82 | −385.47 | −305.80 | +79.67 |
| **net** | 103 | **+75.11** | +46.27 | −28.84 |

**Vasat işlemlerde $80 kurtarıyor, iyi işlemlerde $108 veriyor.** Çünkü +1R'de
yarısını satmak, sonradan 3R+'ye koşacak pozisyonu da yarıya indiriyor — ve o
21 işlem kârın tamamını taşıyor.

Bu, conviction-çöküş çıkışıyla aynı ders: **kuyruğu kesen her şey pahalı.**
Sezgi ("kârı geri verme") makul, veri aksini söylüyor.

**Karar: mekanizma kuruldu, test edildi, varsayılan kapalı.** Ama bu bir
risk/getiri takası — düşüşü %15.8'den %12.8'e indiriyor. Daha düz bir eğri
karşılığında getiri vermeye razı olan biri için savunulabilir; "daha iyi" değil,
**farklı**.

## 10. deneme: kurulu kırılım girişi — neden reddedildi

Sabit arşivle (`--to 2026-09-01`) temiz A/B, aynı veri, aynı kod, tek fark bayrak:

| | kapalı | açık |
|---|---|---|
| işlem | 104 | 106 |
| profit factor | 1.21 | **1.40** |
| expectancy | +0.168R | **+0.228R** |
| getiri | +5.24% | **+10.47%** |
| Sharpe | 0.24 | **0.41** |
| maks düşüş | 11.6% | 11.7% |

Yüzeyde ikna edici. Dağılıma bakınca değil:

| | işlem | toplam R | toplam pnl |
|---|---|---|---|
| kurulu girişler | **5** | +10.28 | **+82.12** |
| diğer hepsi | 101 | +13.89 | +34.49 |

**5 işlem, 101 işlemin iki katından fazla kâr üretti** ve bunun içinde LINK
(+35.07) ile ONDO (+40.47) tek başına %92'yi taşıyor. Kalan üçü: +11.50, −0.09,
−4.83. Yani ölçtüğüm şey bir mekanizma değil, iki işlem.

Bootstrap CI zaten sıfırı içeriyor: [-0.189, +0.708]R.

**Karar: mekanizma kuruldu, test edildi, varsayılan kapalı.** Açmak için gereken
şey ayar değil taze veri — paper/demo ileri testinde kaç kurulu giriş dolduğu ve
dağılımın yoğunlaşmaya devam edip etmediği. Bu tam olarak "bozuk vol-scaler'ın
şanslı bir alt küme seçmesi" vakasının aynısı olabilir; tek farkı bu sefer
mekanizma bozuk değil, örneklem küçük.

Deneme sayılıyor çünkü **bu veri setine bir soru daha soruldu.** Cevabın "hayır"
olması bedeli düşürmüyor — DSR denemeyi fiyatlıyor, sonucu değil.

## Blok collapse tamiri: doğru tamir, ölçülen etki sıfır

`_score_category` kategori içindeki yönlü indikatörlerin **ham ortalamasını**
alıyordu, blok temsilcisini değil — değişmez kural 3'ün ihlali, funding
polaritesiyle aynı kategoride bir hata. Düzeltildi (0 deneme, tamir).

**Ölçülen etki: yok.** PF 1.41 → 1.41, expectancy +0.364R → +0.362R.

Sebebini araştırınca beklediğimden ilginç çıktı: `vwap_distance`'ın blok
arkadaşları (`rsi`, `bb_percent_b`, `donchian_pos`) **başka kategorilerde**.
`flow` kategorisindeki üç yönlü indikatör (`obv_slope`, `mfi`, `vwap_distance`)
zaten üç ayrı blokta. Yani kategori-içi collapse'ın yakalayacağı bir şey yoktu.

**Asıl sızıntı kategorizasyonda.** `vwap_distance` fiyatla ρ=0.96 ve `flow`
sayılıyor; `flow_confirmation`'ın 15 puanı böylece kısmen fiyatın kopyasına
gidiyor. Bunu düzeltmek sonucu değiştirir, yani bir denemedir — sessizce
yapılmadı, buraya yazıldı. Açık hipotez listesine eklendi.

Tamirin kendisi yine de doğru: iki aynı-blok indikatör bir gün aynı kategoriye
düşerse, o sessiz ihlal geri gelirdi.

## Gate'ler neden `allowed` filtresinden muaf

`rel_volume` hiçbir rejimde kuyruk seçimini geçmiyor ama hâlâ `flow` skorunu
0.75x–1.25x ölçekliyor. İlk bakışta tutarsız; ölçünce değil.

Kuyruk analizi **yönlü** bir test: her barda sembolleri ileri getiriye göre
sıralayıp üst desili alıyor. Bir gate yön iddia etmiyor — "arkasında katılım var
mı" diye soruyor. Yapmadığı bir şeyin testinde başarısız olduğu için elemek,
yanlış kanıtla eleme olurdu.

Yine de ölçtüm: gate'leri `allowed`'a tabi kılmak PF 1.41 → 1.38, expectancy
+0.362 → +0.339R, getiri +11.31% → +10.76%. Hem gerekçe zayıf hem sonuç kötü.
Geri alındı, gerekçe koda yazıldı.

## Conviction-çöküş: üç aşamalı bir hata, ve benim kendi ölçümüme düşmem

Bu maddenin geçmişi, bu defterin var olma sebebinin en iyi örneği. **Üç ayrı hata,
üçü de benim.**

**1. İlk tamir no-op'tu.** `conviction_now=None` bağlandı, sonuç değişti sanıldı.
Bot durdurulup sabit arşivle ölçülünce açık ile kapalı **birebir aynı** çıktı.

**2. Sebep, tamirin içine koyduğum korumaydı.** Değişmez kural 10'a uymak için
`no_trade_reason is not None` olan her skoru "fikrim yok" saydım. Ama
`below_threshold` bir engelleme değil — skorlamadan *sonra* atanan **gerçek bir
sayı**. Ve çöken bir tez tanımı gereği giriş eşiğinin altındadır. Yani korumam,
çıkışın okuması gereken skorların tam olarak hepsini atıyordu. Ölçüldü: **391 bar
çöküş eşiğinin altında skorlandı, 391'i de atıldı.**

**3. İyileşmeyi yanlış atfettim.** "PF 1.20→1.41, PBO 1.00→0.33" diye yazdım.
İki koşu arasında arşiv büyümüştü ve tamir zaten çalışmıyordu. Sayılar bugün de
doğru (sabit ölçüm PBO 0.33 veriyor) ama **sebep tamir değil, daha fazla veri**.

Bu üçüncüsü en can sıkıcı olanı, çünkü bu dosyada zaten şu yazıyordu: *"iyileşen
bir tamir daha fazla şüphe hak eder, çünkü kimse hoş sürprizi soruşturmaz."*
Kendi yazdığım uyarıyı kendi tamirime uygulamadım. Kural artık koda bağlı
(değişmez 49): ölçüm için bot durdurulur ve tarih pinlenir.

### Katman düzgün çalışınca ne oldu

Kötüleşti:

| | çöküş ölü | çöküş canlı (32) |
|---|---|---|
| işlem | 103 | 116 |
| profit factor | **1.23** | 1.12 |
| expectancy | **+0.179R** | +0.100R |
| getiri | **+%5.75** | +%2.32 |
| Sharpe | **0.25** | 0.13 |
| yakalama | **%8.7** | %5.6 |
| çöküş çıkışı | 0 | 24 |

24 çıkışın yaptığı şey, hâlâ çalışan pozisyonları kesmek. Ortalama tutuş 111 →
87 bara düştü ve yakalama oranı da düştü — yani erken çıkmak kârı korumadı,
azalttı.

**Karar: mekanizma doğru ve test edildi, seviye onaylanmadı.**
`collapse_threshold: 0.0`. Açmak ayar değil, ön-kayıtlı bir hipotez.

Bootstrap CI zaten sıfırı içeriyor: [-0.244, +0.684]R.

## Ölçekleyici tamirinin bedeli

Volatilite hedefi düzeltilince sonuçlar **kötüleşti**: $1000'da PF 1.34 → 1.16,
getiri +%3.66 → +%0.95. $100'da +%2.17 → −%10.31 (28 → 70 işlem).

Önceki 1.34'ün bir kısmı yapaydı. Bozuk ölçekleyici pozisyonları küçültüp
min_notional'a takarak farkında olmadan bir alt küme seçiyordu, ve o alt küme
şanslıydı. Bu, bir bileşenin bozukluğunun *iyi görünen* bir sonuç üretmesinin
ders niteliğinde örneği — ve tam da altyapının yakalamak için kurulduğu şey.

Düzeltme sonucu kötüleştirdi, ölçümü güvenilir yaptı. Doğru takas.

## İstatistiksel bütçe tükendi

9 denemeden sonra Deflated Sharpe'ın beklenen maks Sharpe eşiği ~1.5. Gözlenen
0.24. Bu veri setinde (21 sembol × 3 yıl) **daha fazla ayar yapıp sonuca
güvenmek matematiksel olarak mümkün değil** — CPCV (%44 pozitif yol) ve
PBO (0.67) aynı şeyi bağımsız söylüyor.

Bu kodun yanlış olduğu anlamına gelmiyor. Aynı veriye kaç kez soru sorulduğunun
bir bedeli var ve o bedel ödendi.

**Buradan çıkış yolu ayar yapmak değil, taze veri:**
1. **İleri test (paper).** Maliyeti sıfır, ürettiği veri madenlenmemiş. Kapı 1
   parayı korumak için var; paper koşusu para riske atmıyor. Doğru hareket:
   canlı veriyle ileri gitmek ve kararı out-of-sample sonuçlara bırakmak.
2. **Daha fazla sembol/geçmiş.** Bybit spot 2021-07'de başlıyor; başka borsa
   verisi eklemek örneklemi gerçekten büyütür.
3. **Tek seferlik test edilen yeni hipotez sınıfı** — ayarlanan değil.

## Açık hipotezler

1. **Örneklem büyüklüğü asıl darboğaz.** Denenen hiçbir konfigürasyon 25 işlemi
   geçmedi; hiçbir istatistik bu sayıda anlamlı değil. Her ayar denemesi bir DSR
   denemesi olduğu için ayar aramak da pahalı. Asıl soru: bu tasarım ölçülebilir
   sayıda işlem üretebilir mi, yoksa seçicilik ile ölçülebilirlik birbirini mi
   dışlıyor?

2. **Sermaye kısıtı evreni sessizce değiştiriyor.** `risk/capital.py` ölçtü:
   $100'da risk-doğru boyut ancak stop **%13'ün altındaysa** borsanın $5
   minimumunu geçiyor. Yani volatil semboller hiç işleme girmiyor ve canlı evren
   backtest evreninden farklı. $300 bu sınırı %39'a çıkarıyor. Ölçülmeli: bu
   filtre edge'i mi kesiyor, gürültüyü mü?

3. **Stop kazananlar için gereğinden geniş.** `exit_optimizer` kazananların p90
   MAE'sini 0.175R buldu — stop 3.33 kat geniş. Ama n=1 kazanan; uygulanamaz.
   Örneklem büyüdüğünde tekrar ölçülmeli.

4b. **`vwap_distance` yanlış kategoride olabilir.** Fiyatla ρ=0.96 ama `flow`
   sayılıyor, yani "hacim arkasında mı" sorusuna fiyatın kendisi cevap veriyor.
   `flow_confirmation` 15 puan taşıyor ve bugün büyük ölçüde fiyatı ikinci kez
   oyluyor olabilir. Kategoriyi değiştirmek işlem popülasyonunu değiştirir →
   bir deneme. **Kasıtlı olarak çalıştırılmadı.**

4. **Kurulum kapısı gereksiz olabilir — TEST EDİLDİ, kapı kalıyor (deneme 14).** Kesitsel modda
   huni `no_setup_at_entry: 17556` gösteriyor: sıralama üst desili geçiriyor,
   sonra ayrıca bir kurulumun tetiklenmesi gerekiyor. Ama ölçüm "üst desil"
   dedi, "üst desil VE bir kurulum tetiklensin" demedi. Bu, kesitsel düzeltmeyle
   aynı kategoride bir uyumsuzluk olabilir.

   **Kasıtlı olarak çalıştırılmadı:** 10. deneme olurdu ve bütçe zaten tükenmiş.
   Taze veri üzerinde ilk test edilecek hipotez bu olmalı.

5b. **Kar-al seviyesi — mekanizma hazır, seviye ARANMADI.** `exits.target_atr_mult`
   yazıldı ve test edildi ama varsayılanı 0.0, yani kapalı. Bir seviye seçmek
   serbest parametre aramaktır ve o parametreyi n=17'lik MFE arşivinden seçmek,
   aynı veriye bir soru daha sormaktır. Excursion raporu `suggested_target_r`
   6.49 diyor — ama aynı rapor `capture` 0.109 diyor, yani kazananlar
   ortalamada MFE'lerinin %11'ini alıyor. İkisi birlikte "hedef koy" demiyor,
   "trail çok gevşek olabilir" diyor. Örneklem büyüyene kadar açılmayacak.

   Ayrıca **borsaya konamaz**: Bybit spot'ta stop base varlığı kilitliyor,
   ikinci reduce-only emir `170131` alıyor (ölçüldü). Yani hedef yalnız bot
   çalışırken geçerli — bu da onu stop'tan farklı bir güvenlik sınıfına koyuyor.

5. **Rejim kapısı çok dar olabilir.** Haftalık rejimde TREND_UP barların sadece
   %7'si; günlükte %14-17. RANGE rejiminde de long'a izin veriliyor ama
   `range_dip` kurulumu hiç tetiklenmedi. Ölçülmeli: rejim kapısı edge mi
   koruyor, örneklemi mi öldürüyor?

## Kural

Hipotez buraya yazılır, backtest'e çevrilir, CPCV'den geçerse koda girer.
Doğrudan config değiştirip "daha iyi görünüyor" demek, tam olarak DSR'ın
cezalandırdığı şeydir.
