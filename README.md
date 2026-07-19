# Katılım Analiz

**BilisimVadisi2026**

Katılım Analiz, Türkiye'deki katılım bankalarının resmî kampanya ve ürün
sayfalarını kanıt bağlı yapılandırılmış verilere dönüştüren; benzer ürünleri
açıklanabilir biçimde karşılaştıran ve Türkçe dashboard/chat arayüzü sunan
tamamen yerel bir analiz ürünüdür. Finansal tavsiye veya katılım ilkelerine
uygunluk kararı üretmez; yalnızca kaynağın ne söylediğini, ne zaman gözlendiğini
ve hangi dönüşümle yapılandırıldığını gösterir.

> Proje aktif geliştirme aşamasındadır. Kod kapıları yeşildir; insan doğrulamalı
> gold veri ve bazı yarışma kabul kanıtları henüz tamamlanmamıştır. Aşağıdaki
> durum bölümü bilinçli olarak eksikleri de gösterir.

## İçindekiler

- [Öne çıkan özellikler](#öne-çıkan-özellikler)
- [Mevcut durum](#mevcut-durum)
- [Teknoloji yığını](#teknoloji-yığını)
- [Önkoşullar](#önkoşullar)
- [Yerel Kubernetes kurulumu](#yerel-kubernetes-kurulumu)
- [Geliştirici kurulumu ve kalite kapıları](#geliştirici-kurulumu-ve-kalite-kapıları)
- [Mimari](#mimari)
- [Model ve çıkarım politikası](#model-ve-çıkarım-politikası)
- [Veri ve lisans sınırı](#veri-ve-lisans-sınırı)
- [Yapılandırma](#yapılandırma)
- [Operatör komutları](#operatör-komutları)
- [Değerlendirme](#değerlendirme)
- [Dağıtım profilleri](#dağıtım-profilleri)
- [Sorun giderme](#sorun-giderme)
- [Katkı ve lisans](#katkı-ve-lisans)

## Öne çıkan özellikler

- Güncel BDDK listesindeki her katılım bankası için başarı, engel veya erişim
  durumunu açıkça kaydeden allowlist tabanlı toplama.
- Ham HTML hash'i, temiz blok, DOM locator, kısa alıntı, alan pointer'ı ve
  extractor sürümüne kadar izlenebilir evidence/provenance zinciri.
- Türkçe para, oran, vade ve tarih normalizasyonu ile katılım finansına özgü
  oran ve ürün semantiği.
- Karşılaştırılamayan ürün/bazları reddeden typed karşılaştırma motoru.
- PostgreSQL üzerinde sürümlü kayıtlar, tam metin arama, dayanıklı işler,
  transactional outbox ve kayıpsız bildirim cursor'ı.
- FastAPI/OpenAPI API, React dashboard, filtreleme, karşılaştırma, extraction
  preview, kaynak alıntıları ve uygulama içi bildirim akışı.
- Kurallar önce; yalnız çözülemeyen alanlarda yetkisiz ve şema-kısıtlı yerel
  model adayı. Model geliştirici fallback'ında kapatılabilir; yerel yarışma
  profili modeli açık çalıştırır.
- Aynı immutable uygulama imajından ayrı API ve worker process rolleri.
- Kind üzerinde PostgreSQL ve CPU-only Ollama dâhil tamamen yerel kurulum;
  kurum ortamı için ayrı Kustomize overlay'i.

## Mevcut durum

19 Temmuz 2026 yerel doğrulamasının özeti:

| Alan | Durum |
|---|---|
| Backend testleri | 469 geçti |
| Frontend testleri | 16 geçti |
| Ruff, format, mypy, ESLint, TypeScript | Temiz |
| OpenAPI snapshot | Güncel |
| Kubernetes manifestleri | Base/local/offline/institution render oluyor |
| PostgreSQL migration head | `f6a91c2d8e47` |
| Yerel Kubernetes runtime | API, worker, PostgreSQL ve CPU Ollama hazır |
| Kontrollü 4B pozitif smoke | 25,598 sn; 1/1 kanıtlı `12 ay vade` fact'i kabul edildi |
| Kontrollü model-skip smoke | 0,088 sn; `3 taksit` doğru biçimde modele gönderilmedi |
| Banka coverage | 10/10 açık durum; 6 başarılı kaynak, 4 erişim engeli |
| Gold veri | 4 öneri, 0 insan doğrulamalı |
| Extraction/classification skoru | `insufficient_data`; geçer sayılmaz |
| Prompt-injection profili | Güncel ürün hash'iyle 20/20 geçti; yetki kaçışı/desteksiz iddia 0 |
| Fine-tune kararı | Yetkilendirilmedi |

Makine önerileri gold sayılmaz. Bir örnek ancak iki farklı insan reviewer aynı
kaynak ve semantik bağı onayladığında değerlendirmeye girer. Güncel kabul
otoritesi [`control/pointers.json`](control/pointers.json), ayrıntılı sonuç ise
[`evals/results/baseline-2026-07-19.md`](evals/results/baseline-2026-07-19.md)
dosyasıdır.

## Teknoloji yığını

- Python 3.12.10, FastAPI 0.138+, Pydantic 2, SQLAlchemy 2, asyncpg, Alembic
- PostgreSQL 17
- React 19, TypeScript 5.9, Vite 8, TanStack Query, Recharts
- `uv` ve kilitli `uv.lock`; pnpm 10 ve kilitli `pnpm-lock.yaml`
- Docker/OCI, Kubernetes 1.34, Kind 0.32, Kustomize
- Ollama 0.32 ve yerel yarışma profilinde `qwen3.5:4b` CPU çıkarımı
- Pytest, Ruff, mypy, Vitest, Testing Library, ESLint

Kafka, RabbitMQ, Redis, Celery, LangChain, LlamaIndex, vector database veya
genel amaçlı autonomous-agent framework kullanılmaz. Bu bileşenler ancak
ölçülmüş bir kabul testi mevcut PostgreSQL/typed-plan yaklaşımının yetersizliğini
kanıtlarsa değerlendirilir.

## Önkoşullar

Birincil geliştirme ortamı Windows + PowerShell 7'dir.

- Git
- PowerShell 7
- Python 3.12.10
- [`uv`](https://docs.astral.sh/uv/) 0.9.x
- Node.js 22.12–22.x ve Corepack/pnpm 10.28.2
- Docker Engine veya Docker Desktop
- Kind 0.32.0
- kubectl 1.34.x

`scripts/local-up.ps1` Kind/Kubernetes/kubectl sürümlerini fail-closed kontrol
eder. Docker için en az 4 CPU, 12 GiB kullanılabilir RAM ve model dâhil yeterli
disk alanı önerilir. 16 GiB laptop kabul ölçümü henüz tamamlanmış bir performans
iddiası değildir.

## Yerel Kubernetes kurulumu

Repo henüz public GitHub'a yayımlanmadı. Yayınlandıktan sonra bu bölümdeki clone
adresi gerçek public URL ile değiştirilecektir; release öncesi placeholder
bırakılmayacaktır.

Repo kökünde PowerShell 7 ile:

```powershell
pwsh -File scripts/local-up.ps1
```

Bu komut:

1. `katilim-analiz` Kind kümesini oluşturur veya doğrular;
2. PostgreSQL ve Ollama PVC/deployment'larını hazırlar;
3. sabit model kimliğini indirip digest ile doğrular ve CPU RAM'de sıcak tutar;
4. frontend/backend içeren tek OCI imajını oluşturur ve Kind'a yükler;
5. Alembic migration ve idempotent demo seed Job'larını çalıştırır;
6. API/worker rollout, readiness, coverage ve demo kayıtlarını doğrular.

Başarılı kurulum: [http://127.0.0.1:8080](http://127.0.0.1:8080)

Model zaten PVC'de varsa indirmeden doğrulamak için:

```powershell
pwsh -File scripts/local-up.ps1 -SkipModelPull
```

Uygulama imajı Kind node'unda zaten varsa geliştirme döngüsünde ayrıca
`-SkipBuild` kullanılabilir. Bu bayraklar doğrulama ve rollout'u atlamaz.

> Kurulum CPU-only'dir; yarışma profili GPU kullanmaz. Model indirme veya model
> çalıştırma istemiyorsanız bu scripti çağırmayın; geliştirici kapıları model
> olmadan çalışır ve `MODEL_PROFILE=rules_only` kullanılabilir.

Ayrıntılar: [`docs/deployment/local-kubernetes.md`](docs/deployment/local-kubernetes.md)

## Geliştirici kurulumu ve kalite kapıları

Backend bağımlılıkları:

```powershell
Set-Location backend
uv sync --locked --extra dev
Set-Location ..
```

Frontend bağımlılıkları:

```powershell
corepack enable
pnpm --dir web install --frozen-lockfile
```

Ortam örneği:

```powershell
Copy-Item .env.example .env
```

`.env` içindeki parolaları ve kurum adreslerini gerçek ortamda değiştirin;
dosyayı Git'e eklemeyin.

Hızlı geliştirici kontrolleri:

```powershell
uv run --project backend pytest -q
uv run --project backend ruff check backend/src backend/tests
uv run --project backend ruff format --check backend/src backend/tests
uv run --project backend mypy backend/src
pnpm --dir web contract:check
pnpm --dir web lint
pnpm --dir web typecheck
pnpm --dir web test
pnpm --dir web build
uv run --project backend python tools/check_pointers.py
```

Fail-closed release kapısı:

```powershell
python scripts/verify_release.py --profile full
```

Bu komut yürütülebilir testleri geçse bile insan/hardware evidence pointer'ları
eksikse başarı kodu vermez. `--profile quick` yalnız ön kontroldür ve release
geçti anlamına gelmez.

## Mimari

```text
Browser
  -> Kubernetes Service
     -> API Deployment: FastAPI + React static build
        -> PostgreSQL
        -> private Ollama adapter (optional)
     -> Worker Deployment: same application image
        -> PostgreSQL durable jobs/outbox
        -> allowlisted official sources (only when enabled)
```

Kaynak-veri akışı:

```text
official URL
  -> immutable fetch artifact + raw hash
  -> cleaned blocks/tables/locators
  -> deterministic candidates
  -> optional narrow local-model candidates
  -> exact quote alignment + semantic validation
  -> versioned fact + review issues
  -> typed query/comparison
  -> dashboard/chat answer + metadata-owned citations
```

Kod modüler monolittir. API ve worker farklı Kubernetes process rolleri olarak
ölçeklenebilir, fakat aynı imaj ve domain sözleşmelerini kullanır. Mikroservis
bölünmesi yalnız bağımsız ölçek/yönetim ihtiyacı ölçülürse yapılır.

Önemli dizinler:

```text
backend/src/katilim_analiz/  domain, ingestion, extraction, storage, app, API
backend/migrations/         PostgreSQL Alembic revision'ları
web/src/                    React dashboard ve API istemcisi
data/registry/              sürümlü banka/kampanya kaynak allowlist'i
data/private/               Git dışı ham üçüncü taraf içerik
datasets/                   public türetilmiş, gold ve eval veri setleri
evals/                      fail-closed değerlendirme harness'ı
deploy/k8s/                 base, component ve ortam overlay'leri
control/pointers.json       gereksinim/ADR/EVAL/WP kontrol düzlemi
docs/                       mimari, değerlendirme, dağıtım ve demo kanıtları
```

Derin mimari açıklama: [`docs/architecture/system.md`](docs/architecture/system.md)

Bağımlılık ve stop-loss kararları:
[`docs/architecture/dependency-strategy.md`](docs/architecture/dependency-strategy.md)

## Model ve çıkarım politikası

- `rules_only`: model çağrısı yok; deterministik geliştirici/fail-closed fallback
  profili. Yarışma başarı profili değildir.
- `laptop`: yerel Kubernetes yarışma profili; kurallar önce, yalnız çözülemeyen
  ve kaynak sinyali bulunan alanlar CPU-local modele gider.
- `workstation`: aynı 4B kalite sözleşmesi CPU RAM'de sıcak tutulur.
- GPU mevcut yarışma release'inin dışında tutulur.
- Model URL, SQL, filesystem veya tool yetkisi almaz.
- Model-authored URL/offset kabul edilmez; alıntı kaynakta deterministik hizalanır.
- Model çıktısı kuralı sessizce ezemez; çatışma review issue olur.
- Timeout veya şema/evidence hatasında sistem veri uydurmaz ve abstain eder.

19 Temmuz 2026 kontrollü canlı smoke'unda sıcak `qwen3.5:4b`, tam API/Kubernetes
yolunda `12 ay vade` alıntısını 25,598 saniyede doğru kanıt/offset ile çıkardı.
Aynı anda `vade farksız 3 taksit` örneği 0,088 saniyede modele gönderilmeden
doğru biçimde taksit kampanyası olarak kaldı. Bu iki örnek model yolunun
çalıştığını ve staged seçicinin gereksiz çağrıyı kestiğini kanıtlar; frozen set
başarımı veya p95 iddiası değildir. Daha büyük model veya fine-tune öncesinde
insan-doğrulanmış frozen split zorunludur.

## Veri ve lisans sınırı

- Ham resmî banka HTML'leri `data/private/` altında tutulur, Git'e girmez.
- Public repoda URL, zaman, hash, kısa evidence excerpt ve ekipçe üretilmiş
  yapılandırılmış türevler bulunabilir.
- CAPTCHA/authentication bypass yapılmaz; engel açık coverage sonucu olur.
- Banka metinleri, marka/adlar ve model weights kendi hak/lisanslarını korur.
- Apache-2.0 yalnız ekipçe yazılan kod, şema, annotation ve dokümana uygulanır.

Ayrıntı: [`datasets/PROVENANCE.md`](datasets/PROVENANCE.md) ve
[`docs/legal/data-boundary.md`](docs/legal/data-boundary.md).

## Yapılandırma

Temel değişkenler `.env.example` içinde belgelenir.

| Değişken | Amaç | Güvenli varsayılan |
|---|---|---|
| `DATABASE_URL` | API/worker PostgreSQL bağlantısı | Zorunlu ortam değeri |
| `MIGRATION_DATABASE_URL` | Ayrı DDL-capable migration rolü | Zorunlu ortam değeri |
| `MODEL_PROFILE` | `rules_only`, `laptop`, `workstation` | `rules_only` |
| `OLLAMA_BASE_URL` | On-prem model servisi | `127.0.0.1:11434` |
| `MODEL_TIMEOUT_SECONDS` | Uçtan uca model deadline | `120` |
| `MODEL_KEEP_ALIVE` | Modeli CPU RAM'de tutma | `-1` |
| `INGEST_NETWORK_ENABLED` | Resmî kaynak toplamayı opt-in açar | `false` |
| `INGEST_PER_HOST_DELAY_SECONDS` | Host başına minimum gecikme | `3` |
| `PRIVATE_RAW_DIR` | Git dışı ham artifact alanı | `data/private/raw` |
| `LOG_FORMAT` | `json` veya geliştirme formatı | `json` |

Secret'lar ConfigMap'e veya repoya yazılmaz. Institution overlay örnek Secret'ı
yalnız şablondur; secret manager tarafından sağlanmalıdır.

## Operatör komutları

Backend ortamı kurulduktan sonra:

```powershell
uv run --project backend katilim-analiz registry-sync
uv run --project backend katilim-analiz demo-seed
uv run --project backend katilim-analiz worker --once
uv run --project backend katilim-analiz enqueue-source BANK_ID HTTPS_URL
```

`scan` komutu Kubernetes Job kimliğiyle çalışır; rastgele yerel `SCAN_RUN_ID`
uydurulmamalıdır. Toplama varsayılan olarak kapalı ve registry allowlist'iyle
sınırlıdır.

Sağlık/tanı:

```powershell
kubectl --context kind-katilim-analiz -n katilim-analiz get pods,pvc,jobs
kubectl --context kind-katilim-analiz -n katilim-analiz logs deployment/api
kubectl --context kind-katilim-analiz -n katilim-analiz logs deployment/worker
Invoke-RestMethod http://127.0.0.1:8080/health/live
Invoke-RestMethod http://127.0.0.1:8080/health/ready
```

## Değerlendirme

Model/GPU olmadan mevcut baseline'ı yeniden üretmek için:

```powershell
$env:PYTHONPATH = "backend/src"
uv run --project backend python -m evals.security_eval
uv run --project backend python -m evals run `
  --security-predictions evals/results/security-rules-only-v1.0.jsonl `
  --allow-incomplete
```

`--allow-incomplete`, yetersiz veriyi başarılı yapmaz; yalnız rapor üretimine
izin verir. CI/release kapısında bu bayrak kaldırılır.

İlk iki-reviewer gold seti hazırlandıktan sonra extraction/classification F1,
evidence coverage ve unsupported-claim ölçümleri aynı frozen split'te alınır.
Fine-tuning kararı ancak bu hata taksonomisinden sonra verilir.

Ayrıntı: [`evals/USAGE.md`](evals/USAGE.md) ve
[`docs/evaluation/acceptance-plan.md`](docs/evaluation/acceptance-plan.md).

## Dağıtım profilleri

- `base`: tekrar kullanılabilir manifest; model için fail-closed `rules_only`.
- `local`: tek-node Kind + PostgreSQL + CPU Ollama; `laptop` profilini açıkça açar.
- `offline`: hazırlanmış image/model/corpus ile public egress ve scan kapalı.
- `institution`: managed PostgreSQL ve onaylı kurum-içi model endpoint'i için
  örnek network/config sınırı; kurum erişimi varmış gibi davranmaz.

AD, SMTP veya OpenShift erişimi bu repoda varsayılmaz. İlgili yetenekler
ileride aynı typed port'lara gerçek kurum adapter'ları bağlanarak eklenebilir;
yerel demo adapter'ı üretim entegrasyonu kanıtı sayılmaz.

## Sorun giderme

### Docker veya Kind bulunamıyor

`docker info`, `kind version` ve `kubectl version --client` çalışmalıdır.
Script sürüm uyuşmazlığında bilinçli olarak durur; rastgele daha yeni Kind node
imajıyla devam etmez.

### Readiness başarısız

```powershell
kubectl --context kind-katilim-analiz -n katilim-analiz describe pod -l app.kubernetes.io/component=api
kubectl --context kind-katilim-analiz -n katilim-analiz logs job/database-migrate
```

Liveness yalnız process'i, readiness PostgreSQL/model gibi zorunlu profil
bağımlılıklarını temsil eder.

### Model yavaş veya timeout oluyor

Bu laptop CPU profilinde beklenen bir olasılıktır. `rules_only` profiline dönün;
timeout'u körlemesine büyütmeyin. Model yoksa alan eksik/ambiguous kalmalıdır.

### Kaynak erişilemiyor

Coverage kaydındaki `error_code`, robots sonucu ve HTTP durumunu inceleyin.
CAPTCHA, auth veya host policy engeli bypass edilmez.

### Release verifier kod 2 döndürüyor

Yürütülebilir kapılar yeşil olsa bile blocking EVAL pointer'ları insan/hardware
kanıtı bekliyor olabilir. Ayrıntı için
[`docs/deployment/release-verification.md`](docs/deployment/release-verification.md)
dosyasına bakın.

## Katkı ve lisans

Değişiklikler küçük, pointer'a bağlı ve testli olmalıdır. Yeni runtime bağımlılığı
eklemeden önce lisans, offline kurulum, SBOM, kaynak bütçesi ve frozen-gold
regresyon kapıları belgelenmelidir. Ham banka içeriği, secret, parola veya model
weight'i commit etmeyin.

Ekipçe üretilen proje materyali [Apache License 2.0](LICENSE) ile lisanslanır.
Üçüncü taraf sınırları için [NOTICE](NOTICE) ve
[`docs/legal/license-inventory.md`](docs/legal/license-inventory.md) geçerlidir.
