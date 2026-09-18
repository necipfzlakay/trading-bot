# Kurulum ve senin yapman gerekenler

Kod tarafı bitti. Aşağıdakiler senin makinende, senin onayınla yapılması gereken
şeyler — hiçbiri kod yazmayı gerektirmiyor.

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

## 3. Bybit demo hesabı (paper'dan sonraki adım için)

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

## 6. 7/24 çalıştırma (launchd)

```
./ops/install.sh
```

Bu iki launchd agent kurar: bot ve panel. `KeepAlive=true` — süreç nasıl ölürse
ölsün geri gelir, girişte otomatik başlar.

Durdurmak (tasarımdaki "elle kapatma" bu):
```
launchctl unload -w ~/Library/LaunchAgents/com.tbot.bot.plist
```

Durum kontrolü:
```
launchctl list | grep tbot
tail -f runtime/logs/bot.jsonl
open http://127.0.0.1:8787
```

## 7. Gerçek para (Kapı 3 geçilmeden yapma)

`config/live.yaml` kullanır ve ek olarak environment değişkeni ister:

```
TBOT_CONFIRM_LIVE=yes .venv/bin/python scripts/run.py --config config/live.yaml
```

Bu bariyer kasıtlı. Yanlışlıkla gerçek parayla emir göndermeyi imkânsız kılıyor.

---

## Sıradaki iş (kod tarafı, bende)

Kapı 1 şu an **geçilmiş değil** — strateji v1 ölçüldü ve al-tut'un altında kaldı.
`runtime/reports/cpcv.md` altı kontrolden beşinin neden başarısız olduğunu
gösteriyor. Sıradaki adım strateji araştırma döngüsü: hipotez → backtest →
CPCV → kabul veya red. Altyapı bunun için hazır.
