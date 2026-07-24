# TEKNOFEST 2026 Yarı Final Teslimat Kontrol Listesi

Bu liste, şartnamedeki her teslimat kalemini depodaki somut karşılığına veya
sahibin tamamlaması gereken işe (OWNER-TODO) bağlar (issue #8).

| # | Şartname kalemi | Depodaki karşılık / durum |
|---|-----------------|---------------------------|
| 1 | Demo videosu | **OWNER-TODO** — canlı sistem üzerinde uçtan uca akışı (tarama → doğrulama → karşılaştırma → sohbet) gösteren video; linki README'ye ve teslim formuna eklenecek. |
| 2 | Sunum (yarı final sunumu) | **OWNER-TODO** — proje sunumu; `docs/` altında PDF olarak arşivlenmesi önerilir. |
| 3 | Halka açık veri seti linki | `dataset-export` CLI çıktısı (aşağıdaki komut); üretilen JSON dosyası bir **GitHub Release** varlığı olarak yayınlanır ve release linki teslim formunda "halka açık veri seti" olarak verilir. |
| 4 | Platform etiketi (repo konuları) | GitHub repo topics: `teknofest-2026`, `nlp`, `turkish`, `participation-banking`, `information-extraction`. |
| 5 | CI (sürekli entegrasyon) | `.github/workflows/ci.yml` — her PR'da ve `main`'e push'ta ruff + birim testleri (Python 3.12, `uv`). |
| 6 | LICENSE | Depo kökünde `LICENSE` (Apache-2.0) ve `NOTICE`. Ekip tercih ederse teslimden önce MIT'e çevrilebilir. |

## Halka açık veri seti üretimi

Veri seti, veritabanındaki en güncel **doğrulanmış** (validated) kayıtlardan
üretilir; her kayıt banka, başlık, ürün ailesi, kampanya tipi, birebir alıntılı
kanıtlar, resmi kaynak URL'si ve çıkarım kaynağı (provenance) taşır.

```bash
cd backend
uv run python -m katilim_analiz dataset-export \
  --output ../artifacts/katilim-analiz-public-dataset-v1.0.0.json \
  --dataset-version 1.0.0
```

- `--as-of 2026-08-01T00:00:00+03:00` ile anlık görüntü zamanı sabitlenebilir;
  verilmezse veritabanı saati kullanılır.
- Üretilen dosya elle düzenlenmez; şema `schema_version: "1.0"` ile
  sürümlenmiştir.

## Yayınlama adımları (GitHub Release)

1. `dataset-export` komutunu çalıştır, çıktıyı sürüm numarasıyla adlandır.
2. `gh release create dataset-v1.0.0 artifacts/katilim-analiz-public-dataset-v1.0.0.json \
   --title "Halka açık veri seti v1.0.0" --notes "Doğrulanmış kampanya kayıtları"`
3. Release linkini teslim formuna ve README'ye ekle.

## Teslim öncesi son kontroller

- [ ] `cd backend && uv run python -m pytest tests/unit -q` yeşil
  (bilinen tek istisna: issue #21'deki seed-sha testi).
- [ ] `uv run ruff check .` temiz.
- [ ] CI workflow'u son PR'da yeşil.
- [ ] Veri seti release linki erişilebilir (gizli repo ise release'in görünürlüğü doğrulanmalı).
- [ ] Demo video ve sunum linkleri teslim formunda (OWNER-TODO).
- [ ] Repo topics ayarlı.
