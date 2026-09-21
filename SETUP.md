# Kurulum ve senin yapman gerekenler

Kod tarafı bitti. Aşağıdakiler senin makinende, senin onayınla yapılması gereken
şeyler — hiçbiri kod yazmayı gerektirmiyor.

## 0. Önce bunu çalıştır

```
./ops/pre-install.sh
```

Ortam, bağımlılıklar, `.env`, testler, veri, ölçümler, backtest, Kapı 1 ve
motorun duman testi — hepsi tek koşuda, her adım kontrol edilerek. Sonunda
aşağıdakilerden **hangilerinin gerçekten sana kaldığını** listeler; çoğu
zaman liste kısadır.

Bu dosyadaki §3 (Bybit anahtarı), §4 (Telegram) ve §5 (LLM) script'in
yapamayacağı şeyler — hesap açmak ve anahtar üretmek gerekiyor. §1 ve §2 sudo
istiyor ya da dışarıya bir şey gönderiyor. §6 (7/24) ve §9 (gerçek para) ise
`ops/bot.sh` ile yapılıyor ama ayrı ve kasıtlı kararlar, o yüzden kurulumun
parçası değiller.

Ayrıntılı kullanım: **[docs/USAGE.md](docs/USAGE.md)**.

## 1. Xcode lisansı (git için — zorunlu)

Git sistem genelinde bloke, çünkü Xcode Command Line Tools lisansı kabul
edilmemiş. Sudo gerektirdiği için ben yapamıyorum:

```
sudo xcodebuild -license accept
```

## 2. GitHub'a private repo (isteğe bağlı ama önerilir)

```
gh auth login          # tarayıcıda açılır, GitHub hesabınla giriş yap
./ops/git_setup.sh     # repo oluşturur, private olarak pushlar
```

Script pushlamadan önce `.env` ve `runtime/` klasörünün gerçekten ignore
edildiğini doğruluyor. API anahtarların ve işlem geçmişin repoya gitmez.

## 3. Bybit demo hesabı (motorun çalıştığı yer)

1. bybit.com → giriş yap → **Demo Trading** moduna geç
2. Demo arayüzünde **API** → yeni anahtar oluştur (izinler: Spot Trade + Read)
3. `.env.example` dosyasını `.env` olarak kopyala ve doldur:

```
cp .env.example .env
```

```
BYBIT_DEMO_API_KEY=...
BYBIT_DEMO_API_SECRET=...
```

> Demo anahtarı **testnet değil**. Testnet arayüzünden anahtar oluşturursan
> kimlik doğrulama hatası alırsın — ikisi ayrı platform.

Sanal bakiye istemek için:
```
.venv/bin/python scripts/demo_topup.py --usdt 1000
```

## 4. Telegram (isteğe bağlı, 2 dakika)

1. Telegram'da `@BotFather` ile konuş → `/newbot` → token al
2. Kendi botuna bir mesaj yaz, sonra chat id'ni öğren:
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. `.env`'e ekle:

```
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

4. `config/base.yaml` içinde `telegram.enabled: true` yap

## 5. LLM (isteğe bağlı)

Ücretsiz, kart gerekmiyor:
- **Gemini** (birincil): https://aistudio.google.com/apikey
- **Groq** (yedek): https://console.groq.com/keys

```
GEMINI_API_KEY=...
GROQ_API_KEY=...
```

Sonra `config/base.yaml` içinde `llm.enabled: true` ve kullanmak istediğin
rolleri `true` yap.

> **Gizlilik:** Gemini ücretsiz katmanında gönderilen veri eğitim için
> kullanılabiliyor. `llm/redact.py` bakiye, anahtar, pozisyon büyüklüğü gibi
> alanların çıkmasını zorla engelliyor — sadece anonim piyasa verisi ve yüzdesel
> istatistik gidiyor. Yine de bilerek karar ver.

## 6. 7/24 çalıştırma

Elle python çalıştırmana gerek yok:

```
./ops/bot.sh start demo        # veya live / fast
./ops/bot.sh status
./ops/bot.sh stop
```

Moda göre launchd agent'larını üretir, **kurmadan önce o modun gerçekten
başlayabildiğini doğrular**, yükler ve motorun doğru modda ayağa kalktığını
bekler. Üç servis: bot, panel, sağlık kontrolü. `KeepAlive=true` — süreç nasıl
ölürse ölsün geri gelir, girişte otomatik başlar.

Ön kontrol olmadan bu tehlikeli: `KeepAlive` altında başlayamayan bir config
30 saniyede bir sonsuza kadar denenir. Script bunu kurmadan önce yakalıyor,
kalkamazsa da agent'ı geri kaldırıyor.

`./ops/install.sh` eski isim olarak duruyor, `bot.sh start demo`'ya yönlendiriyor.

Ayrıntı: **[docs/USAGE.md](docs/USAGE.md)** §7.

## 7. Ne kadar para gerekiyor?

**Minimum $183, rahat $306.** Bu bir tercih değil, aritmetik — `scripts/capital_check.py`
ile ölçüldü ve `risk.min_equity` olarak config'e yazıldı. Bu bakiyenin altında bot
yeni pozisyon açmaz.

Sebep: boyutlandırma zinciri sabit.

```
1R        = bakiye × risk_oranı × volatilite_ölçeği
adet      = 1R / stop_mesafesi
notional  = adet × fiyat = 1R / stop_yüzdesi
```

Borsa $5'in altını reddediyor. Ters çevir: bir hesap ancak
`1R / $5.75` kadar geniş stop ifade edebilir.

$100'da bu sınır %13. Kurulumların %84'ü geçer, **%16'sı sessizce atlanır** — ve
atlananlar volatil olanlar. Yani bot test edilen stratejinin küçültülmüş halini
değil, evrenin **daha sakin bir alt kümesini** işler. Backtest o alt kümeyi
tarif etmiyor.

| bakiye | 1R | en geniş stop | erişilen kurulum |
|---|---|---|---|
| $50 | $0.38 | %6.5 | %62 |
| $100 | $0.75 | %13.0 | %84 |
| $150 | $1.12 | %19.6 | %92 |
| **$200** | $1.50 | %26.1 | **%96** |
| **$300** | $2.25 | %39.1 | **%99** |
| $500 | $3.75 | %65.2 | %100 |

Risk oranını yükseltmek sınırı düşürür ama portföy riskini artırır:

| risk/işlem | min bakiye | portföy riski (3 pozisyon) |
|---|---|---|
| %0.50 | $274 | %1.5 |
| %0.75 | **$183** | %2.2 |
| %1.00 | $137 | %3.0 |
| %1.50 | $91 | %4.5 |
| %2.00 | $69 | %6.0 |

Değiştirirsen yeniden ölç:

```
.venv/bin/python scripts/capital_check.py --equity 250
```

> `starting_balance` config'te sadece backtest için. **Canlı modda
> bakiye borsadan okunur**, config'teki değer kullanılmaz.

## 7b. Pozisyon büyüklüğü tavanları

Yüzde bazlı boyutlandırma sınırsız ölçekleniyor: %0.75 riskle 1R, $200 hesapta
$1.50 iken $50.000'de $375 olur. Aritmetik tutarlı, operasyonel olarak akılsız —
küçük pozisyonlarla doğrulanmış bir strateji, ince bir emir defterini oynatacak
büyüklükte doğrulanmış değildir.

`config/base.yaml` → `risk:` altında üç sert tavan. **Hiçbiri pozisyonu
büyütemez, sadece kısar.** Hangisi önce bağlarsa o geçerli. `0` = kapalı.

```yaml
max_position_notional: 0.0    # tek pozisyonda maks USDT. 0 = sınırsız
max_position_pct: 0.40        # tek pozisyon equity'nin en fazla %40'ı
max_risk_amount: 0.0          # tek işlemde maks 1R (USDT). 0 = sınırsız
```

Ne zaman hangisi bağlar ($10.000 equity, %0.75 risk):

| stop | tavansız pozisyon | equity payı | %40 tavanıyla |
|---|---|---|---|
| %1 | $7.500 | %75 | **$4.000** |
| %2 | $3.750 | %38 | $3.750 |
| %5 | $1.500 | %15 | $1.500 |

Dar stop büyük pozisyon üretir — `max_position_pct` orada devreye giriyor.
`max_position_notional` hesap büyüdükçe, `max_risk_amount` ise doğrudan tek
işlemin kaybını sınırlar (en doğrudan tavan).

Tavan bağladığında risk ilişkisi korunuyor: `1R = adet × stop_mesafesi` hâlâ
doğru, sadece maruziyet kısılıyor. Kısılan pozisyon loglarda `capped_by` ile
görünür — küçük açılan pozisyon gizemli olmaz.

## 8. Komisyon nasıl çalışıyor

Emir **gönderirken değil, dolduğunda** kesiliyor. İptal edilen veya hiç dolmayan
emir sıfır maliyet.

```
ALIŞ   $100 × %0.10 = $0.100
SATIŞ  $105 × %0.10 = $0.105   ← çıkış değeri üzerinden
                      ────────
       gidiş-dönüş   $0.205 = pozisyonun %0.205'i
```

Bybit spot'ta maker = taker = %0.1. Satış tarafı çıkış değerinden kesildiği için
kazandıkça komisyon da büyüyor.

Ek gizli maliyet **spread** — market emri karşı taraftan alır. Evrende ölçüldü:
medyan 1 bps, en kötü 8 bps. İhmal edilebilir.

Sistem toplam 30 bps varsayıyor (komisyon 20 + kayma 10); gerçek ~21-28 bps.
Muhafazakâr taraf, doğrusu bu. Canlıda gerçek kayma ölçülüyor
(`execution/safety.py`) ve varsayımı aşarsa uyarıyor.

## 9. Gerçek para (Kapı 3 geçilmeden yapma)

```
./ops/bot.sh start live
```

İki kapı var ve ikisi de kasıtlı:

1. **Kapı 1 geçilmiş olmalı.** `runtime/gate1.json` FAIL diyorsa script kurulumu
   reddediyor. Şu an FAIL diyor.
2. **Onay cümlesini elle yazman gerekiyor.** Bayrak yok — bayrak bir script'in
   yanlışlıkla geçebileceği şeydir.

Ayrıca API anahtarının çekim yetkisi varsa kurulum reddediliyor, ve bakiye
`min_equity`'nin altındaysa uyarıyor.

Tek seferlik elle çalıştırmak istersen bariyer hâlâ environment değişkeni:

```
TBOT_CONFIRM_LIVE=yes .venv/bin/python scripts/run.py --config config/live.yaml
```

---

## Sıradaki iş (kod tarafı, bende)

Kapı 1 şu an **geçilmiş değil** — strateji v1 ölçüldü ve al-tut'un altında kaldı.
`runtime/reports/cpcv.md` altı kontrolden beşinin neden başarısız olduğunu
gösteriyor. Sıradaki adım strateji araştırma döngüsü: hipotez → backtest →
CPCV → kabul veya red. Altyapı bunun için hazır.
