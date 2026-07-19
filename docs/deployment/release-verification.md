# Release doğrulaması

`scripts/verify_release.py`, çalıştırılabilen kontroller ile insan/hardware
kanıtlarını aynı sonuca sıkıştırmaz. Kubernetes kümesine yazmaz; manifest
kontrolleri yalnız istemci tarafında `kubectl kustomize` render'ıdır.

## Profiller

Kurulu ortamın hızlı ön kontrolü:

```powershell
python scripts/verify_release.py --profile quick
```

`quick`, var olan `backend/.venv` ve `web/node_modules` ortamını kullanır.
Bağımlılık kurmaz; backend tam entegrasyon takımını çalıştırmaz. Atlanan her
full kapısı `NOT_EXECUTED (full profile is required)` olarak raporlanır. Bu
profil hiçbir zaman release geçti sonucu veya `0` çıkış kodu üretmez.

Tam yürütülebilir kapı:

```powershell
python scripts/verify_release.py --profile full
```

`full` aşağıdaki sırayı fail-closed yürütür:

1. pointer graph ve doğrulayıcının odaklı regresyonları;
2. `uv lock --check`, `uv sync --locked --extra dev`;
3. OpenAPI snapshot drift, Ruff lint/format, mypy ve backend tam pytest takımı;
4. `pnpm install --frozen-lockfile`, üretilmiş TypeScript drift, ESLint,
   Vitest, TypeScript typecheck ve production build;
5. repodaki her `deploy/k8s/**/kustomization.yaml` dizininin render'ı;
6. bütün PowerShell scriptlerinin parser kontrolü ve offline bundle smoke testi.

Bir yürütülebilir kapı başarısız olunca sonraki komutlar çalıştırılmaz ve
`NOT_EXECUTED (blocked by an earlier executable failure)` olarak gösterilir.
Bu, sonraki satırların yanlışlıkla başarılı görünmesini engeller.

## Çıkış kodları ve kanıt sınırı

| Kod | Anlam |
|---:|---|
| `0` | Full profilde bütün yürütülebilir kapılar ve blocking kanıt pointer'ları `passed`. |
| `1` | En az bir yürütülebilir kapı başarısız. |
| `2` | Quick profil kullanıldı, bir full kapısı çalıştırılmadı veya blocking insan/hardware kanıtı henüz `passed` değil. |

Kanıt durumu için ayrı dosya varmış gibi davranılmaz; otorite
`control/pointers.json` içindeki evaluation pointer'larıdır:

- `EVAL-010`: hazırlanmış image/model ile dış ağ kapalı gerçek tek-node koşumu;
- `EVAL-012`: bağımlılık, model ve veri lisans envanteri ile SBOM incelemesi;
- `EVAL-014`: temiz checkout kurulum, test, sample seed, build ve demo koşumu;
- `EVAL-011`: kontrollü 16 GB CPU laptop ölçümü; non-blocking olarak ayrıca
  raporlanır ve tek başına release'i durdurmaz.

Bir blocking evaluation yalnız pointer durumu açıkça `passed` olduğunda geçer.
Dosya varlığı, eski log veya scriptin mevcut olması kanıtın insan tarafından
kabul edildiği şeklinde yorumlanmaz.

## Araç davranışlarının kaynakları

Kullanılan bayrakların ve subprocess davranışının birincil kaynakları
`docs/references/implementation-sources.md` matrisindedir. Özellikle frozen
install, Kustomize'ın client-side render'ı ve PowerShell AST parser davranışı
ürün dokümanlarına bağlanır; shell zinciri kullanılmaz.
