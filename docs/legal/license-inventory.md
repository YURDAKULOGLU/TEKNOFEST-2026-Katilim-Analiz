# Lisans envanteri ve SBOM üretimi

Bu belge çalışma zamanı bağımlılıkları, OCI imajı, yerel model ve veri sınırı için yeniden üretilebilir envanteri tanımlar. Sonuç bir hukuk görüşü değildir; `PASS`, yalnızca aşağıdaki fail-closed envanter politikasının sağlandığını gösterir. Telif bildirimi, kaynak sunma ve diğer lisans yükümlülükleri ayrıca yerine getirilmelidir.

## Mevcut sonuç

| Kapsam | Sabit girdi | Bileşen | Çözümlenmemiş | Yasak işaret | Sonuç |
| --- | --- | ---: | ---: | ---: | --- |
| Backend | `backend/uv.lock` | 32 | 0 | 0 | PASS |
| Frontend | `frontend/pnpm-lock.yaml` | 47 | 0 | 0 | PASS |
| Model | `qwen3.5:4b` / tam SHA-256 | 1 | 0 | 0 | PASS |
| OCI geliştirme snapshot'ı | imaj SHA-256 | 139 | 0 | 0 | PASS |
| Veri | provenance ve dağıtım sınırı | 4 kapsam | — | — | PASS_WITH_RIGHTS_BOUNDARY |

Makinece okunabilir gerçek kaynak [`artifacts/licenses/runtime-license-inventory.json`](../../artifacts/licenses/runtime-license-inventory.json), bütünlük özeti ise [`artifacts/licenses/SHA256SUMS`](../../artifacts/licenses/SHA256SUMS) dosyasıdır.

> **Final imaj uyarısı:** Mevcut OCI SBOM'u, canlı Kind doğrulamasından geçen V1 snapshot'ı `ghcr.io/yurdakuloglu/katilim-analiz@sha256:13db851181a21d12f8431d16f62127c2c67d645ed7094cc65c131783c6e3c9cd` için üretildi. Bu, V1.1–V1.3 geliştirmeleri tamamlanmadan alınmış ara snapshot'tır; final yarışma/release imajı değildir. Final imaj derlendikten sonra aşağıdaki komut o imaj etiketiyle yeniden çalıştırılmalı ve yeni immutable digest release kanıtına bağlanmalıdır.

## Politika ve resmî kaynaklar

- SBOM biçimi [CycloneDX 1.7](https://cyclonedx.org/specification/overview/) JSON'dur. Package URL değerleri [resmî purl specification](https://github.com/package-url/purl-spec) biçimindedir. Graph `bom-ref` alanları, encoded PURL'lerden bağımsız deterministik URN'lerdir; gerçek PURL her bileşenin `purl` alanında korunur.
- Geçerli lisans kimlikleri [SPDX License List 3.28.0](https://spdx.org/licenses/)'dan sabitlenmiştir. Python metadata'sı [PyPI JSON API](https://docs.pypi.org/api/json/) üzerinden exact sürümle alınır. PEP 639 ifadesi bulunmayan exact sürümler için incelenmiş resmî kaynaklar [`python-license-overrides.json`](../../artifacts/licenses/python-license-overrides.json) içinde kayıtlıdır.
- Frontend ağacı frozen pnpm lock ve [`pnpm list`](https://pnpm.io/cli/list) çıktısından çıkarılır; lisans değeri kurulu exact yayımlanmış paketin `package.json` dosyasından okunur.
- OCI keşfi [Syft](https://github.com/anchore/syft) `v1.48.0` ile yalnızca `dpkg-db-cataloger` ve `python-installed-package-cataloger` kullanılarak yapılır. Kullanılan sürüm ve imaj digest'i envantere yazılır.
- Debian `main` arşivinin dağıtılabilirlik ve kaynak koşulları [Debian Policy — The main archive area](https://www.debian.org/doc/debian-policy/ch-archive.html#the-main-archive-area) ile [Debian Social Contract / DFSG](https://www.debian.org/social_contract) temelinde değerlendirilir. Kurulu paketlerin ayrıntılı telif metinleri imaj içindeki `/usr/share/doc/PACKAGE/copyright` dosyalarında kalır.
- Modelin adı, digest'i ve lisansı [Ollama `qwen3.5:4b`](https://ollama.com/library/qwen3.5:4b) ile [Qwen3.5-4B model card](https://huggingface.co/Qwen/Qwen3.5-4B) üzerinden doğrulanır: `Apache-2.0`, Q4_K_M, manifest SHA-256 `2a654d98e6fba55d452b7043684e9b57a947e393bbffa62485a7aac05ee4eefd`.

Fail-closed politika, runtime bileşeninde lisans beyanı yoksa veya değer `UNLICENSED`, `PROPRIETARY`, `NOASSERTION` ya da `NONE` ise üretimi bloklar. Debian taramasında SPDX kimliğine çevrilemeyen 76 ad/hash beyanı kaybolmaz: envanterde `non_spdx_named_declarations` altında aynen raporlanır. Bunlar “lisanssız” sayılmamıştır; aynı zamanda yükümlülüklerin kalktığı ya da tüm lisansların permissive olduğu anlamına gelmez.

Veri lisansı kod lisansından ayrıdır. Takımın ürettiği şema, test ve annotation'lar `Apache-2.0` kapsamındadır. BDDK/banka kaynaklı olgular ve kısa kanıt parçaları yeniden lisanslanmaz; kaynak URL'si ve provenance korunur. Tam HTML ile temizlenmiş sayfa metni private runtime verisidir ve dağıtım artifact'ına alınmaz. Ayrıntı: [`data-boundary.md`](data-boundary.md).

## Üretim

Syft'in resmî `v1.48.0` Windows AMD64 ZIP dosyası [release sayfasından](https://github.com/anchore/syft/releases/tag/v1.48.0) alınmalıdır. ZIP SHA-256 değeri yayınlanan checksum ile doğrulanmalıdır:

```text
b46cb02a47c5b76a1656958757d62ac07d0cb7de35f92e8a7e02d450cbb53097
```

Repo kökünde üretim komutu:

```powershell
$syft = Join-Path ([System.IO.Path]::GetTempPath()) 'katilim-syft-v1.48.0\syft.exe'
python scripts/licenses/generate_sboms.py `
  --image ghcr.io/yurdakuloglu/katilim-analiz:dev `
  --syft-executable $syft `
  --require-image
```

Final build için `--image` değeri final yerel tag ile değiştirilir. Script Docker inspect ile immutable digest'i çözer; imaj veya pinned Syft yoksa, runtime lisansı eksikse ya da yasak işaret bulunursa başarılı sonuç üretmez. PyPI/SPDX metadata'sını resmî uçlardan bilinçli olarak yenilemek için ayrıca `--refresh-metadata` kullanılabilir.

Üretilen dosyalar:

- `artifacts/sbom/backend.cdx.json`
- `artifacts/sbom/frontend.cdx.json`
- `artifacts/sbom/model.cdx.json`
- `artifacts/sbom/oci.cdx.json`
- `artifacts/licenses/runtime-license-inventory.json`
- `artifacts/licenses/{pypi,npm}-runtime-metadata.json`
- `artifacts/licenses/spdx-license-ids-3.28.0.json`
- `artifacts/licenses/SHA256SUMS`

## Doğrulama

```powershell
python -m unittest discover -s scripts/licenses/tests -v
python -m py_compile scripts/licenses/generate_sboms.py

Get-ChildItem artifacts/sbom/*.json | Sort-Object Name | ForEach-Object {
  pnpm dlx --package=@cyclonedx/cdxgen@12.7.1 cdx-validate `
    --input $_.FullName --strict --no-include-manual `
    --min-severity critical --fail-severity critical
  if ($LASTEXITCODE -ne 0) { throw "SBOM doğrulanamadı: $($_.Name)" }
}
```

Mevcut dört dosyada hem `schemaValid=true` hem `deepValid=true` sonucu alınmıştır. `cdx-validate` tarafından ayrıca gösterilen SCVS/CRA benchmark skorları şema ve graph bütünlüğünden farklı, daha geniş bir kanıt olgunluğu puan kartıdır; bu belge o skorları mevzuat uyum beyanı olarak kullanmaz.

Aynı kilitler, aynı model digest'i, aynı OCI digest'i ve aynı metadata cache'iyle generator iki kez çalıştırılmış; dört SBOM ile altı lisans artifact'ının tamamında SHA-256 değerleri byte-byte aynı kalmıştır (`10/10`, değişen `0`).
