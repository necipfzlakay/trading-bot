# Kısa vadeli al-sat: fizibilite ölçümü ve plan

**Tarih:** 2026-09-19 · **Branch:** `feat/capital-recycling`
**Soru:** "Bir işleme gir, %1-2-4-10 ne yakalarsan al ve çık. Günde en az bir işlem.
Günlük %1-10 portföy artışı." Bu mekanizma kurulabilir mi?

**Cevap: hayır, ve artık üç ayrı ölçümle kapandı.** Mekanizma kurulabilir ama
para kaybeder, ve kaybın büyüklüğü önceden hesaplanabilir. Fiyat indikatörleri,
sembol seçimi ve akış verisi — üçü de aynı duvara çarptı: edge var, komisyonun
sekizde biri kadar. Aşağıdaki sayıların hepsi bu repodaki
veriyle, 21 sembol × 3 yıl × 26.325 saatlik bar üzerinde ölçüldü.

---

## 1. Ölçüm: bariyer testi

Her saatlik barın kapanışında girildiğini varsay. Sonraki 24 barda (1 gün) fiyat
önce hedefe mi, önce stop'a mı değiyor? Aynı bar ikisine de değerse **stop
sayıldı** (değişmez kural 40 — kötümser seçim).

| hedef | stop | isabet | kayıp | zaman aşımı | isabet oranı | beklenen (net) |
|---|---|---|---|---|---|---|
| +1% | −1% | 39.505 | 43.865 | 1.133 | 47,4% | **−0,352%** |
| +1% | −0,5% | 25.626 | 58.475 | 402 | 30,5% | **−0,343%** |
| +2% | −1% | 25.980 | 55.038 | 3.485 | 32,1% | **−0,336%** |
| +2% | −2% | 37.051 | 38.892 | 8.560 | 48,8% | **−0,344%** |
| +3% | −1,5% | 23.353 | 51.441 | 9.709 | 31,2% | **−0,384%** |
| +5% | −2,5% | 16.300 | 41.524 | 26.679 | 28,2% | **−0,564%** |
| +10% | −5% | 6.047 | 20.529 | 57.927 | 22,8% | **−0,799%** |

Hepsi negatif, ve kayıp neredeyse tam olarak **işlem maliyeti kadar**. Yani fiyat
bu ufukta adil bir yazı-tura gibi davranıyor: isabet oranları teorik adil oranın
(stop/(hedef+stop)) çeperinde. Kaybedilen şey edge değil, **komisyon**.

## 2. Sinyalimiz bunu değiştiriyor mu?

Hedef +2% / stop −1% için. Adil oran %33,3; başabaş için gereken %43,3.

| | n | isabet | beklenen |
|---|---|---|---|
| taban (tüm barlar) | 506.926 | 31,9% | −0,341% |
| `price_slope` üst desil | 50.545 | **34,5%** | −0,267% |
| `adx_di` üst desil | 50.651 | 33,5% | −0,295% |
| `ema_stack` üst desil | 108.139 | 33,4% | −0,299% |
| `rsi` üst desil | 50.672 | 33,3% | −0,302% |
| `supertrend` üst desil | 244.130 | 32,5% | −0,325% |

**Sinyal gerçek ama 8 kat küçük.** En iyisi isabeti %31,9 → %34,5'e taşıyor:
**+1,2 puan**. Maliyeti karşılamak için **+10 puan** gerekiyor.

Sembol bazında: 21 sembolden **yalnız 2'si** adil oranın üstünde (VVV +1,2 puan,
AAVE +0,2 puan). Ortalama **−1,38 puan**.

## 3. Maliyet tarafında yapılacak bir şey var mı? Hayır

Bybit spot'ta **maker = taker = %0,1** (borsadan okundu, indirim yok).

| senaryo | gidiş-dönüş | gereken isabet | elimizdeki |
|---|---|---|---|
| market emir + kayma | %0,30 | 43,3% | 34,5% |
| limit emir, kaymasız | %0,20 | 40,0% | 34,5% |
| **maliyet sıfır (imkânsız)** | %0,00 | 33,3% | 34,5% ✅ |

Sadece **maliyet tamamen sıfır olsaydı** işlem başına +0,038% kazanırdık. Yani
bütün edge, komisyonun içinde eriyor. Bu bir uygulama sorunu değil, aritmetik.

## 4. Hedefin büyüklüğü

Günlük %1 bileşik = yılda **+%3.678**. Günlük %10 = yılda 10^14 kat.
Referans: bu evrende BTC al-tut 3 yılda +%205.

Ölçülen gerçek: işlem başına −0,34%. Günde bir işlemle yılda ≈ **−%71**.

Yani istenen mekanizma kurulursa, yönü hedefin tam tersi olur — ve bunu tahmin
edebiliyoruz, deneyerek öğrenmemize gerek yok.

---

## 5. Ama asıl istek yapılabilir

Kullanıcının somut şikâyeti üç maddeydi:

1. **"Para kilitli kalıyor, yeni işlemleri kaçırıyoruz"**
2. **"Realize etmiyoruz"**
3. **"10 günde 1 işlem bir trade botu değil"**

Bunların **hiçbiri kısa vadeli edge gerektirmiyor.** Ölçüm:

| engel | pay |
|---|---|
| `below_threshold` | 45,4% |
| `regime_veto` | 43,2% |
| `max_positions` | **4,2%** ← "para kilitli" durumu |
| `already_in_position` | 2,4% |
| `correlation_full` | 0,7% |

Ve skoru geçen **17.909 adaydan 17.749'u (%99,1) kurulum tetiklenmediği için**
öldü. Piyasa maruziyeti %29 — para zaten nakitte oturuyor, **pozisyonlarda
kilitli olduğu için değil, hiçbir şey filtreleri geçemediği için.**

Yani 113 işlemin sebebi uzun ufuk değil, **aşırı sıkı filtreler**. Edge'in
bulunduğu ufku değiştirmeden işlem sayısı artırılabilir.

---

## Plan

### FAZ 1 — Realize etme (mekanizma; seviyeler birer deneme)

Kullanıcının 2. maddesi. Şu an 1–3R arası 34 işlemden **sıfır** alıyoruz:
MFE 1–2R gören 29 işlem ortalama −0,01R, 2–3R gören 5 işlem +0,00R. 33 işlem
(%29) tam breakeven'da kapanıyor çünkü `breakeven_at_r: 1.0` stop'u girişe
taşıyor, sonra günlük ATR'den hesaplanan geniş trail yetişemiyor.

- `exits.scale_out`: +XR'de pozisyonun bir kısmını kapat, kalanı koşsun
  → 3R+ kuyruğunu (tüm kârın kaynağı) öldürmez, ortadaki boşluğu doldurur
- Kısmi çıkış `Position.quantity`'yi azaltmalı, borsa stop'u yeniden
  boyutlandırılmalı (`_sellable` kısıtı geçerli), R hesabı orijinal 1R'ye göre
  kalmalı
- Backtest + paper + canlı simetrik
- **Varsayılan kapalı.** Seviye = serbest parametre = ön-kayıtlı hipotez

### FAZ 2 — SONUÇLANDI: kapı yerini hak ediyor

Hipotez #4 test edildi. Kurulum kapısı kaldırılınca işlem 103 → **367** (ayda
2,9 → 10,2), ama Kapı 1'de **5/6 → 2/6** ve PBO **0,33 → 0,67**. Aşırı uyum
imzası geri geldi. Detay: `research/hypotheses.md`, deneme 14.

Hipotez #5 (rejim kapısı) ölçüldü ve **zaten dar değil**: `regime_allows`
hem TREND_UP hem RANGE'e long izni veriyor, yani barların %55,5'i. Engellenen
%44,5 = TREND_DOWN (%16,5) + UNKNOWN (%19,9) + HIGH_VOL (%8,1). Bunları açmak
"düşen bıçağı yakala" demek; ayrı bir hipotez olarak dursun, önceliği yok.

### FAZ 3 — SONUÇLANDI: yeni bilgi de yok

`scripts/flow_screen.py` ile ölçüldü (Binance kline 10. alan `takerBuyBaseVolume`,
8 sembol × 12 ay × 63.027 gözlem). Aynı bariyer testi, aynı maliyet:

| | isabet |
|---|---|
| taban | %29,9 |
| imbalance üst %20 | %30,8 |
| imbalance üst %10 | %30,7 |
| imbalance üst %1 | **%28,4** ← tabandan kötü |
| **gereken** | **%43,3** |

Lift **+0,8 puan**, gereken **+13**. 24 bar ileri getiriyle korelasyon **+0,011**.
Üstelik monotonik bile değil — en uç yüzdelik tabandan kötü performans veriyor.

Fiyat indikatörlerinin en iyisi (+2,6 puan) bundan güçlü. Yani **elimizdeki tek
fiyat-dışı bilgi kaynağı da bu ufukta boş.**

Kalan iki yol ve neden takip edilmiyorlar:

- **Gün içi veri (5m/15m).** Test edilebilir ama maliyet tablosu zaten cevabı
  veriyor: 15m'de medyan hareket %0,24, gidiş-dönüş maliyet %0,30 — maliyet
  hareketten büyük. Daha kısa ufuk oranı **kötüleştirir**, iyileştirmez
- **Daha volatil evren.** Hareket/maliyet oranını iyileştirir ama spread ve
  likidite cezaları devreye girer. Ölçülen spread aralığı zaten 0,01–2,7bps,
  yani bu evren likidite tarafında zaten iyi; daha volatil olan daha ince olur

**Karar: kısa vadeli al-sat kolu kapatıldı, kanıtla.** Üç farklı sinyal sınıfı
(fiyat indikatörleri, sembol seçimi, akış verisi) aynı duvara çarptı: edge var
ama komisyonun sekizde biri kadar.

---

## Bu plan neyi kasıtlı olarak yapmıyor

- **Günlük %1-10 hedefini kabul etmiyor.** Ölçülen beklenti işlem başına
  −0,34%; hedef +1%. Aradaki fark ayarla kapanacak türden değil
- **Filtreleri şimdi gevşetip mevcut veride ölçmüyor.** 11 deneme sonrası bu
  sette arama yapmak matematiksel olarak sonuçsuz — ve bugün bu hatanın bedeli
  bir kez ödendi (bkz. denetim #47–#49)
- **Ana motoru bozmuyor.** `main` stabil kaldı; bu iş `feat/capital-recycling`
  branch'inde
