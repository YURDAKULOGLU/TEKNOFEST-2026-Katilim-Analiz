# Çevrimdışı Kind paketi

Bu akış `EVAL-010` ve `EVAL-014` için, bağlı bir hazırlama makinesindeki
image/model artefaktlarını internet erişimi olmayan tek düğümlü Kind kurulumuna
taşır. Import betiği hiçbir registry veya Ollama model-pull çağrısı yapmaz.

## Dayanılan resmî davranışlar

- Kind'ın [çevrimdışı çalışma rehberi](https://kind.sigs.k8s.io/docs/user/working-offline/)
  digest ile sabitlenmiş node image'inin `docker save` ile taşınıp `docker load`
  ile yüklenmesini tarif eder.
- Kind [v0.32.0 release kaydı](https://github.com/kubernetes-sigs/kind/releases/tag/v0.32.0)
  Kubernetes 1.34.8 için projede kullanılan
  `sha256:02722c2dedddcfc00febf5d27fbeb9b7b2c14294c82109ff4a85d89ac9ba3256`
  node digest'ini yayımlar ve release eşleşmesi için digest kullanımını şart
  koşar.
- Kind'ın [image yükleme rehberi](https://kind.sigs.k8s.io/docs/user/quick-start/#loading-an-image-into-your-cluster)
  workload arşivleri için `kind load image-archive` komutunu ve
  `IfNotPresent`/`latest` olmayan tag gereğini belirtir.
- Docker'ın resmî [`image save`](https://docs.docker.com/reference/cli/docker/image/save/)
  ve [`image load`](https://docs.docker.com/reference/cli/docker/image/load/)
  komutları image/tag katmanlarını tar arşivine yazar ve geri yükler.
- Ollama'nın [Modelfile referansı](https://docs.ollama.com/modelfile),
  `ollama show --modelfile` çıktısını ve GGUF için göreli `FROM
  ./model.gguf` kullanımını belgeler. [Model import rehberi](https://docs.ollama.com/import)
  GGUF artefaktının `ollama create` ile kurulmasını tarif eder.
- `kubectl cp` [resmî referansında](https://kubernetes.io/docs/reference/kubectl/generated/kubectl_cp/)
  belirtildiği gibi hedef container'da `tar` gerektirir. Sabitlenen Ollama
  0.32.0 image'i export/import önkoşulunda bu ikili için kontrol edilen
  çalışma ortamıdır.
- Microsoft'un resmî [`Get-FileHash` referansı](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.utility/get-filehash?view=powershell-7.5)
  SHA-256'nın dosya içeriği değişikliklerini doğrulamak için kullanılmasını ve
  `-LiteralPath` değerinin wildcard olarak yorumlanmamasını belgeler. Betikler
  hem manifest sidecar'ını hem her payload/repo girdisini açıkça
  `-Algorithm SHA256 -LiteralPath` ile doğrular.
- Kubernetes 1.34'ün resmî [`kubectl apply`](https://v1-34.docs.kubernetes.io/docs/reference/kubectl/generated/kubectl_apply/),
  [`scale`](https://v1-34.docs.kubernetes.io/docs/reference/kubectl/generated/kubectl_scale/),
  [`wait`](https://v1-34.docs.kubernetes.io/docs/reference/kubectl/generated/kubectl_wait/)
  ve [`delete`](https://v1-34.docs.kubernetes.io/docs/reference/kubectl/generated/kubectl_delete/)
  sözleşmeleri fail-closed geçişte kullanılır. `apply` yalnız verilen kaynakları
  oluşturup güncellediği için, önceki bağlı overlay'den kalan public egress
  nesnesi `--ignore-not-found=true` ile ayrıca silinir; API ve worker sıfıra
  ölçeklendikten sonra pod silinmeleri beklenir.

Ham `/models` PVC düzeni kararlı bir dış sözleşme sayılmaz. Bu nedenle paket,
PVC'yi körlemesine arşivlemek yerine Ollama'nın ürettiği Modelfile'ı ve onun
işaret ettiği doğrulanmış GGUF blob'unu taşır. Import sonrası Ollama'nın tam
model manifest digest'i export edilen değerle karşılaştırılır.

## Paket kapsamı

Bir paket yalnız şu yedi dosyayı içerir:

```text
bundle-manifest.json
bundle-manifest.sha256.json
images/kind-node.tar
images/workloads.tar
model/model.gguf
model/Modelfile
model/source.json
```

`images/workloads.tar` uygulama, PostgreSQL, Ollama ve warmup için curl
image'lerini içerir. Kind'ın Kubernetes sistem image'leri sabitlenmiş node
image'inin içindedir. Manifest her payload'ın byte uzunluğunu ve SHA-256
özetini; kullanılan image referansı/image ID'sini; model, GGUF ve Ollama
manifest digest'lerini; ayrıca kurulumu üreten repo manifestlerinin
SHA-256'larını ve tek hedef platformu (`linux/amd64` veya `linux/arm64`)
kaydeder. Import, Docker Engine ve yüklenen her image'in platformunu bu değerle
eşleştirir.

Paket şu verileri özellikle içermez:

- parola, token, `.env` veya Kubernetes Secret;
- PostgreSQL verisi;
- banka sitelerinden alınmış ham HTML;
- `data/private` veya başka yerel kullanıcı verisi.

SHA-256 aktarım bozulmasını ve yanlış dosyayı bulur; yayıncı kimliğini
kanıtlayan bir dijital imza değildir. Dağıtım kanalı tehdit modeline göre
manifest özeti ayrıca imzalanmalıdır.

## Bağlı hazırlama makinesinde export

Önkoşullar:

1. PowerShell 7, Docker Engine, Kind 0.32.0 ve kubectl 1.34.x kurulu olmalı.
2. `ghcr.io/yurdakuloglu/katilim-analiz:dev` image'i mevcut checkout'tan
   derlenmiş ve yerel Docker image deposunda bulunmalı.
3. `katilim-analiz` kümesindeki Ollama PVC'sinde `qwen3.5:4b` bulunmalı.
4. Model digest'i `deploy/k8s/base/config-map.yaml` içindeki
   `OLLAMA_MODEL_DIGEST` ile aynı olmalı.

Önce yalnız metadata ve kaynak dosyalarını denetleyen, image/model kopyalamayan
plan çalıştırılır:

```powershell
pwsh -NoProfile -File scripts/export-offline.ps1 -MetadataOnly
```

Sonra var olmayan, üst dizini önceden oluşturulmuş açık bir hedef seçilir:

```powershell
pwsh -NoProfile -File scripts/export-offline.ps1 `
  -OutputDirectory C:\OfflineBundles\katilim-analiz-2026-07-18
```

Varsayılan export, digest ile sabitlenmiş dış image'leri registry'den yeniden
çeker; böylece yanlış yerel tag'e güvenmez. Hazırlama makinesi zaten çevrimdışı
ve exact image'ler cache'de ise açıkça `-UseCachedImages` verilebilir. Eksik
veya farklı bir image/modelde betik durur.

Export hedefi varsa üzerine yazılmaz. Bütün payload önce aynı üst dizindeki
benzersiz staging dizinine yazılır, checksum'lar tamamlanınca hedef adına
taşınır. Hata halinde yalnız doğrulanmış staging dizini temizlenir; küme, PVC
ve mevcut model silinmez.

## Hedef makinede doğrulama ve import

Kind'ın resmî çevrimdışı rehberi gibi bu akış da host araçlarının önceden
kurulmuş olduğunu varsayar. Docker/PowerShell/Kind/kubectl kurulum paketleri bu
bundle'a dahil değildir; işletim sistemi ve CPU mimarisine uygun bu araçlar
ayrıca hazırlanmalıdır. Hedefte aynı release checkout'u ve bundle dizini
bulunmalıdır.

İlk adım yalnız yerel dosyaları okur. Docker'a image yüklemez, küme veya PVC'ye
dokunmaz:

```powershell
pwsh -NoProfile -File scripts/import-offline.ps1 `
  -BundleDirectory C:\OfflineBundles\katilim-analiz-2026-07-18 `
  -ValidateOnly
```

Doğrulama şunlardan birinde fail-closed davranır: manifest sidecar uyuşmazlığı,
payload checksum/boyut uyuşmazlığı, beklenmeyen dosya veya symlink, path
traversal, GGUF magic/digest uyuşmazlığı, taşınabilir olmayan Modelfile,
digest-pinsiz runtime image'i ya da checkout manifesti farkı.

Gerçek EVAL-010 koşumunda host/Docker çıkış ağı kapatıldıktan sonra:

```powershell
pwsh -NoProfile -File scripts/import-offline.ps1 `
  -BundleDirectory C:\OfflineBundles\katilim-analiz-2026-07-18 `
  -RequireNoOutboundNetwork
```

Import sırasıyla:

1. checksum ve checkout eşleşmesini yeniden doğrular;
2. mevcut adlı küme varsa tek node ve exact node image eşleşmesini yazma
   yapmadan denetler; ardından default-deny sınırını uygular, bağlı profile ait
   public HTTPS iznini siler, ingestion ayarını kapatır ve API/worker'ı sıfıra
   ölçekleyerek durmalarını bekler;
3. Kind node ve workload arşivlerini `docker load` ile yükler;
4. kümeyi yalnız yoksa sabit config ile oluşturur;
5. workload arşivini `kind load image-archive` ile node'a yükler ve beklenen
   her image referansının containerd tarafından çözülebildiğini doğrular;
6. yalnız PostgreSQL/Ollama ve iç ağ izinlerini içeren `offline-infra`
   overlay'ini uygular; public HTTPS politikasının absent olduğunu yeniden
   doğrular;
7. GGUF/Modelfile'ı scoped geçici PVC dizinine kopyalayıp `ollama create`
   çalıştırır;
8. oluşan modelin tam digest'ini bundle ile eşleştirir ve modeli warmup Job'ı
   ile `keep_alive=-1` profilinde yükler;
9. `offline-predeploy` overlay'ini uygular; API/worker replica sayılarının sıfır,
   `APP_ENV=offline-kubernetes`, `INGEST_NETWORK_ENABLED=false` ve public HTTPS
   politikasının absent olduğunu doğrulayıp Alembic migration Job'ının
   tamamlanmasını bekler;
10. migration sonrasında checksummed imaj içindeki sürümlü, kanıt bağlı demo
    snapshot'ını idempotent `demo-seed` Job'ı ile yükler;
11. ancak seed tamamlandıktan sonra tam `offline` overlay'ini uygulayarak
    API/worker'ı birer replica ile başlatır ve rollout'ları bekler;
12. readiness, 10 coverage kaydı, dört `needs_review` kampanya, model durumu ve
    istenmişse worker'dan `1.1.1.1:443` negatif bağlantı testini çalıştırır.

Mevcut kümenin node image'i farklıysa veya aynı isimli model farklı digest ile
PVC'de bulunuyorsa import bunların üzerine yazmaz. Bu koşumun yeni oluşturduğu
model beklenen digest'i vermiyorsa yalnız o yeni tag geri alınır; checksummed
GGUF bundle'da kaldığı için işlem tekrar edilebilir. Küme silme hiçbir koşulda
otomatik değildir. Mevcut runtime fail-closed hazırlama moduna alındıktan sonra
sonraki bir adım hata verirse API/worker bilerek durmuş, ingestion kapalı ve
public HTTPS izni silinmiş halde kalır; betik eski bağlı çalışma modunu otomatik
olarak geri açmaz. Tam runtime apply'i kısmen veya tamamen başladıktan sonraki
rollout/readiness/ağ/model doğrulama hatalarında da betik iki rolü yeniden
sıfıra ölçekleyip pod silinmesini bekler. Bu hata-toparlama adımı ayrıca
başarısız olursa asıl hata korunur ve cleanup hatası açık bir warning olarak
yazılır; operatör çalışan pod kalmadığını elle doğrulamalıdır.

## Ağ kanıtının sınırı

`INGEST_NETWORK_ENABLED=false` uygulamanın bağlı tarama yapmasını engeller.
Import ayrıca bağlı profile ait public HTTPS NetworkPolicy nesnesini açıkça
siler; yalnız overlay çıktısında görünmemesine güvenmez. `offline-infra` ve
`offline` render'ları bu politikayı hiç içermez. NetworkPolicy'nin fiilen
uygulanması CNI sorumluluğudur; YAML tek başına ağ izolasyonu kanıtı değildir.
`-RequireNoOutboundNetwork`, worker'dan public bir IP'ye doğrudan TCP
bağlantısının başarısız olduğunu şart koşar. Bu negatif test beklenmedik şekilde
başarılı olursa import hata verir ve API/worker'ı tekrar sıfır replicaya alır.
Release kanıtında hedef host/Docker ağının da gerçekten kapalı olduğu ayrı
kaydedilmelidir.

## Otomatik küçük test

Çok GB'lık image/model artefaktı oluşturmadan metadata, checksum, beklenmeyen
dosya, manifest path traversal ve payload bozulması reddi şu testle doğrulanır:

```powershell
pwsh -NoProfile -File scripts/tests/test-offline-bundle.ps1
```

Bu smoke test gerçek `docker load`, boş PVC model importu veya ağsız uçtan uca
koşumun yerine geçmez. Release kapısında yeni/boş bir hedef kümede tam boyutlu
export → transfer → `-ValidateOnly` → ağ kapatma → import koşumu ayrıca
gerçekleştirilmeli ve süre/disk/RAM kanıtı kaydedilmelidir.

## Bilinen veri başlangıç durumu

Bundle bilerek PostgreSQL dump'ı veya ham HTML içermez. Migration sonunda şema
hazırdır; fakat yarışma örnek verisinin veritabanına yüklenmesi, sürümlü
türetilmiş dataset için ayrı ve doğrulanmış seed komutu gerektirir. Bu komut
release CLI'ında yoksa UI boş fakat sağlıklı açılır; `EVAL-014` örnek-veri
kapısı tamamlanmış sayılmaz.
