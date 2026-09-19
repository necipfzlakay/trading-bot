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

**Kümülatif deneme: 16.** Bir sonraki CPCV koşusunda `--trials 16` geçirin.

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
