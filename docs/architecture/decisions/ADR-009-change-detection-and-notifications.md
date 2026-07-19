# ADR-009: Gözlem kimlikli değişiklik tespiti ve uygulama içi bildirim

- Durum: Kabul edildi; V1.1'de uygulanacak
- Tarih: 2026-07-19
- İlgili kapsam: ENH-001, EVAL-015

## Bağlam

Takım, resmî banka sayfasında yeni veya değişmiş bir kampanya görüldüğünde
dashboard sonucunun yenilenmesini ve uygulama içinde bildirim gösterilmesini
istiyor. Mevcut tek-seferlik iş dedupe anahtarı aynı kaynağın sonraki günlerde
yeniden taranmasını engelliyor. Kampanya sürümü de extraction sırasında,
kalıcılık transaction'ı dışında ayrılıyor; iki worker aynı `version` değerini
üretebilir. Ayrıca `(campaign_key, record_sha256)` benzersizliği A→B→A
geri dönüşünü üçüncü bir gözlem olarak saklayamıyor.

Yeni kampanya keşfi ile bilinen bir detay sayfasının içerik değişikliği aynı
şey değildir. Birden fazla kampanyayı tek HTML'de gösteren koleksiyon sayfası,
ayrı kampanya kayıtlarına güvenilir biçimde bölünmeden “yeni kampanya” diye
sunulamaz.

## Karar

### Tarama kimliği ve Kubernetes zamanlayıcısı

- Periyodik tarama Kubernetes `CronJob` ile başlatılır; ayrı scheduler servisi
  eklenmez.
- `concurrencyPolicy: Forbid` aynı CronJob'ın normal çakışmasını azaltır fakat
  exactly-once garantisi sayılmaz. Kubernetes 1.34 dokümanı bazı koşullarda iki
  Job veya hiç Job oluşabileceğini ve işlerin idempotent olması gerektiğini
  açıkça belirtir [Kubernetes CronJob][k8s-cronjob].
- Pod'a Downward API ile gelen
  `metadata.labels['batch.kubernetes.io/controller-uid']` gerçek `scan_run_id`
  olur. Bu değer aynı Job'ın replacement podlarında sabit, iki ayrı Job'da
  farklıdır. `batch.kubernetes.io/job-name` yalnız gözlemlenebilir addır.
  Bu alanlar Kubernetes Job ve Downward API sözleşmelerinde yayımlanır
  [Kubernetes Job labels][k8s-job], [Downward API][k8s-downward].
- Tarama podu API sunucusunu okumaz; ServiceAccount token mount edilmez ve bu
  iş için RBAC eklenmez.

### Sürümlü izleme kaydı ve link keşfi

BDDK banka kayıt defterinden ayrı, sürümlü bir
`monitored-campaign-sources` kaydı tutulur. Her banka satırı yalnız doğrulanmış
resmî liste/index URL'sini, izinli detay path prefix'lerini, link üst sınırını
ve `verified` ya da açık `unavailable` durumunu içerir. URL tahmin edilmez.

`discover_campaign_index` işi mevcut robots, DNS/IP, redirect, boyut ve hız
sınırlarıyla index HTML'ini alır. Yalnız gerçek `<a href>` bağlantıları HTML
standardına göre çözülür [WHATWG links][whatwg-links]. Sonuçlar:

- index ile aynı izinli hostta olmalıdır;
- kayıtlı detay path prefix'lerinden birine uymalıdır;
- canonicalize ve dedupe edilmelidir;
- tek seviye ve banka başına kayıtlı `max_links` ile sınırlıdır;
- `monitored_campaign_targets` tablosuna `discovered_from`, `first_seen_at`,
  `last_seen_at` ve registry sürümüyle upsert edilir.

Index hatası eski hedefleri pasifleştirmez. Her tarama doğrulanmış indexleri ve
önceden bilinen aktif detay hedeflerini işler. Aynı tarama/hedef işi
`sha256(scan_run_id, campaign_key, canonical_url)` ile dedupe edilir; başka bir
tarama aynı URL'yi yeniden alabilir.

Tek HTML'de çok kampanya barındıran index/koleksiyon sayfası için güvenilir
segmentasyon yoksa yalnız `source_index_changed` ve insan incelemesi üretilir.
Bu sonuç kampanya kaydı, kampanya bildirimi veya EVAL-015 “yeni kampanya”
başarısı sayılmaz.

### Gözlem modeli ve A→B→A

`campaign_observations` her detay taramasını kampanya sürümünden ayırır:

- `observation_key = sha256(scan_run_id, campaign_key, canonical_url)`;
- `(campaign_key, observation_key)` benzersizdir;
- `scan_run_id`, `record_id`, `clean_sha256`, `record_sha256`, `observed_at`
  saklanır.

Kalıcılık transaction'ı şu sırayı izler:

1. `campaign_key` için `pg_advisory_xact_lock` alır;
2. aynı observation varsa önceki sonucu döndürür;
3. en son kampanya sürümünü okur;
4. son sürümün semantik hash'i gelenle aynıysa yeni sürüm/outbox yazmadan
   observation'ı mevcut record'a bağlar;
5. farklıysa `version=N+1`, gözlem kimliğini içeren yeni `record_id`, record,
   observation ve outbox olayını aynı transaction'da yazar.

Transaction advisory lock otomatik transaction sonunda bırakılır ve kısa
uygulama-kontrollü kritik bölgeye uygundur [PostgreSQL advisory locks][pg-lock].
Bu düzen A→A'yı idempotent tutar, A→B→A'yı v3 yapar ve eski A taramasının B
sonrasında retry edilmesini yanlış bir v3'e dönüştürmez.

Mevcut `(campaign_key, record_sha256)` ve `(campaign_key, payload_sha256)`
unique constraint'leri non-unique arama indexlerine çevrilir;
`(campaign_key, version)` benzersiz kalır. Migration eski kayıtları kendi
record kimliğiyle observation olarak backfill eder. A→B→A verisi oluştuktan
sonra eski unique constraint'lere kayıpsız downgrade mümkün değilse downgrade
ön kontrolü fail-closed olur; sürümleri sessizce birleştirmez.

### Extraction candidate kimliği

Candidate kimliği semantik veri, kanıt ve extractor/model sürümünden oluşur;
çalışma başlangıç/bitiş zamanı bu kimlikte değildir. Aynı A içeriğinin sonraki
gözleminde aynı candidate güvenle yeniden kullanılır. Repository conflict
kontrolü de candidate kimliğiyle aynı kanonik alanları kullanır; gözlem
timestamp'ını candidate içeriği gibi karşılaştırıp A→B→A'yı bozmaz. Zaman ve
tarama kökeni observation/record üzerinde kalır. Kimlik çakışması sessizce
yutulmaz.

### Transactional outbox ve bildirim semantiği

Yalnız yeni kampanya record sürümü aynı transaction'da bir outbox olayı
üretir. Dedupe anahtarı `campaign-change:<new_record_id>` olur. Olay en az:

- `campaign_key`, yeni `record_id` ve `record_version`;
- `change_kind` (`created` veya `updated`);
- gerçek record durumu (`validated`, `needs_review` veya `rejected`);
- varsa `previous_record_id` ve `observed_at`

taşır. İki eşzamanlı scan Job aynı değişikliği görse de tek record ve tek
outbox oluşur. Exactly-once teslim iddiası yoktur; V1.3 yayıncısı at-least-once
çalışır.

Bildirim feed sırası transaction başlangıç zamanı veya UUID ile kurulmaz.
Her outbox satırı değişmez, benzersiz bir `BIGINT feed_sequence` taşır. Değer,
`CACHE 1 NO CYCLE` ayrılmış PostgreSQL sequence'inden şu protokolle alınır:

1. kampanya ve diğer domain kilitleri/yazıları tamamlanır;
2. coverage dahil outbox dışındaki transaction yazıları tamamlanır;
3. sabit iki-`integer` namespace'inde global `pg_advisory_xact_lock` alınır;
4. outbox insert ve sequence allocation yapılır; bundan sonra yeni kilit alınmaz;
5. global kilit transaction commit/rollback ile bırakılır.

Bu nedenle sıra `N` commit olmadan başka bir transaction `N+1` alamaz. Rollback
sequence boşluğu bırakabilir; feed yalnız `feed_sequence > cursor` ve
`ORDER BY feed_sequence` kullandığı için boşluk güvenlidir. `created_at`,
`occurred_at`, UUID ve publisher `published_at` alanları yalnız metadata'dır;
API görünürlüğünü veya cursor sırasını belirlemez. Aynı transaction'daki birden
fazla olay ardışık değer alır ve committe atomik görünür.

Opaque notification cursor sürümü `v2` yalnız `feed_sequence` konumunu taşır.
Kanonik ve geçerli eski timestamp/UUID cursor'ı satıra çevrilmez; olası eski
atlamayı korumamak için sıfırdan güvenli replay başlatır. Public
`CampaignChangeEvent` sözleşmesine iç sıra alanı eklenmez.

V1.1 API'si bu olayları keyset cursor ile salt-okunur listeler. Tarayıcı kısa,
sabit aralıkla polling yapar; yeni cursor geldiğinde TanStack Query ile
campaign listesi geçersiz kılınır ve aktif sorgu yeniden alınır
[TanStack Query invalidation][tanstack-invalidation]. WebSocket, Kafka, Redis
ve service worker eklenmez.

Seçim UI'da record ID ile değil mantıksal `campaign_key` ile korunur. Her yeni
campaign listesinde güncel record ID yeniden çözülür; detail ve comparison
sorgu anahtarı yeni ID'lerle değiştiğinde sonuç otomatik yeniden hesaplanır.
Ara bir notification kaçırılsa bile N→N+1 zincir tahmini yapılmaz.

V1.1'de bildirim feed'i read-only'dir; görüldü bilgisi tarayıcı oturumunda
yereldir. PostgreSQL'de kullanıcıya bağlı okundu mutation'ı ancak ADR-010
oturumu uygulandıktan sonra V1.2'de eklenir.

## Reddedilen seçenekler

- Kafka/Redis/Celery/WebSocket: tek düğüm ve PostgreSQL varken ek işletim
  bağımlılığıdır.
- URL'leri her taramada yalnız registry sürümüyle dedupe etmek: sonraki
  değişiklikleri sonsuza dek kaçırır.
- Son record hash'ini global benzersiz yapmak: A→B→A geçmişini yok eder.
- Yalnız `previous_record_id` zinciriyle UI seçimi taşımak: arada kaçan olayda
  seçim bayatlar.
- Koleksiyon HTML'ini tek kampanya saymak: yanlış banka/kampanya iddiası üretir.

## Doğrulama şartları

EVAL-015 en az şunları kanıtlar:

- aynı tarama retry'si: bir observation, bir record, bir outbox;
- A→A: iki observation, bir record, bir outbox;
- A→B→A: üç observation, üç record sürümü, üç toplam create/change outbox;
- eski A işinin B sonrasında retry'si yeni record üretmez;
- aynı değişikliği gören iki eşzamanlı Job: tek yeni sürüm ve tek bildirim;
- candidate reuse gerçek PostgreSQL'de conflict üretmez;
- index linkleri host/path/adet sınırını aşamaz ve recursive crawl yapmaz;
- index hatası bilinen hedefleri silmez;
- çok-kampanyalı koleksiyon “campaign changed” diye etiketlenmez;
- polling cursor tekrarında duplicate göstermez;
- kampanya listesi yenilenince detail/comparison güncel record ID ile tekrar
  hesaplanır;
- on bankanın her biri verified index veya açık unavailable durumundadır;
  verified olmayan banka için yeni-kampanya keşif başarısı iddia edilmez.

[pg-lock]: https://www.postgresql.org/docs/17/explicit-locking.html#ADVISORY-LOCKS
[k8s-cronjob]: https://v1-34.docs.kubernetes.io/docs/concepts/workloads/controllers/cron-jobs/#job-creation
[k8s-job]: https://v1-34.docs.kubernetes.io/docs/concepts/workloads/controllers/job/#job-labels
[k8s-downward]: https://v1-34.docs.kubernetes.io/docs/concepts/workloads/pods/downward-api/#available-fields
[whatwg-links]: https://html.spec.whatwg.org/multipage/links.html
[tanstack-invalidation]: https://tanstack.com/query/v5/docs/framework/react/guides/query-invalidation
