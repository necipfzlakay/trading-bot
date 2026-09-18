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

**Kümülatif deneme: 9.** Bir sonraki CPCV koşusunda `--trials 9` geçirin.

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

4. **Kurulum kapısı gereksiz olabilir — HENÜZ TEST EDİLMEDİ.** Kesitsel modda
   huni `no_setup_at_entry: 17556` gösteriyor: sıralama üst desili geçiriyor,
   sonra ayrıca bir kurulumun tetiklenmesi gerekiyor. Ama ölçüm "üst desil"
   dedi, "üst desil VE bir kurulum tetiklensin" demedi. Bu, kesitsel düzeltmeyle
   aynı kategoride bir uyumsuzluk olabilir.

   **Kasıtlı olarak çalıştırılmadı:** 10. deneme olurdu ve bütçe zaten tükenmiş.
   Taze veri üzerinde ilk test edilecek hipotez bu olmalı.

5. **Rejim kapısı çok dar olabilir.** Haftalık rejimde TREND_UP barların sadece
   %7'si; günlükte %14-17. RANGE rejiminde de long'a izin veriliyor ama
   `range_dip` kurulumu hiç tetiklenmedi. Ölçülmeli: rejim kapısı edge mi
   koruyor, örneklemi mi öldürüyor?

## Kural

Hipotez buraya yazılır, backtest'e çevrilir, CPCV'den geçerse koda girer.
Doğrudan config değiştirip "daha iyi görünüyor" demek, tam olarak DSR'ın
cezalandırdığı şeydir.
