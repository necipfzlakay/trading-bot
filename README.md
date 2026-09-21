# tbot

Otonom kripto al-sat botu. Bybit spot, sürekli tarama, yüksek seçicilik.

Tasarım ilkesi tek cümlede: **yanılmak kârlı görünmesin.** Sistem bir stratejiyi
kabul etmeye değil, reddetmeye ayarlı. Şu anda kendi stratejisini reddediyor
(aşağıya bakın) — bu bir arıza değil, çalıştığının kanıtı.

## Hızlı başlangıç

```bash
./ops/pre-install.sh          # kurulum + ölçüm + kontrol (bir kez)
./ops/bot.sh start demo       # 7/24 çalıştır — bot + panel + sağlık kontrolü
./ops/bot.sh status           # ne oluyor
./ops/bot.sh stop             # durdur
```

`pre-install.sh` ortamı kurar, veriyi indirir, indikatörleri ölçer, backtest ve
Kapı 1'i çalıştırır, sonra motorun açılış yolunu kilide kadar yürüyerek her
adımın **gerçekten** işe yaradığını doğrular. İlk koşu ~20 dakika, yarıda
kesilirse kaldığı yerden devam eder. Botu başlatmaz — o ayrı bir karar.

`bot.sh` launchd agent'larını moda göre üretir (`demo` · `live` · `fast`),
**kurmadan önce o modun başlayabildiğini doğrular**, sonra motorun gerçekten
kalktığını bekler. Elle python çalıştırmak gerekmiyor. Gerçek para Kapı 1 ve
elle yazılan bir onay cümlesi istiyor.

Tek tek çalıştırmak, ne olduğunu görmek veya bir adımı tekrarlamak için:
**[docs/USAGE.md](docs/USAGE.md)**.

## Dokümanlar

| | |
|---|---|
| **[docs/USAGE.md](docs/USAGE.md)** | **Nasıl çalıştırılır ve kullanılır** — sıfırdan günlük takibe |
| [SETUP.md](SETUP.md) | API anahtarları, Telegram, gereken sermaye, komisyon |
| [docs/INDICATORS.md](docs/INDICATORS.md) | 22 indikatör: ne ölçüyorlar, hangisi işe yaradı |
| [CLAUDE.md](CLAUDE.md) | Mimari, değişmez kurallar, denetim geçmişi |
| [research/hypotheses.md](research/hypotheses.md) | Denenen/denenecek hipotezler + deneme sayacı |

## Neyi farklı yapıyor

**İndikatör seçimi ölçülüyor, varsayılmıyor.** 22 indikatörün her biri
Information Coefficient ve kuyruk (tail) analiziyle sınanıyor; geçemeyen ne
kadar popüler olursa olsun oy veremiyor. Korelasyonu 0.8'i geçenler tek "oy
bloğuna" düşüyor — ölçümde 7 indikatör tek bloğa indi, yani onları ayrı onay
saymak 1 okumanın 7 kopyasına sahip olmak demekti.

**Doğrulama peşin bir bara bağlı.** CPCV (purge + embargo ile yüzlerce
out-of-sample yol), Deflated Sharpe (kaç deneme yapıldığını fiyatlar) ve PBO.
Eşikler önceden sabit; sonucu kurtarmak için oynatılamaz.

**Risk sürekli, ikili değil.** Düşüşte pozisyon küçülür ve conviction eşiği
yükselir; bot susmaz. Sadece mutlak zeminde (%50 düşüş) yeni giriş durur,
mevcutlar yönetilmeye devam eder.

**LLM sadece risk azaltabilir.** Sinyal üretemez, boyut belirleyemez, stop
taşıyamaz. Sınır kodda: veto rolünün yanıt şemasında `side`/`size`/`entry` alanı
yok, yani "al" cümlesi temsil edilemez. Her veto karşı-olgusal ölçülüyor;
faydası gösterilemeyen rol kapatılır.

## Gereken sermaye

**Minimum $183, rahat $306** — ölçüldü, varsayılmadı (`scripts/capital_check.py`).
Altında bot test edilen stratejiyi küçültmez; evrenin daha sakin bir alt kümesini
işler ve backtest o alt kümeyi tarif etmez. `risk.min_equity` bunu zorluyor.

## Şu anki durum: Kapı 1 geçilmedi

| kontrol | değer | |
|---|---|---|
| işlem ≥ 50 | 116 | ✅ |
| profit factor ≥ 1.2 | 1.20 | ❌ |
| maks düşüş ≤ %20 | %11.6 | ✅ |
| DSR anlamlı | p=1.000 | ❌ |
| CPCV yollarının çoğu pozitif | %55.6 | ✅ |
| PBO ≤ 0.5 | 1.00 | ❌ |

Strateji **+%4.73**, BTC al-tut **+%194.9**. `--trials 9` ile ölçüldü — 9 deneme
sonrası DSR'ın beklediği "şans eseri en iyi Sharpe" gözlenenin çok üstünde.

Altyapı tamamlandı ve çalışıyor. Strateji tamamlanmadı — açık hipotezler
`research/hypotheses.md`'de, her biri denendiğinde `--trials` sayacına eklenmeli.

## Lisans / sorumluluk

Kişisel kullanım için yazıldı. Alım satım para kaybettirir; bu kod hiçbir şey
garanti etmez. Gerçek paraya geçiş `TBOT_CONFIRM_LIVE=yes` gerektirir ve
Kapı 1/2/3 ölçütleri rapor çıktısıyla karşılanmadan yapılmamalıdır.
