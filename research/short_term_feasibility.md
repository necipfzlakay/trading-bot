# Kısa vadeli al-sat: fizibilite ölçümü ve plan

**Tarih:** 2026-09-19 · **Branch:** `feat/capital-recycling`
**Soru:** "Bir işleme gir, %1-2-4-10 ne yakalarsan al ve çık. Günde en az bir işlem.
Günlük %1-10 portföy artışı." Bu mekanizma kurulabilir mi?

**Cevap:** Mekanizma kurulabilir, **ama ölçüm onun para kaybedeceğini söylüyor** —
ve kaybın büyüklüğü tahmin edilebilir. Aşağıdaki sayıların hepsi bu repodaki
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

### FAZ 2 — Daha fazla işlem, edge'in olduğu ufukta

Kullanıcının 3. maddesi. İkisi de `hypotheses.md`'de zaten yazılı ve
"taze veride ilk test edilecek" diye işaretli:

- **Hipotez #4:** kurulum kapısı gereksiz olabilir. Ölçüm "üst desil" dedi,
  "üst desil VE bir kurulum tetiklensin" demedi. %99,1'i burada ölüyor
- **Hipotez #5:** rejim kapısı çok dar olabilir (TREND_UP barların %7–17'si)

Kaba tahmin: kurulum kapısı kalkarsa bar başına ~0,69 uygun aday × 3 slot ×
medyan 48 bar tutuş → **günde ~1–1,5 işlem**. Kullanıcının hedefini karşılar.

⚠️ Her biri bir deneme ve bütçe tükendi (11 deneme, DSR p=1.000). Bu yüzden
sıra önemli: önce mekanizma, sonra demo'da taze veriyle karar.

### FAZ 3 — Kısa vadeyi mümkün kılacak tek şey: yeni bilgi

Mevcut sinyallerle kısa vade ölü. Onu değiştirebilecek üç şey var, üçü de
**önce ölçüm, sonra kod**:

1. **Akış verisi (CVD / taker imbalance).** Fiyattan türemeyen tek kaynak.
   `hypotheses.md` B1'de duruyor. Binance kline `takerBuyBaseVolume` ile
   2017'ye kadar bedava taranabilir — canlı yola dokunmadan
2. **Gün içi veri.** Arşivde 15m/5m **yok**, sadece 1h ve üstü. Kısa vadeyi
   1h barla test etmek zaten yaklaşık; gerçek test için backfill gerekir
3. **Daha volatil evren.** Günlük medyan hareket %0,21 (BTC) ile %0,72 (FLOCK)
   arasında. Maliyet sabit olduğu için hareketin büyümesi oranı doğrudan
   iyileştirir — ama likidite ve spread cezası devreye girer (A5 verisi artık
   toplanıyor, bu ölçüm için hazır)

**Ayrı bir motor ne zaman mantıklı:** Faz 3 bir edge bulursa. Şu an ikinci bir
motor kurmak, ölçülmüş bir oranda para kaybeden bir makine kurmak olur.
Altyapı hazır (ayrı config + ayrı `runtime/` dizini yeterli), eksik olan
**kod değil, edge.**

---

## Bu plan neyi kasıtlı olarak yapmıyor

- **Günlük %1-10 hedefini kabul etmiyor.** Ölçülen beklenti işlem başına
  −0,34%; hedef +1%. Aradaki fark ayarla kapanacak türden değil
- **Filtreleri şimdi gevşetip mevcut veride ölçmüyor.** 11 deneme sonrası bu
  sette arama yapmak matematiksel olarak sonuçsuz — ve bugün bu hatanın bedeli
  bir kez ödendi (bkz. denetim #47–#49)
- **Ana motoru bozmuyor.** `main` stabil kaldı; bu iş `feat/capital-recycling`
  branch'inde
