# Yerel Kubernetes kurulumu

Bu profil tek bilgisayarda çalışan yarışma/demonstrasyon kurulumudur. API,
worker, PostgreSQL ve Ollama aynı Kind kümesinde; veri ve model ayrı PVC'lerde
çalışır. Uygulama dış bir LLM veya ücretli bulut API'si çağırmaz.

## Sabitlenen araç ve çalışma sürümleri

- Docker Engine/Desktop: çalışan kapıda 29.5.3
- kind: 0.32.0
- Kubernetes node: 1.34.8,
  `kindest/node` release digest'i ile sabit
- kubectl: 1.34.1
- Kustomize: 5.7.1 (`kubectl kustomize`)
- PostgreSQL: 17 Bookworm, image digest'i ile sabit
- Ollama: 0.32.0, image digest'i ile sabit
- Model: `qwen3.5:4b`; ilk indirme için yaklaşık 3.4 GB disk alanı

Kind 0.32.0'ın varsayılan node'u 1.36.1 olduğu için varsayılana güvenilmez.
Kesin node image'i `deploy/kind/cluster.yaml` içindedir. Bu kararın resmî
kaynakları `docs/references/implementation-sources.md` içinde izlenir.

## İlk kurulum

PowerShell 7 oturumunda repo kökünden:

```powershell
pwsh -File scripts/local-up.ps1
```

Komut sırasıyla:

1. `katilim-analiz` Kind kümesini yoksa oluşturur;
2. PostgreSQL ve Ollama altyapısını uygular;
3. modeli indirir ve `keep_alive=-1` ile ısıtır;
4. tek OCI uygulama imajını derleyip Kind'a yükler;
5. Alembic migration Job'ını tamamlar;
6. sürümlü demo snapshot'ını ayrı, idempotent Job ile yükler;
7. API ve worker rollout'larını bekler;
8. readiness, 10-bankalık coverage, dört `needs_review` kayıt ve `ollama ps`
   durumunu denetler.

Başarılı koşum sonunda ürün [http://127.0.0.1:8080](http://127.0.0.1:8080)
adresindedir. `local-up.ps1` var olan PVC'leri veya kümeyi silmez.

Model ve imaj zaten hazırsa geliştirme döngüsü kısaltılabilir:

```powershell
pwsh -File scripts/local-up.ps1 -SkipModelPull
```

`-SkipModelPull` model doğrulamasını atlamaz. Script warmup ve uygulama
rollout'undan önce Ollama `/api/tags` yanıtında base ConfigMap'teki model adını
tam ve büyük/küçük harfe duyarlı olarak bir kez arar; dönen lowercase 64-hex
digest'in `OLLAMA_MODEL_DIGEST` ile birebir eşleşmesini zorunlu tutar. Yalnızca
aynı etiketin veya başka bir modelin bulunması yeterli değildir.

`-SkipBuild` yalnız aynı `:dev` uygulama imajının Kind node içinde zaten mevcut
olduğu biliniyorsa kullanılmalıdır. Bu bayrak rollout'u atlamaz; node'daki
etikete karşılık gelen imajın gerçekten çalışan podlara alınması için API ve
worker yine yeniden başlatılır.

Demo seed ham banka sayfalarını dağıtmaz. Dört kısa, alan-kanıtı bağlı örneği
`needs_review` olarak ekler; `human_verified_count` daima sıfırdır. On bankalık
collector snapshot'ındaki sekiz aday, public `campaign_count` alanında
doğrulanmış kayıt gibi sayılmaz. Kurum overlay'i demo Job'ını otomatik çalıştırmaz.

## Sağlık ve tanı

```powershell
kubectl --context kind-katilim-analiz -n katilim-analiz get pods,pvc,jobs
kubectl --context kind-katilim-analiz -n katilim-analiz logs deployment/api
kubectl --context kind-katilim-analiz -n katilim-analiz logs deployment/worker
Invoke-RestMethod http://127.0.0.1:8080/health/live
Invoke-RestMethod http://127.0.0.1:8080/health/ready
```

`live` yalnız prosesin yanıt verebildiğini, `ready` ise zorunlu bağımlılıkların
istek kabul etmeye hazır olduğunu gösterir. Liveness başarısızlığını geçici veri
tabanı kesintisiyle bir tutmamak için iki uç ayrı tutulur.

## Model profilleri

- `rules_only`: model çağrısı yoktur; en düşük kaynak tüketimli ve tamamen
  deterministik profil.
- `laptop`: kurallar önce çalışır; yalnız çözülemeyen alanlar 4B modele gider.
- `workstation`: aynı sözleşme ve 4B kalite profili; model bellekte tutulur.

Model çağrısında 120 saniye toplam wall-clock deadline vardır. HTTPX'in tek ağ
işlemi timeout'u bu garanti için yeterli olmadığından bütün stream ayrıca
`asyncio.timeout` ile sınırlandırılır. Deadline dolarsa veri uydurulmaz; alan
eksik/belirsiz kalır.

## Veri sınırı

- Ham üçüncü taraf HTML yalnız `data/private/` PVC/yerel alanına yazılır ve Git
  tarafından yok sayılır.
- Repoya yalnız kaynak hash'i, kanıt bağı, normalize edilmiş türetilmiş kayıt ve
  kapsam durumu girer.
- Secret ve gerçek parola manifestte tutulmaz. Local overlay'deki parola yalnız
  izole geliştirme kümesi içindir; kurum profiline taşınamaz.

## Ağ güvenliği hakkında dürüst sınır

Kubernetes `NetworkPolicy` nesneleri manifestte mevcuttur; fiilî enforcement
CNI eklentisinin sorumluluğudur. Release kapısı hem manifesti hem gerçek negatif
egress testini raporlar. Yalnız YAML'ın API sunucusu tarafından kabul edilmesi,
trafiğin engellendiği iddiası için yeterli sayılmaz.

2026-07-18 Kind doğrulamasında API podundan `1.1.1.1:443` bağlantısı 4 saniyede
timeout olurken PostgreSQL ve Ollama iç bağlantıları başarılı oldu. Tekrarlanabilir
kanıt ve ortam sürümleri `docs/evaluation/network-policy-2026-07-18.md` içindedir.

## Temizleme

Küme ve PVC'ler veri silen bir işlemdir; otomatik kurulum bunu yapmaz. Artık
gerekmiyorsa hedefi açıkça doğruladıktan sonra kullanıcı kendisi çalıştırır:

```powershell
kind delete cluster --name katilim-analiz
```

Bu işlem PostgreSQL ve model PVC'leri dâhil Kind kümesini geri alınamaz biçimde
siler.
