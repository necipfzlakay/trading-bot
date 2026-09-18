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

| 9 | Conviction-çöküş çıkışı canlıda ve backtest'te **hiç tetiklenmiyordu** (`conviction_now=None` sabit) | mimari denetim | **doğrulandı ve tamir edildi** — PF 1.20→1.41, expectancy +0.339→+0.364R, Sharpe 0.22→0.44, **PBO 1.00→0.33** | 0 (tamir, deneme değil) |

| 10 | Kırılım seviyesine önceden kurulu emir bırakmak, bar kapanışını beklemekten iyidir | bar-arası körlük | **test edildi, REDDEDİLDİ** — sonuç iyi göründü ama 5 işlemden geliyor, 2'si %92'sini taşıyor. Mekanizma kuruldu, varsayılan **kapalı** | 1 |

**Kümülatif deneme: 10.** Bir sonraki CPCV koşusunda `--trials 10` geçirin.

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

## Conviction-çöküş tamiri hakkında bir uyarı

Bu tamir sonucu **iyileştirdi** — volatilite ölçekleyici tamirinin aksine, o kötüleştirmişti.

İyileşen bir tamir daha fazla şüphe hak eder, çünkü kimse hoş sürprizi soruşturmaz.
Kayda geçen çekinceler:

1. **Temiz bir A/B değil.** İki koşu arasında arşiv büyüdü (al-tut +%194.9 → +%205.2),
   yani farkın bir kısmı veriden geliyor olabilir. Temiz atıf için eski kodu aynı veriyle
   koşturmak gerekirdi.
2. **Bootstrap CI hâlâ sıfırı içeriyor:** [-0.096, +0.903]R. Yani expectancy'nin pozitif
   olduğu istatistiksel olarak gösterilmiş değil.
3. **Deneme sayılmama gerekçesi:** hiçbir parametre aranmadı. `collapse_threshold: 32.0`
   config'te baştan beri duruyordu; yalnız kendisine geçirilen değer `None` olduğu için
   dal hiç çalışmıyordu. Funding polaritesi ve vol-scaler tamirleriyle aynı kategori.
4. **PBO 1.00 → 0.33 asıl haber.** Aşırı uyum sinyali büyük ölçüde kayboldu. Kalan tek
   engel DSR ve DSR bu stratejiyle değil **deneme sayısıyla** ilgili — oradan çıkış hâlâ
   taze veri.

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

4. **Kurulum kapısı gereksiz olabilir — HENÜZ TEST EDİLMEDİ.** Kesitsel modda
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
