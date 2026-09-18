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

**Kümülatif deneme: 7.** Bir sonraki CPCV koşusunda `--trials 7` geçirin.

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

4. **Kurulumlar ölçümü temsil etmiyor olabilir.** Tail analizi *üst desil
   momentum*un işe yaradığını söyledi. `momentum_continuation` bunu ADX>25 +
   RSI>58 eşikleriyle yaklaşıkladı — ama bu bir yaklaşım, ölçülen şeyin kendisi
   değil. Doğrudan "conviction skorunun üst desili" ile giriş denenmeli.

5. **Rejim kapısı çok dar olabilir.** Haftalık rejimde TREND_UP barların sadece
   %7'si; günlükte %14-17. RANGE rejiminde de long'a izin veriliyor ama
   `range_dip` kurulumu hiç tetiklenmedi. Ölçülmeli: rejim kapısı edge mi
   koruyor, örneklemi mi öldürüyor?

## Kural

Hipotez buraya yazılır, backtest'e çevrilir, CPCV'den geçerse koda girer.
Doğrudan config değiştirip "daha iyi görünüyor" demek, tam olarak DSR'ın
cezalandırdığı şeydir.
