# İndikatörler — ne ölçüyorlar, hangisi işe yarıyor

22 indikatör var. Hepsi `src/tbot/indicators/ta.py` içinde elle yazıldı (dış TA
kütüphanesi yok) ve her biri referans değerlerle + "geleceği görebiliyor mu"
testiyle doğrulandı.

Bu doküman iki şeyi ayırıyor: **indikatör ne yapıyor** (tanım) ve **bu veride ne
yaptı** (ölçüm). İkincisi `runtime/reports/{ic,tails,redundancy,polarity}.md`
dosyalarından geliyor ve `scripts/ic_report.py` ile tekrar üretilebilir.

---

## Temel ayrım: oy verenler vs kapı tutanlar

| rol | soru | örnek |
|---|---|---|
| **DIRECTIONAL** | "hangi yöne?" | RSI, MACD, Supertrend |
| **GATE** | "işlem yapılabilir bir ortam mı?" | Squeeze, volatilite yüzdelik dilimi |

Bollinger bant genişliği yön hakkında hiçbir şey söylemez — sadece "sıkışma var"
der. Bunu oy olarak saymak, "hava kapalı" ile "kuzeye git" demeyi karıştırmaktır.
Kapılar skoru ölçekler (0.75x–1.25x), yön seçmez.

---

## 1. Trend — yön ve trendin varlığı

### `ema_stack` — 20/50/200 EMA dizilimi
Üç üssel ortalamanın sırası + fiyatın konumu. Tam puan için hem dizilim doğru
(20 > 50 > 200) hem fiyat en üstte olmalı. Fiyatın geri düştüğü bir dizilim,
bozulmakta olan bir trenddir.

### `adx_di` — trend gücü, yönle işaretlenmiş
ADX trendin **gücünü** ölçer, yönünü değil — çöküşte de yükselir. Tek başına
kullanmak bir çöküşü "al" diye puanlamak olur. Bu yüzden DI farkının işaretiyle
çarpılıyor: `güç × yön`.

### `supertrend` — ATR bantlı rejim bayrağı
Bantlar fiyata doğru sıkışır, sadece yön değişiminde genişler. Çıktısı +1 / −1.

### `donchian_pos` — N-bar kanalındaki konum
**Kritik detay:** kanal mevcut barı **dışlıyor** (`shift(1)`). Mevcut barı içeren
bir kanal o bar tarafından asla kırılamaz — backtest'te kırılım hiç tetiklenmez,
canlıda her zaman tetiklenir. Aynı formül, zıt davranış. Bu sessiz hatanın en
sık yapıldığı yer.

### `ichimoku` — bulut + tenkan/kijun
Senkou spanları **ileriye** kaydırılır (tasarımı gereği), yani bar *i*'deki bulut
*i−26*'daki veriden hesaplanmıştır — nedensel. Chikou span (geriye kayan) kasıtlı
olarak **yok**: mevcut barda kullanılamaz, kullanmak lookahead olur.

### `price_slope` — volatiliteye ölçeklenmiş regresyon eğimi
Bar başına yüzdesel değişim olarak ifade edilir, böylece $0.50 ve $70.000'da
işlem gören semboller karşılaştırılabilir.

---

## 2. Momentum — hareketin gücü / tükenişi

### `rsi` — Wilder RSI(14)
**Momentum olarak** okunuyor: 50 üstü yükseliş. Ortalamaya dönüş olarak
okunması gerekip gerekmediği rejime bağlı — bu varsayılmıyor, rejim-kırılımlı IC
testiyle ölçülüyor.

Uygulama notu: Wilder yumuşatması `alpha=1/period` (standart EMA'nın
`2/(n+1)`'i değil) ve ilk 14 değerin basit ortalamasıyla tohumlanıyor. Bu iki
detay yaygın olarak yanlış yapılıyor; doğrusu grafik platformlarıyla uyuşan
versiyon.

### `macd` — ATR'ye ölçeklenmiş histogram
Ham histogram sembolden sembole karşılaştırılamaz; ATR'ye bölünce olur.

### `stoch_rsi`, `roc` — RSI'ın stokastiği ve değişim oranı
Daha hızlı, daha gürültülü. IC testi için tutuluyor.

---

## 3. Volatilite — rejim ve sıkışma (çoğu KAPI)

### `bb_percent_b` — Bollinger bandındaki konum (yönlü)
### `squeeze` — Bollinger, Keltner'ın içinde mi (KAPI)
Sıkışma yön değil **birikim** söyler: bir hareket depolanıyor, ama hangi yöne
çözüleceğini söylemiyor. Tek başına giriş sinyali olarak asla kullanılmıyor.

### `vol_regime` — realized vol yüzdelik dilimi (KAPI)
Ham volatilite sayısı semboller arası anlamsız; kendi geçmişindeki yüzdelik
dilimi anlamlı. Yüksek okuma küçülme sebebidir, taraf seçme sebebi değil.

### `bb_width` — bant genişliği z-skoru (KAPI)

---

## 4. Hacim / akış — hareketin arkasında para var mı

### `obv_slope` — On-Balance Volume eğimi
OBV'nin seviyesi keyfi; sadece yönü anlam taşır.

### `mfi` — Money Flow Index
Fiyat yerine işlem değeriyle ağırlıklandırılmış RSI.

### `vwap_distance` — VWAP'tan ATR cinsinden uzaklık
### `rel_volume` — hacmin kendi medyanına oranı (KAPI)
Medyan, ortalama değil: tek bir likidasyon kaskadı sonraki 20 barın "normal"ini
yeniden tanımlamamalı.

---

## 5. Türev verisi — fiyattan türetilmemiş

Bu grup özellikle değerli çünkü **yapısal olarak bağımsız**. Fiyattan hesaplanan
20 indikatör birbirine benzeyebilir; funding oranı fiyattan hesaplanmıyor.

### `funding` — funding oranı z-skoru
**Ölçümle düzeltilen varsayım.** Yaygın kabul contrarian: yüksek funding =
kalabalık long = squeeze yakın. Bu veride 5 ufkun **5'inde de tersi** çıktı —
yüksek funding daha *yüksek* getiri öngördü. 1d/20 bar: üst desil +%5.31 vs
taban +%2.28, güven aralığı [+%2.49, +%7.70].

Yorum: yüksek funding, insanların içinde kalmak için **para ödediği** bir trendi
işaretler; çok haftalık ufuklarda o trend kopmaktan çok devam etti.

Uyarı: örneklem 2023-2026, genel olarak yükselen piyasa. Uzun bir ayı fazı bunu
contrarian'a geri çevirebilir. `scripts/polarity.py` tek seferlik değil, tekrar
çalıştırılabilir olması bu yüzden.

### `long_short` — hesap long/short oranı
**Contrarian doğrulandı** (5/5 ufuk). Kalabalık long → düşük getiri.

### `oi_change`, `oi_divergence` — açık pozisyon
`oi_divergence` fiyat yönü ile OI değişimini birleştirir. Yükselen fiyat +
yükselen OI = yeni para giriyor. Yükselen fiyat + düşen OI = short kapaması.
**Aynı mum, farklı anlam** — ve fiyat serisindeki hiçbir şey ikisini ayıramaz.

---

## Ölçüm sonuçları

### Bulgu 1: 7 indikatör tek oy bloğuna düştü

4h'de korelasyon matrisi şunları birleştirdi (maks |ρ| = 0.955):

```
rsi · macd · stoch_rsi · roc · bb_percent_b · donchian_pos · vwap_distance
```

Dört farklı kategoriden geliyorlar (momentum, trend, volatilite, akış) — yani
**kategori taksonomisi burada yanılıyor**, korelasyon ölçümü yanılmıyor.

Bu yedisini kullanıp "7 teyidim var" demek, 1 okumanın 7 kopyasına sahip olmak.
18 yönlü indikatör → **12 bağımsız blok**.

### Bulgu 2: IC ile kuyruk analizi çelişti, kuyruk haklı

Tüm barlarda Information Coefficient ≈ −0.02, hiçbir t-istatistiği 2'yi geçmiyor.
Klasik okumayla: *hiçbir indikatör işe yaramıyor*.

Ama üst desilde güçlü pozitif momentum var. Sebep: ilişki **monotonik değil**.
IC tüm dağılımdaki monotonik ilişkiyi ölçer; bot dağılımın ortasında hiç işlem
yapmaz, sadece uçları bekler.

Bunu fark etmeseydim tüm indikatörleri eleyip yanlış sonuca varırdım.

### Bulgu 3: ufuk, indikatör seçiminden daha belirleyici

1d, 20 bar tutuş — üst desil fazlalığı (taban oranın üstü), maliyet düşülmüş:

| indikatör | fazlalık | %95 GA |
|---|---|---|
| `price_slope` | +%5.97 | [+%4.58, +%12.44] |
| `rsi` | +%5.21 | [+%4.70, +%12.33] |
| `mfi` | +%4.67 | [+%4.39, +%11.42] |
| `adx_di` | +%4.51 | [+%3.71, +%11.99] |

Aynı indikatörler 4h/12 barda ~%0.8. **Ufuk değişimi, indikatör değişiminden
daha büyük fark yaratıyor.**

### Bulgu 4: TREND_UP rejiminde etki büyüyor

`rsi` h=20, sadece TREND_UP barlarında: **+%9.34 fazlalık, %65 isabet**. Rejim
kapısı tasarımı veriyle doğrulandı.

---

## Nasıl kullanılıyorlar

```
1d  İZİN     rejim sınıflandırması. Hayır derse alt katmanlar konuşamaz.
4h  KURULUM  konfluens burada aranır, conviction skorunun gövdesi
1h  TETİK    giriş zamanlaması, spread/likidite kontrolü
```

Kural tek yönlü: **üst zaman dilimi altı veto eder, alt üstü asla ezemez.**

Skorlama (0-100):

| bileşen | max | ne ölçer |
|---|---|---|
| rejim uyumu | 25 | izin katmanı bu yönü destekliyor mu |
| indikatör konfluensi | 25 | **bağımsız blok** oyları (indikatör değil) |
| çok zaman dilimi hizalaması | 15 | 1d/4h/1h aynı yönde mi |
| akış teyidi | 15 | hacim var mı |
| türev verisi | 10 | funding, OI, kalabalıklık |
| beklenen R | 10 | benzer geçmiş kurulumların MFE/MAE'si |

Artı cezalar: geniş spread, ince likidite, aşırı ATR.

**En önemli mekanizma:** oylar blok başına sayılır, indikatör başına değil.
Korelasyonlu 12 indikatörün aynı yöne eğilmesi, yüksek sesle söylenmiş **tek**
bir fikirdir.

---

## Tekrar üretmek için

```bash
python scripts/ic_report.py --timeframe 1d   # IC + kuyruk + redundans
python scripts/polarity.py                   # türev sinyallerin yönü
```

Çıktılar: `runtime/reports/ic.md`, `tails.md`, `redundancy.md`, `polarity.md`
