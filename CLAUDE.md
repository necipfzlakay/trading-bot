# TRADING-BOT — proje haritası

Otonom kripto al-sat botu. Bybit spot, önce paper/demo, sonra gerçek para ($100).
Plan: `~/.claude/plans/simdi-benim-aklimda-bir-ticklish-micali.md` · Kurulum: `SETUP.md`

```bash
.venv/bin/python -m pytest -q             # 449 test
.venv/bin/ruff check src tests scripts    # lint
```

## Değişmez kurallar

Tercih değil, sistemin doğruluğunu taşıyan sözleşmeler. Değiştirmeden önce ilgili
testi oku — hepsinin kendini koruyan bir testi var.

1. **Sadece kapanmış bar.** Sinyal bar *t* kapanışında, emir *t+1* açılışında.
   Oluşmakta olan bar diske bile yazılmaz (`ParquetStore._normalise_ohlcv`).
   → `test_indicators.py::test_indicator_cannot_see_the_future` (33 indikatör)
2. **Zaman dilimi hizalaması kapanışa göre.** Bir 4h barı, ancak *kapandıktan*
   sonra bir 1h barına görünür. İçerme mantığı 4 saat geleceği okur.
   → `backtest/align.py` · `test_backtest_core.py::test_fast_bar_only_sees_slow_bars_that_have_already_closed`
3. **Bloklar oy verir, indikatörler değil.** |ρ|≥0.8 olanlar tek bloğa düşer.
   → `strategy/conviction.py::_count_blocks`
4. **Bot zararda durmaz, küçülür.** `risk_multiplier` sıfır olamaz; config
   validasyonu böyle bir merdiveni reddeder. → `risk/budget.py`
5. **Stop tetiklendiği yerde dolar**, sonraki açılışta değil. Aksi halde zarar
   olduğundan küçük görünür. → `paper_broker.force_fill_at`
6. **Emirler idempotent.** `client_id = hash(strateji, sembol, bar, aksiyon)`.
   Gönderim ile kayıt arasındaki çökme pozisyonu ikiye katlayamaz.
7. **LLM riski yalnız azaltabilir.** Sinyal üretemez, boyut belirleyemez, stop
   taşıyamaz. Yanıt şeması bunu temsil edilemez kılıyor. → `test_llm_safety.py`
8. **Kaldıraç kapısı.** Kod 1.0'ın üstüne kendiliğinden çıkamaz; kapı geçilse
   bile kullanıcı tavanını aşamaz. → `risk/leverage.py`
9. **Gerçek para ek onay ister.** `TBOT_CONFIRM_LIVE=yes` olmadan başlamaz.
10. **NaN = "fikrim yok", 0 değil.** Isınma nötr oy olarak sayılmaz.
11. **Paper emirleri sonraki barın açılışında dolar**, gönderildiğinde değil.
    Aksi halde paper backtest ile aynı yolu izlemez. → `_drain_paper_fills`
12. **Canlıda stop borsada da durur.** Süreç kapalıyken bellekteki stop hiçbir
    şeyi korumaz. → `_place_protective_stop`
13. **Çekim yetkili API anahtarı reddedilir.** Bot buna hiç ihtiyaç duymaz;
    varlığı sadece risk. → `execution/safety.py`
14. **Tek instance.** İki motor aynı hesapta = her pozisyon iki kez.
    → `engine/lock.py` (flock, çökmede kernel serbest bırakır)
15. **Açık pozisyonun sembolü evrenden düşse bile yönetilir.** Aksi halde stop
    ilerlemez ve çıkış hiç tetiklenmez. → `_refresh_data`
16. **Bar eşikleri zaman dilimine göre.** 3 yıllık haftalık veri tanım gereği
    156 bardır; sabit 200 eşiği her haftalık seriyi reddeder.
17. **Haftalık barlar Pazartesi'ye hizalı**, epoch'a (Perşembe) değil.
    → `clock.timeframe_offset`
18. **Minimum bakiye yapısal.** `risk.min_equity` (ölçülen: 183) altında yeni
    giriş yok. Altında bot test edilen stratejiyi küçültmez, evrenin daha sakin
    bir alt kümesini işler. → `scripts/capital_check.py`
19. **`starting_balance` sadece paper/backtest.** Canlıda bakiye borsadan okunur.
20. **Pozisyon tavanları sadece kısar.** `max_position_notional` / `_pct` /
    `max_risk_amount` — hangisi önce bağlarsa. Tavan bağlasa bile
    `1R = adet × stop_mesafesi` korunur. → `risk/sizing.py`
21. **`SizingResult` keyword-only.** Alan eklemek pozisyonel argümanı sessizce
    yanlış alana bağlamasın diye — bir kez oldu, red sebebi `capped_by`'a düştü.
22. **Canlıda bakiye borsadan, portföy de öyle.** `_equity_now` başarısız okumada
    `starting_balance`'a düşemez; portföy `seed_from_venue` ile besleniyor. Aksi
    halde nakit sınırı hayali oluyor ve ilk barda equity kendiyle çelişiyor.
    → `test_portfolio_seeded_from_venue_replaces_configured_balance`
23. **Stop miktarı satılabilir bakiyeye kısılır.** Bybit spot alış komisyonunu
    **base varlıktan** kesiyor: 65.69 dolan emir hesapta 65.62 bırakıyor, 65.69'luk
    stop "Insufficient balance" ile reddediliyor. → `_sellable`
24. **Reddedilen stop her barda yeniden denenir.** Tek red, pozisyonu ömrü
    boyunca korumasız bırakıyordu. → `_place_protective_stop` retry
25. **Kilitli varlık hâlâ pozisyondur.** Koruyucu stop base varlığı rezerve
    eder; `fetch_positions` bu yüzden `total` okur, `free` değil. Nakit ise
    `free` — kilitli para boyutlandırılamaz. → `_balances`
26. **Çalışan stop iptal edilmeden önce yerine konacağı bilinmelidir.** Var olan
    ve doğru seviyedeki stop benimsenir; iptal yalnızca gerçekten
    değiştirilecekse yapılır. → `place_stop` adopt/replace
27. **Kod değişikliği otomatik yüklenmez.** launchd çökmede geri getirir,
    dosya değişince getirmez. → `bot.sh status` bayat kod uyarısı
28. **Koşular arası bakiye değişimi sessiz kalamaz.** Restart'ta borsadaki
    bakiye son kalıcı equity ile karşılaştırılır; %25'ten büyük fark alarm
    üretir. → `_check_balance_against_last_run`
29. **Sıfırlama gerçek paraya dokunamaz.** `scripts/venue_reset.py` yalnızca
    `bybit-demo` kabul eder ve bunu değiştiren bayrak yoktur.
30. **Yinelenen client id red değil kanıttır.** Borsa id'yi hatırlıyorsa emir
    *vardı*: dolan/açık olan benimsenir (çift pozisyon olmaz), iptal edilmiş
    olan yeni id ile tekrar gönderilir (niyet gerçekleşmemişti).
    → `_resolve_duplicate`
31. **Satılamayan miktar pozisyon değildir.** `min_amount` altı bakiye
    `fetch_positions`'ta görünmez — temizlenemeyen uyarı üretmemeli.
32. **Dolan emir, hâlâ elde tutulan pozisyonun kanıtıdır.** Borsa geçmişi malı
    satılmış dolumları da hatırlar; benimsemeden önce envanter doğrulanır,
    yoksa o bar atlanır. → `_resolve_duplicate`
33. **Yüzdeler gerçekten var olan paraya göre ölçülür.** `starting_equity`
    state'te taşınır; hiçbir arayüz sabit bir taban sayı kullanamaz.
34. **Hiçbir tarama satırı sebepsiz kalamaz.** `no_trade_reason=None` panelde
    "eligible" demek; değerlendirilip elenen sembol için bu yalandır.
    → `test_every_candidate_ends_the_bar_with_a_reason`
35. **Upsert pozisyonun tamamını tazeler.** Çakışma anahtarı sembol olduğu için
    yeni bir pozisyon eskisinin satırına düşer; giriş fiyatı miras kalırsa R
    ölçümü başka bir işleme ait olur. → `upsert_position`
36. **`meta` kalıcıdır.** `stop_order_id`'yi taşır; kaybolursa motor kendi
    borsa stop'unu ne taşıyabilir ne tanıyabilir. → `_reconcile`
37. **Motor beklerken de nabız atar.** `HEARTBEAT_SECONDS=60`; ölü-adam
    anahtarı ancak nabız kadar keskin olabilir. Bar uzunluğu, arıza tespit
    süresini belirlememeli.
38. **Bar arası yalnız seviye sorulur, indikatör hesaplanmaz.** Bar kapanışında
    hesaplanmış bir sayıya fiyat değdi mi — tek izinli soru. Oluşmakta olan barda
    indikatör hesaplamak backtest'in canlı yolu üretmesini imkânsız kılar, ki bu
    repodaki her sayı ona dayanıyor. → `ExitManager.evaluate_intrabar`
39. **Bybit spot'ta stop ve kar-al aynı anda duramaz** (perp'te durabilir — bkz. 56). Stop base varlığı
    kilitliyor; ikinci reduce-only emir `170131` ile reddediliyor (demo'da
    ölçüldü). Bu yüzden **stop borsada, hedef motorda**. Asimetri kasıtlı:
    kaçırılan hedef fırsat kaybı, eksik stop sermaye kaybı.
40. **Aynı barda hem stop hem hedef tetiklenirse stop dolmuş sayılır.** Bar OHLC
    hangisinin önce olduğunu söyleyemez; kötümser seçilir. Aksi halde canlı R
    backtest'ten *iyi* çıkar — ve kimse hoş sürprizi soruşturmaz.
41. **Kurulu giriş emrinin ömrü bir bardır** ve iptali **doğrulanır.** Seviyeyi
    üreten kanal kaydı; kalan emir, stratejinin artık fikri olmadığı bir fiyattan
    dolar. Sessizce başarısız olan iptal, hiç yapılmamış iptalden kötüdür.
42. **Feed thread'i Broker, Database, Portfolio veya Store'a dokunmaz.** Kendi
    dict'ine kendi kilidiyle yazar; emri motor thread'i verir. Ayrıca public veri
    olduğu için o thread'e hiç anahtar girmiyor.
43. **Bybit public spot WS abonelik başına 10 topic alır.** 11'inci sembol
    aboneliği *sessizce* askıda bırakıyor — hata yok, exception yok, sadece hiç
    çözülmeyen bir future. Tüm evren kararıyor. Bisection ile ölçüldü.
    → `MAX_TOPICS_PER_SUBSCRIPTION`
44. **Borsanın kendi başına yaptığı işlemi motor bar kapanışını beklemeden
    öğrenir.** Stop borsada dolduğunda motor bunu bir saate kadar bilmiyordu:
    portföy yanlış, equity yanlış, slot boşa kilitli, ve sonraki barda olmayan
    pozisyon için ikinci satış. İki okuma şart — tek okuma kanıt değil.
    → `_detect_venue_exits`
50. **Bilinmeyen config anahtarı hatadır, varsayılana dönüş değil.** Her bölüm
    `extra="forbid"`. İki kez ölü ayar üretti (`max_spread_bps`,
    `vol_lookback_bars`): dosyada duruyor, hiçbir kod okumuyor, ve okunuyormuş
    gibi görünüyor. → `StrictModel`
52. **Kurulum kapısı ölçüldü ve kalıyor.** Kesitsel sıralama tek başına
    yetmiyor: kapı kaldırılınca işlem 3.6x artıyor (103→367, ayda 2.9→10.2)
    ama Kapı 1'de 5/6 → 2/6 ve PBO 0.33 → 0.67. Açık hipotez #4 kapandı.
53. **Kısa vadeli al-sat bu evrende ölü, üç ayrı ölçümle.** +2%/−1% bariyerde
    maliyeti karşılamak için isabet %43.3 gerekiyor; taban %31.9, en iyi fiyat
    sinyali %34.5, taker akış dengesizliği %30.7. Edge gerçek ama komisyonun
    sekizde biri. → `research/short_term_feasibility.md`

54. **İki motor tek hesapta = emir kimliğinde isim alanı.** `client_id`
    hash'i (strateji, sembol, bar, aksiyon) idi; `cross_sectional` modda her
    motor `ranked_entry` ürettiği için iki motor aynı barda **birebir aynı
    id**'yi hesaplıyor. İkincisinin `submit`'i birincinin emrini bulup kendi
    emri sanıp benimsiyor. Boş isim alanı eski id'yi bit-bit koruyor, yani
    çalışan motor migrasyon istemiyor. → `client_order_id(..., namespace)`
55. **Okunamayan bakiye boş hesap değildir.** `_balances` hata yakalayınca boş
    sözlük dönüyordu ve bu "her şey satıldı" ile ayırt edilemiyordu. 4 saatlik
    ağ kesintisinde motor yerinde duran pozisyonu `position_inventory_missing`
    diye işaretledi; kurtaran şey stop sorgusunun da aynı anda başarısız olup
    akışı "yüksek sesle bildir" dalına düşürmesiydi — tasarım değil şans.
    Artık `None` dönüyor ve `_reconcile` okuyamadığı borsayla mutabakat
    yapmayı reddediyor.
56. **Perp'te stop ve kar-al aynı anda durabilir.** Değişmez kural 39 spot'a
    özgüymüş: orada stop base varlığı kilitlediği için ikinci reduce-only emir
    `170131` alıyor. Perpetual'da ikisi de pozisyona bağlanıyor ve **tek
    çağrıda kabul ediliyor** (demo'da ölçüldü). Yani hızlı motorda hedef
    borsada yaşıyor ve süreç kapalıyken de korur. → `place_take_profit`
57. **Kaldıraç, margin bayrağından daha bağlayıcı.** UTA hesapta
    `marginMode: ISOLATED_MARGIN` kabul ediliyor ama pozisyon hâlâ
    `tradeMode=0` (cross) raporluyor — bayrağı şart koşmak doğru kurulmuş bir
    hesabı reddederdi. Asıl koruma kaldıraç: **1x'te pozisyon %100 teminatlı,
    short likidasyonu girişten %99 uzakta; 3x'te bu %33'e iner.** Bu yüzden
    doğrulanan şey kaldıraç, ve cross margin yalnız 1x'te tolere ediliyor.
    → `PerpBroker.assert_venue_ready`
58. **Daha kısa bar daha kötü, ölçüldü.** Operatörün sorusu üzerine Bybit
    perp'ten 5m (90g) ve 15m (180g) çekilip aynı bariyer testi koşuldu:
    gereken isabet 5m'de **%48.9**, 15m'de %38.0, 1h'te %35.7 — elimizdeki her
    üçünde de ~%33. Maliyet hareketten önce yakalıyor. Günde birden fazla işlem
    daha hızlı bardan değil, iki yönden + 771 sembolden + zaman stop'undan
    gelir.
59. **Canlı ile backtest aynı seçim kuralını kullanmak zorunda.**
    `conviction.selection: cross_sectional` yazıyordu; backtest onu uyguluyor,
    canlı runner'da sıralayıcı **hiç yoktu** — canlı mutlak eşik (62) ile
    çalışıyordu. Backtest üst desili alıp `rank_absolute_floor`'a (40) kadar
    eşik altındakileri geri kabul ediyor. 40–62 bandında backtest işlem açıyor,
    canlı hiç açmıyordu. Repodaki her sayı backtest'in cevabından geliyor.
    → `Runner._select`

60. **Dolum şartları borsadan okunur, istekten değil.** Bybit'in emir onayı id
    ve durumdan ibaret: dolan miktar yok, ortalama fiyat yok, komisyon yok.
    `average_price or price` geri-düşüşü muhafazakâr değil **yanlış** — ödenmemiş
    fiyatı R'nin, MAE'nin, MFE'nin ölçüldüğü sütuna yazar ve kayma izleyicisine
    her işlemde tam sıfır sürüklenme verir, yani izleyici borsa ne yaparsa yapsın
    ateşleyemez. Market emri gönderildikten sonra borsaya sorulur; bilinmeyen
    sıfır değildir, okuma başarısızsa geri-düşüş **loglanır**.
    → `CCXTBroker._settle`, `Runner._fill_terms`
61. **Spot alışın komisyonu base varlıkta, defter quote'ta.** 1.31 VVV alımında
    `cumExecFee` 0.00131 **VVV** = 0.034 USDT (demo'da ölçüldü). Ham sayıyı dolar
    sütununa yazmak turu neredeyse komisyonun tamamı kadar eksik gösterir.
    Değişmez 23'ün öteki yarısı: orada bu gerçek stop miktarını kısıyordu, burada
    PnL'i. Çevrim notional'ın %1'ini aşarsa birim yanlış okunmuştur — varsayılan
    orana düşülür ve bağırılır. → `_fee_in_quote`
62. **Yanlış v5 kategorisinin cevabı boş liste, hata değil.** `category` sınıf
    niteliği, çünkü `__new__` ile kurulan bir broker'da bile doğru olmalı.
    `SOL/USDT:USDT` düz `replace("/","")` ile `SOLUSDT:USDT` oluyor ve her emir
    araması "böyle bir emir yok" diyor — çift emir çözümü, dolum uzlaştırma ve
    borsa-çıkış tespiti perp'te sessizce körleşiyordu.
    → `CCXTBroker.category`, `_venue_symbol`
63. **Ölü-adam anahtarı motoru adıyla arar.** `pgrep -f scripts/run.py` ilk
    başlayan motoru bulur; durmuş motorun sağlık kontrolü çalışan motorun pid'ini
    kanıt sayıyordu. İki motoru ayırt edemeyen anahtar, anahtar değildir.
    → `healthcheck.check_process(config_path)`
64. **Kaydın yönü emrin yönüdür, literal değil.** `Fill.side` `open_position`
    üzerinden doğrudan `position.side` oluyor: yanlış yön, ters PnL işareti ve
    girişin yanlış tarafında duran bir stop demek. Aynı şekilde giriş/çıkış
    ayracı da yön değil, emrin üstündeki **plan**. Spot-yalnız dünyada `Side.BUY`
    literali tek olasılığı yazdığı için ikisi de doğru görünüyordu.
    → `Runner._enter`, `_apply_paper_fill`, `_maybe_scale_out`
65. **Perp'te stop düzenlenmez, üzerine yazılır.** Stop pozisyonun bir özelliği,
    ayrı bir emir değil — gerçek bir emir id'si yok. Spot'un `edit_order` yolunu
    miras almak her trailing güncellemesini sessizce no-op yapar ve borsa
    pozisyonun ömrü boyunca ilk stop'u tutar. → `PerpBroker.amend_stop`
66. **Borsanın kaldıracı kazanılmış kaldıraçtır.** `risk.max_leverage` ileriye
    dönük bir yetkilendirme; borsaya kurulan sayı `LeverageGate`'in izin
    verdiğidir. Aksi halde motor 1x boyutlandırırken hesap 3x'te durur —
    teminat %100 değil %33 (değişmez 57). → `build_broker`

51. **Kuyruğu kesen her şey pahalıdır.** Kârın tamamı MFE 3R+ gören ~%20'lik
    işlemden geliyor (21 işlem +460, diğer 82 işlem −385). Erken çıkan her
    mekanizma — conviction çöküşü, kısmi kâr alma — vasatlarda kurtardığından
    fazlasını kuyrukta veriyor. İkisi de ölçüldü, ikisi de kapalı.

48. **Engellenmiş skor ile düşük skor aynı şey değildir.** `_blocking_reason`
    skorlamadan önce kısa devre yapar, `total=0.0` hesaplanmadığı içindir —
    "fikrim yok" (değişmez 10). Ama `below_threshold` skorlamadan *sonra*
    atanır: gerçek bir sayıdır, sadece giriş barını geçmemiştir. İkisini bir
    tutmak, conviction-çöküş çıkışının okuması gereken bütün skorları atar.
    → `UNSCORED_REASONS`
49. **Kendi tamirimin ölçümü de A/B ister.** "Ölü kodu canlandırdım, sonuç
    düzeldi" iki ayrı iddiadır ve ikincisi ayrıca kanıt gerektirir. Bir kez
    atlandı: tamir no-op çıktı, iyileşme arşiv büyümesindendi. Ölçüm için bot
    **durdurulur** ve `--to <tarih>` ile pinlenir.

46. **Bekleme döngüsü niyeti değil geçen süreyi ölçer.** Zaman aşımları
    `time.monotonic()` ile karşılaştırılır, adım adım toplanmaz. Toplayan sürüm
    döngü içindeki işi görmüyordu: Telegram `getUpdates` bu makineden 3-8 saniye
    sürüyor ve her 5 saniyelik tick'te çağrılıyordu, yani her tur gerçekte ~9
    saniyeydi ama 5 sayılıyordu. Bar kapanışları **7-38 dakika geç** işlendi.
    → `_wait_for_next_bar`
47. **Telegram komutları yavaş şeritte.** `telegram.poll_seconds` (15s) ve ayrı
    `poll_timeout` (3s). Komut bir kolaylık; açık pozisyonu izleyen kontrol
    değil. Yavaş bir ağ, pozisyon takibini yavaşlatamaz.

45. **Bot çalışırken alınan backtest ölçümü tekrarlanabilir değil.** Motor her
    barda aynı parquet arşivine yazıyor; iki koşu arasında veri büyüyor. A/B
    karşılaştırması `--to <tarih>` ile sabitlenmeli, yoksa kodun etkisi ile
    verinin etkisi ayrılamaz. Bu bir kez 113→114 işlemlik "kod değişikliği"
    üretti; kod aynıydı.

## Denetim (2026-09-17) — bulunan ve kapatılan

Sistemi baştan sona tarayıp uç durumları test ettim. Bulunanlar:

| # | sorun | ciddiyet | durum |
|---|---|---|---|
| 1 | Paper modda emirler **hiç dolmuyordu** — `process_bar` çağrılmıyordu, `average_price=0`, bakiye değişmiyordu | kritik | düzeltildi |
| 2 | Canlıda **borsa tarafı stop yoktu** — süreç kapalıyken pozisyon korumasız | kritik | düzeltildi |
| 3 | Panel kontrolleri ölüydü (`command.json` yazılıp hiç okunmuyordu) | yüksek | düzeltildi |
| 4 | Komutlar sadece bar kapanışında okunuyordu → 1h barda acil `flatten` 1 saat bekler | yüksek | düzeltildi (~10s) |
| 5 | `counterfactual_r` hiç doldurulmuyordu → `llm_impact.py` event_guard hakkında karar veremezdi | orta | düzeltildi |
| 6 | 5 LLM rolünden 3'ü hiç çağrılmıyordu (post_mortem, report_writer, config_auditor) | orta | düzeltildi |
| 7 | `notify_risk_change` / `notify_degraded` / `daily_summary` hiç çağrılmıyordu | orta | düzeltildi |
| 8 | Çekim yetkili API anahtarı kontrolü yoktu | orta | eklendi |
| 9 | Kayma (slippage) varsayımı hiç ölçülmüyordu | orta | eklendi |
| 10 | Bozuk veriden gelen imkânsız equity sıçraması boyutlandırmaya giriyordu | orta | eklendi |
| 11 | Mac uykusu botu durduruyordu (kripto 7/24) | orta | `caffeinate -i` plist'e eklendi |
| 12 | Ölü kod (`Portfolio.is_full`) | düşük | kaldırıldı |

### İkinci tur (aynı gün)

| # | sorun | ciddiyet | durum |
|---|---|---|---|
| 13 | **Süreç kilidi yoktu** — launchd + elle çalıştırma = aynı hesapta iki bot | kritik | `engine/lock.py` |
| 14 | **Evrenden düşen sembolde açık pozisyon sonsuza kadar yönetilmiyordu** — stop ilerlemez, çıkış tetiklenmez | kritik | düzeltildi |
| 15 | Kısmi dolum (partial fill) işlenmiyordu — bot sahip olmadığı miktarı satmaya çalışırdı | yüksek | düzeltildi |
| 16 | **Haftalık barlar yanlış hizalanıyordu** (epoch Perşembe, borsa Pazartesi) — tüm 1w backfill'i "bozuk" diye reddedildi | yüksek | düzeltildi |
| 17 | Sabit bar eşikleri (200/220/250/300) haftalık veriyi imkânsız kılıyordu | yüksek | zaman dilimi tablosuna bağlandı |
| 18 | `funding` contrarian varsayılıyordu — ölçüm 5/5 ufukta momentum dedi | orta | registry düzeltildi, `scripts/polarity.py` eklendi |
| 19 | `_veto_prices` sınırsız büyüyordu | düşük | 2000 ile sınırlandı |

### Üçüncü tur (2026-09-18, ilk demo koşusunda canlı yakalandı)

Bunların hiçbiri teste takılmamıştı; üçü de gerçek Bybit demo koşusunda çıktı.

| # | sorun | ciddiyet | durum |
|---|---|---|---|
| 20 | **Canlı modda portföy config'ten besleniyordu.** Borsada 50.000 varken portföy 1.000 taşıyor; sizing olmayan bir nakit sınırına takılıyor, ilk barda equity 50.000→1.000 "sıçrıyor", implausible-jump koruması motoru degraded'a düşürüyor (risk 0.25x). Değişmez kural 19 ihlali | kritik | `seed_from_venue` |
| 21 | `_equity_now` başarısız bakiye okumasında `starting_balance`'a düşüyordu — geçici API hatası, config parasıyla boyutlandırmaya dönüşürdü | kritik | son bilinen değere düşüyor, yoksa reddediyor |
| 22 | **Her spot long'un koruyucu stop'u reddediliyordu.** Komisyon base varlıktan kesildiği için emir miktarı hesaptakinden büyük → `retCode 170131 Insufficient balance` → pozisyon borsada korumasız. Değişmez kural 12 pratikte çalışmıyordu | kritik | `_sellable` kısma |
| 23 | Reddedilen stop hiç tekrar denenmiyordu | yüksek | her barda retry |
| 24 | **`bot.jsonl` hiç yazılmıyordu.** Modül seviyesindeki `get_logger()` import anında `configure()`'u `log_dir=None` ile çalıştırıyor, sonraki gerçek `configure(log_dir)` no-op'a düşüyordu. Tüm gözlem katmanı sadece stdout'taydı | yüksek | `configure` yeniden girilebilir |
| 25 | `expiredAt` ISO string geliyor, `int()` bekleniyordu — `assert_key_is_safe` çöküyor, demo/live hiç başlayamıyordu | kritik | `_parse_expiry` |
| 26 | **Kendi stop'u pozisyonu görünmez yapıyordu.** Stop base varlığı kilitliyor (`used`), `fetch_positions` sadece `free` okuyordu → motor pozisyonu kapandı sanıp portföyden düşürdü, yönetmeyi bıraktı, equity'yi 1.762 USDT eksik gösterdi | kritik | `fetch_positions` artık `total` |
| 27 | **Önce iptal, sonra yerleştir** — çalışan stop iptal edildi, yerine koyma başarısız oldu, pozisyon korumasız kaldı. Üstelik stop client id'si deterministik olduğu için tekrar deneme `170141 Duplicate clientOrderId` alıyordu | kritik | var olan stop **benimseniyor**; iptal sadece gerçekten değiştirilecekse; retry id'si benzersiz |
| 28 | Açık pozisyonlar `state.json`'a sadece bar kapanışında yazılıyordu — açılıştan ilk bara kadar (1h'e kadar) panel ve `status` "pozisyon yok" diyordu | orta | `_positions_view` açılışta da yayımlanıyor |
| 29 | Kod değişince launchd yeniden başlatmıyor; bot sessizce eski kodu çalıştırmaya devam ediyordu | orta | `bot.sh status` kaynak dosya mtime'ını süreç başlangıcıyla karşılaştırıyor |
| 31 | **Yerel sıfırlama sonrası girişler reddediliyordu.** Borsa client id'leri emrin ömründen sonra da hatırlıyor; `_find_by_client_id` sadece *açık* emirlere baktığı için iptal edilmiş eski emir görünmüyor, yeniden gönderiliyor, `170141 Duplicate clientOrderId` alıyordu. 3 red sonrası sembol evrenden düşüyordu | yüksek | `_resolve_duplicate`: dolan/açık **benimsenir**, iptal edilmiş yeni id ile tekrar gönderilir |
| 33 | **Panel getiriyi 100'e bölüyordu** — sabit kodlanmış eski hesap büyüklüğü. 1014 USDT'lik hesap `%914.8` getiri gösteriyordu. Başlangıç sermayesi state'e hiç taşınmıyordu | yüksek | `EngineState.starting_equity` + panel ona bölüyor |
| 35 | **`_enter` beş yerden sessizce çıkıyordu** — tarama satırının sebebi hiç güncellenmiyordu, elenen sembol panelde `eligible` görünüyordu. "Neden işlem yapmadı" tablosunun tam olarak engellemek için var olduğu şey | yüksek | her red yolu `no_trade_reason` yazıyor; ulaşılmayan adaylar da işaretleniyor |
| 36 | **`upsert_position` çakışmada giriş fiyatını güncellemiyordu.** Çakışma anahtarı `symbol`; aynı sembolde açılan **yeni** pozisyon eskisinin `entry_price`/`initial_stop`/`entry_ts`'ini miras alıyordu. R, MAE, MFE — hepsi başka bir işlemin girişine göre ölçülüyordu | kritik | tüm kimlik alanları güncelleniyor |
| 37 | **Pozisyon `meta`'sı DB'den hiç geri yüklenmiyordu.** İçinde `stop_order_id` var: her restart'ta motor kendi koyduğu borsa stop'unun tutamağını kaybediyor, trailing ilerleyemiyordu. Ayrıca kendi stop'unu sonsuza kadar "orphan" diye raporluyordu | kritik | `meta` yükleniyor |
| 40 | **Conviction-çöküş çıkışı hiç tetiklenmiyordu.** Belgelenen 4 katmanlı çıkış aslında 3 katmandı: `conviction_now=None` hem canlıda hem backtest'te sabit geçiliyordu | yüksek | **iki turda düzeltildi — bkz. #48.** İlk tamir işe yaramadı ve etkisi yanlış ölçüldü |
| 39 | **`state.json` sadece bar kapanışında yazılıyordu.** Saatlik barda takılmış motor bekleyen motordan bir saat boyunca ayırt edilemiyordu; ölü-adam anahtarının eşiği de bu yüzden 90 dakikaya gevşetilmişti — yani yakalamak için var olduğu durumu 90 dakika boyunca kaçırıyordu, pozisyonlar o süre yönetilmeden | yüksek | bekleme döngüsünde 60s heartbeat; `healthcheck` eşiği 5400s → 600s |
| 38 | Mutabakat borsada olmayan pozisyonu bellekten düşürüyor ama DB satırını bırakıyordu — her açılışta tekrar yükleniyor, tekrar düşürülüyor, tekrar uyarıyordu | orta | `delete_position` |
| 34 | **Sıfırlama sonrası hayalet pozisyon.** `_resolve_duplicate` borsa geçmişindeki "Filled" emri benimsiyordu; ama o dolumun malı sıfırlamada satılmıştı. 1000 USDT'lik hesaba 1699 USDT'lik pozisyon yazıldı, nakit −699 oldu | kritik | dolumu benimsemeden önce envanter doğrulanıyor; yoksa bar atlanıyor |
| 32 | Satılamayacak toz bakiye (0.00431 VVV, `min_amount` 0.01) her açılışta "unmanaged position" diye uyarıyordu — hiçbir eylemin temizleyemeyeceği bir uyarı, kanalı görmezden gelmeyi öğretir | düşük | `fetch_positions` `min_amount` altını atıyor |
| 30 | **Motor kapalıyken değişen bakiye sessizce kabul ediliyordu.** Implausible-jump koruması sadece bir koşu *içinde* bar-bar karşılaştırıyor; 50.063 → 1.763 (%97 düşüş) yeniden başlatmada yeni taban olarak benimsendi. Restart arası bir değişimi piyasa açıklayamaz: para çekimi, elle işlem, yanlış hesap ya da sıfırlama | yüksek | `_check_balance_against_last_run` |

### Dördüncü tur (2026-09-18, bar-arası katman kurulurken)

Hepsi gerçek borsaya sorularak bulundu; hiçbiri test paketine takılmamıştı.

| # | sorun | ciddiyet | durum |
|---|---|---|---|
| 41 | **Motor kendi borsa stop'unun dolduğunu bar kapanışına kadar öğrenmiyordu** — saatlik barda 1 saate kadar. O süre boyunca portföy yanlış, equity yanlış, pozisyon slotu boşa kilitli ve sonraki bar olmayan bir pozisyon için ikinci satış emri gönderiyor. `_manage_positions` borsaya sormuyor, `_reconcile` sadece açılışta çalışıyor | yüksek | `_detect_venue_exits`, 60s'de bir, iki okuma şartıyla |
| 42 | **`_score_category` blok collapse uygulamıyordu** — değişmez kural 3 ihlali. Kategori içi yönlü indikatörlerin **ham ortalaması** alınıyordu, blok temsilcisi değil | orta | düzeltildi; **ölçülen etki ~sıfır** (PF 1.41 → 1.41, expectancy +0.364 → +0.362R). Gerçek sızıntı başka yerdeymiş: `vwap_distance`'ın blok arkadaşları (`rsi`, `bb_percent_b`, `donchian_pos`) **başka kategorilerde**, dolayısıyla kategori-içi collapse onları yakalayamaz. Asıl soru `vwap_distance`'ın `flow` sayılması — kategorizasyon sorunu, collapse sorunu değil. Sonucu değiştireceği için sessizce yapılmadı, işaretlendi |
| 43 | **Bybit public spot WS 10 topic sınırı — sessiz askıda kalma.** 21 sembolün hepsi tek subscribe ile isteniyordu; 11'inci sembolde future hiç çözülmüyor. Hata yok, exception yok, reconnect yok. Tüm evren kararıyor ve loglarda hiçbir şey yazmıyor | yüksek | 10'luk parçalara bölündü, her parça bağımsız reconnect ediyor. Ölçüm: 21/21 sembol 3 saniyede |
| 44 | **Spread ve hacim verisi hiç toplanmıyordu.** `ScanRow.spread_bps`/`volume_24h` hep 0, conviction'ın ceza dalları erişilemez, `NoTradeReason.SPREAD_TOO_WIDE` hiç atanmıyor, `universe.max_spread_bps: 8.0` hiçbir kod tarafından okunmuyor | orta | tek istekle 21 sembol için toplanıyor ve `scans` tablosuna yazılıyor. **Ceza dalları kapalı** (`microstructure_penalties: false`) — ölçülen spread 0.01–2.7bps, ceza tabanı 10bps, yani bugün hiçbir şeyi değiştirmezdi; bu da bir bulgu |
| 45 | **Bot çalışırken alınan backtest ölçümü tekrarlanabilir değil.** İki koşu arasında 113 → 114 işlem, PF 1.41 → 1.39 farkı çıktı ve kod bit-bit aynıydı: motor her barda 63 parquet serisini yeniden yazıyor | orta | A/B'ler artık `--to <tarih>` ile sabitleniyor. Ölçüm disiplini sorunu, işlem güvenliği sorunu değil — ama yanlış atıf üretiyordu |

| 46 | **Motor barları 7-38 dakika geç işliyordu.** Bekleme döngüsü `slept += step` ile *niyet edilen* süreyi topluyor, `time.sleep()` dışında geçen zamanı hiç saymıyordu. Telegram `getUpdates` bu makineden **3-8 saniye** sürüyor ve her 5s tick'te çağrılıyordu → her tur ~9s gerçek, 5s kayıtlı, döngü %80 aşıyor. Giriş fiyatı yarım saat bayat bir kapanıştan hesaplanıyordu ve **hiçbir logda tek satır yoktu** | yüksek | `_wait_for_next_bar`: bütün zaman aşımları `time.monotonic()`'e karşı; Telegram 15s'lik yavaş şeride alındı, poll timeout 5s→3s. Ölçüm: nabız aralığı ~110s → **64s** (hedef 60) |

Bu sonuncusu, bir saat önce eklediğim ölü-adam nabzının kendisi tarafından
yakalandı: nabız 60 saniyede bir atacaktı, 110 saniyede atıyordu. Bir ölçüm
aracı kurup ilk işinin kendi çatısındaki bir arızayı göstermesi, aracın doğru
kurulduğunun kanıtı.

| 47 | **#40'ın ilk tamiri no-op'tu ve etkisi yanlış atfedildi.** `conviction_now` bağlandı ama sonuç değişmedi — bot durdurulup sabit arşivle ölçülünce açık/kapalı **birebir aynı**. Sebep: tamirin içine koyduğum koruma, `no_trade_reason is not None` olan her skoru "fikrim yok" sayıyordu. Ama `below_threshold` engelleme değil, **gerçek bir skor** — ve çöken bir tez tanımı gereği eşiğin altındadır. Ölçüldü: 391 bar çöküş eşiğinin altında skorlandı, 391'i de atıldı | yüksek | `UNSCORED_REASONS`: yalnız *skorlanmadan* kısa devre yapan sebepler "fikrim yok" sayılır |
| 48 | **Çalışır hale gelince conviction-çöküş çıkışı sonucu KÖTÜLEŞTİRDİ.** 24 çıkış üretti: PF 1.23 → 1.12, expectancy +0.179 → +0.100R, getiri +%5.75 → +%2.32, yakalama %8.7 → %5.6. Hâlâ çalışan kazananları kesiyor | — | `collapse_threshold: 0.0` (kapalı). Mekanizma doğru ve test edildi; **seviye onaylanmadı** |
| 50 | **`risk.vol_lookback_bars` config'te duruyordu ve hiçbir kod okumuyordu** — motor iki yerde, backtest iki yerde 30'u sabit yazmıştı. `universe.max_spread_bps`'den sonra ikinci ölü ayar. Pydantic bilinmeyen anahtarı sessizce yutuyor, yani modelin hiç büyümediği alan ile büyüdüğü alan aynı görünüyor | orta | alan modele eklendi (240), ve her config bölümü artık `extra="forbid"` — yazım hatası açılışta patlıyor. 30 saatlik pencere yıllık vol tahmini için çok kısa: evren genelinde p90/p10 4.0x (240'ta 3.2x), ölçek bar başına medyan %0.87 zıplıyor (240'ta %0.09) |
| 51 | **Kısmi kâr alma sezgiyi doğrulamadı.** 0.00R biten işlem 31→3 oldu ama PF 1.19→1.12, getiri +%6.04→+%2.60. Sebep: +1R'de yarısını satmak, sonradan 3R+'ye koşan pozisyonu da yarıya indiriyor. Vasatlarda $80 kurtarıp iyilerde $108 veriyor | — | mekanizma kuruldu ve test edildi, `scale_out_at_r: 0.0`. Düşüşü %15.8→%12.8 indiriyor, yani risk/getiri takası — "daha iyi" değil |
| 49 | **PF 1.20→1.41 ve PBO 1.00→0.33 iyileşmesi #40'a atfedilmişti. Yanlış atıf.** İki koşu arasında arşiv büyümüştü (bkz. #45) ve tamirin kendisi no-op'tu. Sayılar doğru — bugünkü sabit ölçüm de PBO 0.33 / 6'da 5 veriyor — ama **sebep A1 değildi** | orta | atıf CLAUDE.md ve `hypotheses.md`'de düzeltildi; A/B'ler artık bot **durdurularak** ve `--to` ile yapılıyor |

Denetim #47 ve #49 birlikte bir ders: **kendi tamirimin ölçümüne en az başkasınınki
kadar şüpheyle bakmalıyım.** #40'ı "canlandırdım" diye yazdım, sonuç iyi göründü,
ve iyi görünen sonucu kimse soruşturmaz — tam olarak `hypotheses.md`'ye "iyileşen
bir tamir daha fazla şüphe hak eder" diye yazdığım şeyi kendim yapmadım.

### Beşinci tur (2026-09-19, demo koşusu sürerken rutin kontrolde)

| # | sorun | ciddiyet | durum |
|---|---|---|---|
| 52 | **Panel her istekte yeni SQLite bağlantısı açıyor, hiçbirini kapatmıyordu.** `web/app.py` içindeki `db()` her çağrıda `Database(cfg.db_path)` kuruyordu. Ölçüldü: **~1 fd/sn**; launchd'in `maxfiles` soft limiti **256**. Panel birkaç dakikada kör oluyor — `[Errno 24] Too many open files` state.json'da (200 kez), `unable to open database file` her sorguda (629 kez) — KeepAlive kaldırıyor ve tırmanış baştan başlıyor. `web.err.log` 5.0 MB traceback. Ayrıca `Database.__init__` şema scriptini + `meta` UPSERT'ünü çalıştırdığı için **okuma amaçlı panel motorun DB'sine istek başına yazıyordu** | yüksek | süreç ömrü boyunca tek bağlantı; `lifespan` ile kapatılıyor; `sqlite3.threadsafety < 3` ise açılışta reddediliyor (endpoint'ler sync, FastAPI threadpool'unda paylaşılıyor). Ölçüm: 960 istek sonrası fd sabit, `web.err.log` 0 bayt. → `test_web.py` |

Bunun hiçbir işlem güvenliği etkisi yok — panel ayrı süreç ve salt-okur. Ama
**gözlem katmanının kendisi arızalıydı** ve bunu hiçbir şey raporlamıyordu:
`bot.sh status` motora bakıyor, `healthcheck.py` motora bakıyor, panelin
çöküşünü yalnız kendi stderr'i biliyordu. Denetim #24'ün ("`bot.jsonl` hiç
yazılmıyordu") aynı sınıftan bir tekrarı: ölçüm aracının ölçülmemesi.

### Altıncı tur (2026-09-20, ikinci motor kurulurken)

Üçü kod okurken, üçü borsaya sorarak bulundu.

| # | sorun | ciddiyet | durum |
|---|---|---|---|
| 53 | **Canlı motor `conviction.selection`'ı hiç okumuyordu.** Config `cross_sectional` diyor, backtest uyguluyor, canlı runner'da sıralayıcı **yok** — canlı mutlak eşikle (62) çalışıyor. Backtest üst desili alıp eşik altındakileri `rank_absolute_floor`'a (40) kadar geri kabul ediyor, yani 40–62 bandında backtest işlem açıyor canlı açmıyor. **Repodaki her sayı backtest'in cevabından geliyor** | yüksek | `Runner._select` eklendi. Bot #1'in işlem popülasyonunu değiştirir → restart operatörün kararına bırakıldı |
| 54 | **`_balances` başarısız okumada boş sözlük dönüyordu**, bu da "hesap boş" ile aynı değer. 4 saatlik kesintide yerinde duran pozisyon `position_inventory_missing` diye işaretlendi; kurtaran şey stop sorgusunun da aynı anda düşmesiydi — tasarım değil şans | yüksek | `None` dönüyor; `_reconcile` okunamayan borsayla mutabakatı reddediyor |
| 55 | **`client_order_id`'de motor kimliği yoktu.** `cross_sectional` modda iki motor aynı barda birebir aynı id üretiyor; ikincisinin `submit`'i birincinin emrini benimsiyor | kritik (ikinci motor için) | `namespace` eklendi. Boş isim alanı eski id'yi bit-bit koruyor → çalışan motor migrasyon istemedi |
| 56 | **`linear_symbol` zaten kontrat olan sembole quote'u ikinci kez ekliyordu** (`SOL/USDT:USDT:USDT`). Perp evreni sessizce **sıfır** funding, sıfır OI, sıfır long/short çekti; `derivatives` kategorisi (skorun %10'u) hiçbir şeye oy vermedi. Güvenli bozuluyor, o yüzden fark edilmesi zor | orta | düzeltildi; ölçüm: funding 2.191, OI 6.000, ls 6.000 satır/sembol |
| 57 | **Kök `Config` bilinmeyen anahtarı yutuyordu.** Değişmez kural 50 her *bölümü* kapatmıştı ama kökü değil — üst seviye bir yazım hatası okunuyormuş gibi duruyordu | orta | kök de `extra="forbid"` |
| 58 | `fetchTradingFees` perp'te `Illegal category`, demo'da hesap fee endpoint'i de 10001 — yani okunacak bir katman yok. Kalıcı `fee_fetch_failed` uyarısı üretiyordu | düşük | `fee_tier_unavailable` info'ya indi, market metadata kullanılıyor (probe'un ölçtüğüyle aynı: %0.06/%0.01) |

### Yedinci tur (2026-09-20, rutin "bot nasıl gidiyor" kontrolünde)

Dördü de motor çalışırken, borsaya sorarak bulundu. Hiçbiri teste takılmamıştı.

| # | sorun | ciddiyet | durum |
|---|---|---|---|
| 59 | **Kaydedilen giriş fiyatı hiç ödenmemişti.** Bybit'in create-order yanıtı dolumu taşımıyor (`filled=0`, `average=0`, `fee=0`), `_merge` de sıfırları yazıyor; `average_price or price` planlanan fiyata düşüyordu. Borsaya soruldu: ETH gerçekte 2587.76'dan doldu, DB 2588.06 diyordu; VVV 26.078 doldu, DB 26.171. **R, MAE, MFE hepsi ödenmemiş bir fiyattan ölçülüyordu** (VVV'de 1R %3.5 şişik). Aynı sebeple `orders.status` sonsuza dek `open` kalıyor ve kısmi dolum dalı erişilemez oluyordu | yüksek | `_settle`: market emri gönderildikten sonra borsadan okunuyor |
| 60 | **Kayma izleyicisi yapısal olarak ölüydü.** `slippage.record(price, fill_price)` çağrısı `fill_price == price` aldığı için sürüklenme **her işlemde tam 0.0**; `median_bps > assumed_bps` (5.0) hiçbir örneklem sayısında doğru olamazdı. Denetim #9 "kayma ölçümü eklendi" diyordu — mekanizma kuruluydu, veri hiç gelmiyordu. #40 ile aynı sınıf | yüksek | #59 ile birlikte düzeldi. Ölçüm: ETH −1.2bps, VVV −35.5bps (ikisi de lehte) |
| 61 | **Komisyon defterde hiç görünmüyordu.** `fills.fee` 3/3 satırda 0.0, `positions.fees_paid` 0.0, çıkış komisyonu varsayılan orandan hesaplanıyordu. Kapanan ilk işlemde `trades.fees` sıfır çıkacaktı, yani PnL tur maliyeti kadar şişik | orta | gerçek `cumExecFee` okunuyor, base→quote çevriliyor (değişmez 61) |
| 62 | **Perp emir aramaları `category: "spot"` sabitliydi** ve sembolü `SOLUSDT:USDT` yapıyordu. `order_state`, `_find_in_history` — ikisi de boş liste alır, "emir yok" der. `fast` motoru başlatılmadığı için ısırmamıştı; `_detect_venue_exits` ve çift-id çözümü orada kör olacaktı | orta (fast için yüksek) | `category` sınıf niteliği + `_venue_symbol` |
| 63 | **`healthcheck.py` iki motoru ayırt etmiyordu.** `pgrep -f scripts/run.py`; `--config config/fast.yaml` ile koşturunca **demo'nun pid'ini** "process alive" diye raporladı. `ops/bot.sh` bu hatayı `code_is_stale`'de zaten düzeltmiş ve yorumunda tarif etmiş, düzeltme healthcheck'e taşınmamıştı | orta | pattern config'i içeriyor |
| 64 | **`code_is_stale` tüm `config/` dizinini tarıyordu**, bu yüzden `config/fast.yaml`'a dokunmak *demo*'nun "eski kod çalıştırıyor" uyarısını tetikliyordu — hiçbir restart'ın temizleyemeyeceği bir uyarı. Denetim #32'nin dersi | düşük | motorun kendi config'i + `extends` ebeveyni + `src`/`scripts` |

#59–#61 tek kök: **borsanın onayı ile borsanın yaptığı şey aynı sanılmıştı.**
Emir kabulü bir makbuz değil; makbuzu ayrıca istemek gerekiyor. Bu üçü birlikte,
repodaki "ölçüm aracının kendisi ölçülmemiş" serisinin (#24, #40, #52) dördüncü
üyesi — ve bu kez aracı bozan şey, aracı besleyen verinin hiç var olmamasıydı.

### Sekizinci tur (2026-09-20, `feat/capital-recycling` kod incelemesinde)

Yedisi de kod okunarak bulundu. Hiçbiri teste takılmamıştı, ve hiçbiri bugüne
kadar ısırmamıştı: **short tarafını açan diff'in kendisi onları erişilebilir
yaptı.** `fast` motoru başlatılmadığı için hepsi hâlâ ölü daldaydı.

| # | sorun | ciddiyet | durum |
|---|---|---|---|
| 65 | **Canlı short girişi long olarak kaydediliyordu.** `Fill(side=Side.BUY)` literal; diff emri gerçek `side` ile kuruyor ama kaydı kurmuyor. `open_position` `position.side`'ı fill'den okuyor → PnL işareti ters, stop girişin yanlış tarafında, çıkış koşulu hiç sağlanamaz. Emir doğru gidiyor, **yalnız kaydı yanlış** — mutabakatın karşılaştıracağı iki taraf da bu kayda bakıyor | kritik | `side=side`; kayma da `side.sign` ile ölçülüyor |
| 66 | **Paper modda her short sessizce düşüyordu.** `_apply_paper_fill` `fill.side is not Side.BUY` ile dönüyordu — bu bir giriş/çıkış ayracıydı ve yalnız her giriş alışken doğruydu. Simülatör dolduruyor ve bakiyeyi oynatıyor, motor hiçbir şey kaydetmiyor: stop yok, çıkış yok, sembol bir sonraki barda yine boş, equity koşunun geri kalanında yanlış. H17'nin ileri testi paper'da başlarsa ölçtüğü şey strateji olmazdı | yüksek | ayraç emrin üstündeki **plan** (`"stop" in meta`); çıkışlar zaten `force_fill_at` ile doluyor, bu yola hiç uğramıyor |
| 67 | **Perp'te trailing stop borsaya hiç ulaşmıyordu.** `PerpBroker` `place_stop`/`cancel_stop` override ediyor, `amend_stop` etmiyor. `place_stop` gerçek id değil `position:<sembol>` dönüyor; miras alınan `amend_stop` bunu `edit_order`'a veriyor, borsa reddediyor, exception yakalanıyor, `False`. Yerel defter ilerletilmiş stop'u gösterirken borsa pozisyonun ömrü boyunca **ilkini** tutuyordu. Açılan fark tam da sürecin kapalı olduğu anda anlam kazanıyor — borsa tarafı stop'un var olma sebebi (değişmez 12) | yüksek | `amend_stop` override: perp'te stop pozisyonun özelliği, düzenlenmez, **üzerine yazılır** |
| 68 | **Borsa kaldıracı kazanılmamış tavana kuruluyordu.** `build_broker` `PerpBroker(leverage=config.risk.max_leverage)`; boyutlandırmanın her çağrı yeri ise `gate.allowed_leverage(...)`. `max_leverage: 3.0` ileriye dönük bir yetkilendirme — kapı geçilene dek motor 1x boyutlandırırken hesap 3x'te duruyordu, yani pozisyon %100 değil ~%33 teminatlı. Değişmez 57'nin açık tutmak için var olduğu likidasyon mesafesi | yüksek | borsa ve boyutlandırıcı **aynı sayıyı** okuyor |
| 69 | **`_resolve_duplicate` okunamayan bakiyede çöküyordu.** Denetim #54 `_balances`'ı `None` dönecek şekilde değiştirdi, iki çağrı yeri güncellendi, bu kaldı: `self._balances("total").get(...)` → `AttributeError`. Yol egzotik değil — yinelenen id'yi üreten şey zaten ağ hıçkırığı, yani burada başarısız olması en muhtemel okuma tam da buna bağlı olan | orta | bar atlanıyor: "hiç tutmuyor" gerçek pozisyonu atar, "tutuyor" olmayanı icat eder |
| 70 | `RiskConfig.vol_lookback_bars` **iki kez** tanımlıydı (`ge=5`, sonra `ge=10, le=2000`). Sınıf gövdesinde sonraki kazanıyor; ilki ölü koddu — ona yapılan bir düzenleme çalışan motorda hiçbir şeyi değiştirmezdi. Değişmez 50'nin kapattığı "okunuyormuş gibi duran ayar" sınıfı, başka bir kapıdan | düşük | ölü tanım kaldırıldı; sınır testle sabitlendi (yalnız ölü tanım 5'i kabul ediyordu) |
| 71 | `LeverageGate.evaluate` `min(user_ceiling, 2.0)` ile kısıyordu. Bu literal config sınırının bayat bir kopyasıydı; sınır 3.0'a taşınınca 3x'i açıkça yetkilendirmiş **ve kazanmış** operatör sessizce 2x'te tutulacaktı. `allowed_leverage`'da aynı literal temizlenmiş, kardeşi unutulmuştu — üstelik sayıyı **kalıcılaştıran** çağrı yeri bu, yani kısıtlama onu üreten koşudan sonra da yaşıyordu | düşük | `granted = user_ceiling`; değişmez 8 etkilenmiyor, tavan hâlâ operatörün |

#65–#67 tek kök ve repoda yeni bir sınıf: **ölü dal, canlanana kadar doğru
görünür.** Üçü de spot-yalnız dünyada bit-bit doğruydu — `Side.BUY` literali
tek olasılığı yazıyordu, `fill.side` gerçekten giriş/çıkış ayracıydı, `amend_stop`
gerçek bir id alıyordu. Short'u mümkün kılan diff hiçbirine dokunmadı ve hepsini
yanlış yaptı. Test paketi 440'ta yeşildi çünkü testler de aynı dünyadan geliyordu.

Bu yüzden yedisinin de artık kendini koruyan bir testi var (448), ve her biri
fix'i geri alarak **doğrulandı** — denetim #47'nin dersi: tamirin işe yaradığını
göstermeyen bir test, tamirin kendisi kadar değersiz.

### Ölçülmüş borsa gerçekleri — linear perpetual (2026-09-20, demo'da emirle)

`scripts/perp_probe.py` ile tekrar üretilebilir. Tam çıktı: `research/perp_venue_facts.md`.

| deneme | sonuç |
|---|---|
| Aynı pozisyonda stop **ve** kar-al, tek çağrıda | ✅ kabul — spot'ta `170131` idi (değişmez kural 39/56) |
| Native trailing stop | ✅ kabul — spot'ta reddediliyordu |
| Hesap tipi | UTA (`unifiedMarginStatus=3`), `marginMode=ISOLATED_MARGIN` |
| Pozisyonun raporladığı `tradeMode` | ❌ `0` (cross) — **hesap bayrağı etkisiz**, bkz. değişmez kural 57 |
| 1x short likidasyon mesafesi | girişten **+%99**; `positionIM` = notional'ın %100'ü |
| Perp işlemi spot USDT'yi oynatıyor mu | ✅ **evet** — cüzdan paylaşımlı, izolasyon inşa edilmeli |
| `$300` @ %1.2 risk, %2.6 stop → $138 notional | **400/400 sembol** ifade edilebiliyor (spot'ta $100'da adayların %97.5'i eleniyordu) |
| Komisyon (market metadata, emirle doğrulandı) | taker %0.06, maker %0.01 → tur %0.12 taker, **%0.07 maker-giriş/taker-çıkış** |

### Ölçülmüş borsa gerçekleri (2026-09-18, demo hesapta tek tek denendi)

Varsayım değil, ccxt bayrağı değil — gerçek emirlerle sınandı. `fetchTradingFees`
tam bu şekilde yanıltmıştı: ccxt "destekleniyor" dedi, Bybit **spot** reddetti.

| deneme | sonuç |
|---|---|
| Açık stop varken kar-al emri | ❌ `170131 Insufficient balance` — stop 1.31 VVV'yi kilitliyor, serbest 0.003 |
| Tek emirde stop + kar-al (OCO) | ❌ `InvalidOrder: only supports contract` |
| Native trailing stop (spot) | ❌ aynı red |
| `triggerDirection` ile trigger emri | ❌ `NotSupported: does not support triggerDirection for spot markets` |
| `triggerPrice` + `orderFilter=StopOrder`, yön belirtmeden | ✅ kabul, `Untriggered` olarak duruyor, iptal edilebiliyor |
| `publicGetV5MarketTickers` tek çağrı | ✅ 536 spot sembol; 21/21 evren için spread + `turnover24h` |
| WS `watch_tickers`, 10 sembol / 11 sembol | ✅ 2sn / ❌ süresiz askıda |

**Sonuç:** borsa tarafı kar-al ve trailing spot'ta **yapısal olarak imkânsız**.
Bu yüzden stop borsada, hedef motorda (değişmez kural 39). Kurulu kırılım girişi
ise mümkün — yön borsa tarafından çıkarsandığı için tetik fiyatının son fiyatın
**üstünde** olduğu her yerleştirmede doğrulanıyor, yoksa satış-stop'u olarak
okunup anında dolar.

**Açık, düzeltilmedi:** `report.py` ve panel `run_id`/`mode` filtrelemiyor. `runs`
tablosunda mod var (`paper` / `live`), ama equity eğrisi ve işlem istatistikleri
paper ($1.000 ölçek) ile demo ($50.000 ölçek) koşularını tek seriye karıştırıyor.
İşlem güvenliği sorunu değil, **rapor doğruluğu** sorunu. Çözüm: sorguları aktif
moda göre filtrelemek, ya da mod başına ayrı `runtime/` dizini.

### Eklenen özellikler

`data/feed.py` bar-arası WebSocket fiyat akışı (ccxt.pro, 10'luk parçalar,
düşerse bar-kapanışına döner) · `_detect_venue_exits` borsa stop'unun dolumunu
60s'de fark eder · motor tarafı kar-al (`exits.target_atr_mult`, **kapalı**) ·
kurulu kırılım girişi (`entries.armed_breakout`, **kapalı**) · spread/hacim
toplama (`execution.microstructure_enabled`) · panelde varlık dökümü ·
`ops/bot.sh` mod başına launchd üretimi + ön kontrol ·
`ops/reset.sh` + `scripts/venue_reset.py` demo hesabı ve yerel geçmişi sıfırlama ·
panel: eksenli equity grafiği (başlangıç çizgisi, üstü yeşil / altı kırmızı), uyarı
şeridi (degraded / nabız durdu / feed bayat / kayma), açık pozisyonda anlık R ve
stop mesafesi, engellenmiş sembol `—` gösteriyor (`/api/meta`), pause/resume/flatten
düğmeleri · `engine/lock.py` tek instance · `risk/capital.py` hesap büyüklüğü teşhisi ·
`execution/safety.py` anahtar izni + kayma ölçümü + imkânsız equity koruması ·
`scripts/healthcheck.py` dead-man + DB yedeği · `scripts/divergence.py` Kapı 2 ·
`scripts/plateau.py` parametre platosu · `scripts/polarity.py` türev sinyal yönü ·
sermaye dondurma zemini (%50) · emir reddi sayacı (3 red → sembol düşer) ·
log rotasyonu · `caffeinate -i` · `config/slow.yaml` · README.md

Performans ölçüldü, sorun değil: 21 sembol tam tarama 6.5s + 63 REST çağrısı 8s,
1h bar için fazlasıyla yeterli. 1m barda yeniden ölçülmeli.

## Dizin haritası

| Yol | İçerik |
|---|---|
| `core/models.py` | Veri sözleşmesi. R-birimi, MAE/MFE, `NoTradeReason` |
| `core/config.py` | pydantic doğrulama. Geçersiz config = açılışta hata |
| `core/clock.py` | UTC ms, bar sınırları |
| `core/db.py` | SQLite. Bybit demo 7 gün tutar; asıl arşiv bu |
| `data/store.py` `backfill.py` `integrity.py` | Parquet arşiv, resumable çekim, kalite kapısı |
| `indicators/ta.py` | 22 indikatör, dış TA kütüphanesi yok |
| `indicators/registry.py` | Ham → [-1,+1]. DIRECTIONAL (oy) vs GATE (kapı) |
| `indicators/ic.py` | IC **+ tail analizi**. Hangi indikatörün yaşayacağına karar verir |
| `indicators/redundancy.py` | Korelasyon blokları |
| `strategy/regime.py` | İzin katmanı |
| `strategy/setups.py` | Giriş tetikleyicileri + stop yerleşimi |
| `strategy/conviction.py` | Seçicilik motoru, 0-100, dinamik eşik |
| `strategy/exits.py` | 4 katmanlı çıkış, sabit süre yok |
| `risk/budget.py` `sizing.py` `leverage.py` | Sürekli merdiven, stop'tan boyut, promosyon kapısı |
| `execution/broker.py` `paper_broker.py` `ccxt_broker.py` | Tek arayüz, üç uygulama |
| `execution/reconcile.py` | Açılışta mutabakat; anlaşmazlıkta borsa kazanır |
| `data/feed.py` | Bar-arası WS fiyat akışı. Yalnız son/yüksek/düşük |
| `engine/runner.py` | Canlı döngü |
| `engine/state.py` `watchdog.py` | Atomik snapshot, devre kesici, degraded mode |
| `backtest/engine.py` `align.py` `metrics.py` | Replay, hizalama, metrikler |
| `backtest/cpcv.py` `dsr.py` `montecarlo.py` `excursion.py` | Kapı 1 aletleri |
| `llm/provider.py` `roles.py` `redact.py` | 5 rol, bütçe, whitelist redaksiyon |
| `web/app.py` | Yerel panel (localhost), motor başına sekme |
| `execution/ccxt_broker.py::PerpBroker` | Linear perpetual: short, borsa tarafı hedef, kaldıraç kapısı |

## Komutlar

```bash
./ops/pre-install.sh                                      # tüm kurulum + ölçüm
./ops/bot.sh start paper|demo|live|fast                   # 7/24 (launchd üretir)
./ops/bot.sh status | stop | restart | logs -f            # motor adı opsiyonel
./ops/bot.sh engines                                      # tanımlı motorlar

python scripts/backfill.py --years 3 --with-derivatives   # veri (resumable)
python scripts/ic_report.py --timeframe 1d                # indikatör ölçümü
python scripts/backtest.py                                # backtest + rapor
python scripts/cpcv_report.py --trials N                  # KAPI 1 kararı
python scripts/exit_optimizer.py                          # MAE/MFE kalibrasyon
python scripts/run.py --config config/paper.yaml          # canlı motor
python scripts/dashboard.py --config config/fast.yaml     # panel :8788 (sekmeli, iki motor)
python scripts/perp_probe.py                              # perp borsa gerçekleri
python scripts/report.py --since 30d                      # durum raporu
python scripts/llm_impact.py                              # LLM karşı-olgusal
```

## Gözlem (Claude buradan bakar)

`runtime/state.json` (anlık durum, atomik yazılır) · `runtime/logs/*.jsonl`
(her satırda `event`/`symbol`/`bar_ts`/`reason`) · `runtime/tbot.db`
(`scans` tablosu **her barda neden işlem yapılmadığını** tutar) ·
`runtime/reports/*.md`

## Ölçülmüş bulgular (2026-09-17, 21 sembol × 3 yıl, 620k bar)

Varsayım değil, bu repodaki veriyle ölçüldü.

- **7 indikatör tek bloğa düştü** (4h, maks |ρ|=0.955): rsi, macd, stoch_rsi,
  roc, bb_percent_b, donchian_pos, vwap_distance. 18 yönlü → 12 bağımsız blok.
- **IC ile tail analizi çelişiyor, tail haklı.** Tüm barlarda IC ≈ −0.02, hiçbir
  t-istatistiği 2'yi geçmiyor. Ama üst desilde güçlü momentum var — ilişki
  monotonik değil. Bot ortada işlem yapmadığı için karar ölçütü tail.
- **Uzun ufuk >> kısa ufuk.** 1d, 20 bar: `price_slope` üst desil fazlalığı
  +%5.97, blok-bootstrap CI [+%4.58, +%12.44]. 4h 12 barda ~%0.8.
- **TREND_UP'ta etki büyüyor** (rsi h=20: +%9.34 fazlalık, %65 isabet).
- **Çıkış zaman dilimi tutuş süresini belirliyor.** Trailing ATR'yi trigger'dan
  (1h) hesaplamak çok haftalık pozisyonu saatler içinde stop'luyordu; 23 işlemin
  hepsi trailing stop'la kapanmıştı. `exits.trail_timeframe: permission` ile
  medyan tutuş 1.5 gün → 3 gün, maks 26 gün.
- **Funding contrarian DEĞİL.** Yaygın kabulün tersi: 5/5 ufukta yüksek funding
  daha *yüksek* getiri öngördü. 1d/20 bar: üst desil +%5.31 vs taban +%2.28,
  CI [+%2.49, +%7.70]. `long_short` ise gerçekten contrarian (5/5).
  `scripts/polarity.py` ile tekrar ölçülebilir — polarite sabit değil.
- **Sermaye kısıtı evreni sessizce değiştiriyor.** $100'da risk-doğru boyut ancak
  stop **%13'ün altındaysa** borsanın $5 minimumunu geçiyor. Volatil semboller
  hiç işleme girmiyor, yani canlı evren backtest evreninden farklı. $300 bu
  sınırı %39'a çıkarıyor. Artık açılışta raporlanıyor (`risk/capital.py`).
- **Uyarı:** örneklem 2023-09..2026-09, taban oran pozitif. Ham getiri değil
  **taban üstü fazlalık** ölçülüyor; CPCV + DSR hâlâ zorunlu.

## Zaman dilimi ekonomisi (ölçüldü)

Gidiş-dönüş maliyet ~%0.3. Tipik bar hareketi ne kadarını yiyor:

| tf | medyan hareket | maliyet / hareket |
|---|---|---|
| 15m | %0.24 | **%124** — maliyet hareketten büyük |
| 30m | %0.34 | %88 |
| 1h | %0.48 | %62 |
| 4h | %0.94 | %32 |
| 1d | %2.58 | %12 |

Düşük zaman diliminde strateji sorunu yok, **aritmetik** sorunu var. Ama maliyet
bar başına değil işlem başına ödeniyor: düşük tf `trigger` olarak bedava giriş
hassasiyeti verir. Belirleyici olan zaman dilimi etiketi değil **tutuş ufku**.

## Kapı 1 durumu: GEÇİLMEDİ

Strateji v1 ölçüldü (`runtime/reports/cpcv.md`):

| kontrol | değer | |
|---|---|---|
| işlem ≥ 50 | 116 | ✅ |
| profit factor ≥ 1.2 | 1.36 | ✅ |
| maks düşüş ≤ %20 | %15.8 | ✅ |
| DSR anlamlı | p=1.000 | ❌ |
| CPCV yollarının çoğu pozitif | %51.1 | ✅ |
| PBO ≤ 0.5 | 0.33 | ✅ |

Altı kontrolden **beşi geçiyor**; kalan tek engel DSR. `--trials 11`, bot
durdurulmuş, tekrarlanabilir (iki ardışık koşu birebir aynı).

⚠️ **Bu iyileşme bir kod değişikliğine atfedilemez.** Önce denetim #40'ın tamirine
bağlanmıştı; sabit ölçüm o tamirin no-op olduğunu gösterdi (#47, #49). PBO'nun
1.00'den 0.33'e düşmesi arşivin büyümesiyle geldi — yani *daha fazla veri*, daha
iyi kod değil. Bu, DSR'ın hâlâ neden reddettiğini de açıklıyor.

DSR bu stratejiyle değil **deneme sayısıyla** ilgili — oradan çıkış ayar değil
taze veri.

Ölçüm `--trials 16` ile, bot durdurulmuş, tekrarlanabilir. Aynı koşu `--trials 1` ile DSR'ı p=0.352 gösteriyor —
deneme sayısını eksik saymak kapıyı geçirmeye yeten farkı üretiyor. Sayaç
`research/hypotheses.md`'de ve `ops/pre-install.sh` oradan okuyor.

### İki yapısal bulgu (v1 → v2)

**1. $100'da ölçülen şey strateji değildi.** Huni ölçümü: taramayı 1.246 aday
geçiyor, **1.215'i (%97.5) `min_notional`'a takılıyor.** Borsanın $5 minimumu,
$0.75 risk bütçesiyle ancak stop %13'ten darsa aşılıyor. Yani sadece dar stop'lu
kurulumlar işleme giriyordu — strateji değil, bir sermaye kısıtı ölçülmüştü.

**1b. Minimum sermaye ölçüldü: $183.** Boyutlandırma zinciri
`1R = bakiye × risk × vol_ölçeği`, `notional = 1R / stop%`, borsa $5 altını
reddediyor. Ters çevirince: hesap ancak `1R/$5.75` kadar geniş stop ifade
edebiliyor. $100'da bu %13 — kurulumların %84'ü geçiyor, %16'sı (volatil olanlar)
sessizce atlanıyor. Volatilite ölçekleyici de aynı sembolleri küçülttüğü için iki
etki birleşiyor. `risk.min_equity` olarak zorlanıyor.

**2. Ölçtüğüm şeyi uygulamamışım.** Tail analizi **kesitsel**: her barda
semboller birbirine karşı sıralanıp üst desil alınıyor. Conviction motoru ise
her sembolü **mutlak bir eşiğe** karşı puanlıyordu. Farklı sorular. Mutlak eşik
kıtlık-bolluk üretiyor; göreli sıralama fırsat sayısını sabit tutuyor.
Düzeltince: 63→116 işlem, PF 0.97→1.34, expectancy −0.004→+0.280R.
`conviction.selection: cross_sectional` artık varsayılan.

Al-tut karşılaştırması: strateji **+%4.73**, BTC al-tut **+%194.9**.

**3. Volatilite ölçekleyici hiç çalışmamış.** `vol_target_annual: 0.35` bir hisse
portföyü hedefi; evrenin medyan yıllık volatilitesi **%79**. Ölçekleyici sürekli
tabanında (0.4x) sıkışıyor, her pozisyon niyetlenenin %44'ü olarak açılıyor,
sonra min_notional'a takılıyordu. Mekanizma muhafazakâr değil **işlevsizdi**.

0.80'e kalibre edilince sonuçlar **kötüleşti**: PF 1.34→1.16, getiri
+%3.66→+%0.95. Önceki sayının bir kısmı yapaydı — bozuk ölçekleyici farkında
olmadan şanslı bir alt küme seçiyordu. Düzeltme ölçümü güvenilir yaptı.

### İstatistiksel bütçe tükendi

9 denemeden sonra DSR'ın beklenen maks Sharpe eşiği ~1.5; gözlenen 0.24. Bu veri
setinde (21 sembol × 3 yıl) **daha fazla ayar yapıp sonuca güvenmek matematiksel
olarak mümkün değil.** PBO (1.00) aynı şeyi bağımsız söylüyor: her bölünmede
içeride en iyi olan konfigürasyon dışarıda medyanın altında kalıyor.

Kod yanlış demek değil. Aynı veriye kaç kez soru sorulduğunun bedeli var ve
ödendi. Çıkış yolu ayar değil **taze veri**: paper ileri testi (maliyeti sıfır,
ürettiği veri madenlenmemiş), daha fazla sembol/geçmiş, ya da tek seferlik test
edilen yeni hipotez sınıfı.

**Kümülatif deneme: 17** (kaynak: `research/hypotheses.md`). Bir sonraki CPCV
koşusunda `--trials 17` geçirin — DSR her denemeyi fiyatlıyor. 17'nci H17:
perp + short, ön-kayıtlı, ileri testte.
`ops/pre-install.sh` bu sayıyı o dosyadan okuyup otomatik geçiriyor.

Bu bir başarısızlık değil, sistemin çalıştığının kanıtı: altyapı yanlış bir
stratejiyi kârlı göstermek yerine reddetti. Sıradaki iş strateji araştırma
döngüsü — hipotez → backtest → CPCV → kabul/red. **Her denemeyi
`--trials N`'e say**; DSR bunu hesaba katıyor ve her deneme bir sonrakinin
barını yükseltiyor.

## Durum

- [x] Faz 0 — iskelet, config, modeller, DB
- [x] Faz 1 — veri (21 sembol × 3 TF × 3 yıl + türev, 63/63 seri temiz)
- [x] Faz 2 — indikatörler, IC/tail/redundans ölçümü
- [x] Faz 3 — kurulumlar, conviction motoru, backtest motoru
- [x] Faz 4 — CPCV + DSR + PBO + Monte Carlo → **Kapı 1: GEÇİLMEDİ**
- [x] Faz 5 — MAE/MFE çıkış kalibrasyonu
- [x] Faz 6 — risk bütçesi, paper/ccxt broker, runner, watchdog, launchd
- [x] Faz 7 — panel + Telegram
- [x] Faz 8 — LLM rolleri + redaksiyon + etki ölçümü
- [ ] Faz 9 — Bybit demo, 4 hafta (Kapı 1 geçilmeden anlamsız)
- [ ] Faz 10 — canlı $100 (Kapı 3'e bağlı)

### İkinci motor ("fast") — kurulu, başlatılmadı

Ana motor (`config/demo.yaml`) hiç değişmeden çalışmaya devam ediyor. Yanında
`config/fast.yaml`: linear perpetual, iki yönlü, $300 sert tavan, 1x kaldıraç.

- [x] Borsa gerçekleri ölçüldü (`scripts/perp_probe.py`)
- [x] İzolasyon: isim alanı, `runtime-fast/`, ayrı launchd etiketleri, panel sekmesi
- [x] `PerpBroker` + kaldıraç kapısı + borsa tarafı hedef
- [x] Short tarafı: rejim izni, 3 aynalanmış kurulum, portföy marj muhasebesi, backtest kolu
- [x] Veri: 30 perp sembol × 3 TF × 2 yıl, 90/90 temiz, türevler dahil
- [x] H17 ön-kaydı + kill kriterleri (`research/hypotheses.md`)
- [ ] **Başlatma — operatör onayı bekliyor** (`./ops/bot.sh start fast`)

**Beklenen sonuç ön-kayıtlı: negatif, işlem başına ≈ −%0.05.** İleri testin
amacı bunu doğrulamak ya da çürütmek; taze veri DSR bütçesinden belgelenen tek
çıkış yolu.
