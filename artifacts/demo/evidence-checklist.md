# Demo kanıt kontrol listesi

Canlı çekim durumu: **NOT_EXECUTED**

Kod/test inceleme tarihi: **2026-07-19**

`RESOLVED_CODE_TEST`, davranışın mevcut kodda ve testte bulunduğunu belirtir;
canlı Kubernetes koşumu, ekran görüntüsü veya video kaydı anlamına gelmez.

## Kayıt kimliği

| Alan | Kanıt |
|---|---|
| Commit SHA |  |
| Çalışma ağacı durumu |  |
| Demo seed sürümü ve SHA-256 |  |
| Kayıt tarihi/saat dilimi |  |
| Bir dakikalık video SHA-256 |  |
| Beş dakikalık video SHA-256 |  |

## Canlı ürün kanıtı

| Kimlik | Beklenen gerçek | Kanıt yöntemi | Sonuç | Referans |
|---|---|---|---|---|
| D-001 | Doğru Kind bağlamında tek düğümlü küme | `kubectl get nodes -o wide` | NOT_EXECUTED |  |
| D-002 | API, worker, PostgreSQL ve Ollama hazır | `kubectl get pods` | NOT_EXECUTED |  |
| D-003 | Uygulama readiness durumu `ok` | `/health/ready` ham yanıtı | NOT_EXECUTED |  |
| D-004 | `qwen3.5:4b` digest'i `2a654d98e6fba55d452b7043684e9b57a947e393bbffa62485a7aac05ee4eefd` | Ollama `/api/tags` ve ConfigMap karşılaştırması | NOT_EXECUTED |  |
| D-005 | Model bellekte tutuluyor | `ollama ps` | NOT_EXECUTED |  |
| D-006 | Coverage 10 benzersiz banka | `/api/v1/coverage` kaydı | NOT_EXECUTED |  |
| D-007 | Kapsam dağılımı 6 success, 4 blocked; doğrulanmış toplam 0 | Coverage yanıtından sayım | NOT_EXECUTED |  |
| D-008 | Demo veritabanı 4 pending kayıt içeriyor | `/api/v1/campaigns?limit=100` | NOT_EXECUTED |  |
| D-009 | Kart filtresi 3 kayıt gösteriyor | UI zaman damgası + API filtre yanıtı | NOT_EXECUTED |  |
| D-010 | Preview yalnız coverage banka kimliği ve metin gönderiyor | Ağ kaydı + request gövdesi | NOT_EXECUTED |  |
| D-011 | Preview `unverified_preview`, `human_verified=false`, `persisted=false` | UI zaman damgası + response | NOT_EXECUTED |  |
| D-012 | Preview adayı alan kanıtı ve girdi SHA-256 gösteriyor | UI zaman damgası + response | NOT_EXECUTED |  |
| D-013 | Preview kampanya sayısını değiştirmiyor | Preview öncesi/sonrası campaign response | NOT_EXECUTED |  |
| D-014 | Vakıf MTV kalıcı kaydı alan kanıtlarını gösteriyor | UI zaman damgası + detail response | NOT_EXECUTED |  |
| D-015 | Pending kayıtlar karşılaştırmada sıralanmıyor | UI zaman damgası + comparison response | NOT_EXECUTED |  |
| D-016 | Sohbet pending veya preview verisinden yanıt üretmiyor | `insufficient_evidence=true`, citations boş | NOT_EXECUTED |  |
| D-017 | Bir dakikalık video 60 saniyeyi geçmiyor | Media metadata | NOT_EXECUTED |  |
| D-018 | Beş dakikalık video 5 dakikayı geçmiyor | Media metadata | NOT_EXECUTED |  |

## Resmî beklenti eşlemesi

| Teknik şartname beklentisi | Mevcut çekim yolu | Hazırlık durumu |
|---|---|---|
| Kullanıcı arayüzü ve dashboard | Genel görünüm, preview, filtre, ayrıntı | RESOLVED_CODE_TEST |
| Kampanya metni girdisi | Coverage banka seçimi + 20.000 karakterle sınırlı metin alanı | RESOLVED_CODE_TEST |
| Yapılandırılmış çıkarım çıktısı | Non-persistent aday, alan kanıtı, model katkısı, girdi SHA-256 | RESOLVED_CODE_TEST |
| Chatbot | Validated kanıt olmadığı için güvenli geri çekilme | RESOLVED_CODE_TEST |
| Farklı bankaları karşılaştırma | Pending kayıtlar güvenle reddediliyor; başarılı kıyas için validated çift yok | OPEN_G-003 |
| En fazla 5 dakikalık video | Senaryo hazır; video henüz yok | NOT_EXECUTED |
| Sunum için 1 dakikalık video | Senaryo hazır; video henüz yok | NOT_EXECUTED |

## Ürün gerilimlerinin durumu

| Kimlik | Durum | Kapanan/açık konu | Kod kanıtı | Test kanıtı |
|---|---|---|---|---|
| G-001 | RESOLVED_CODE_TEST | Pending liste artık “Kanıt bağlı aday kayıtlar” diyor. | `web/src/app/App.tsx` | `web/tests/app.test.tsx` — `shows campaigns, coverage freshness, and field evidence without fabricated bank branding` |
| G-002 | RESOLVED_CODE_TEST | Coverage `campaign_count` yalnız insan-doğrulanmış güncel kayıt; demo public değeri 0. Kaynak adayı sayısı `source_candidate_count` olarak ayrı. | `backend/src/katilim_analiz/contracts/models.py`; `backend/src/katilim_analiz/demo/seed.py`; `web/src/components/CoverageSummary.tsx` | `backend/tests/integration/demo/test_demo_seed_integration.py` — `{item.campaign_count} == {0}`; `web/tests/app.test.tsx` coverage etiketi assertion'ı |
| G-003 | OPEN | En az iki insan-doğrulanmış, aynı bazda kayıt yok; başarılı kıyas sonucu üretilemez. | Karşılaştırma `record_not_validated` ile fail-closed. | Mevcut karşılaştırma testleri pending kaydı sıralamadığını doğruluyor; olumlu validated çift kanıtı yok. |
| G-004 | RESOLVED_CODE_TEST | `POST /api/v1/previews/extractions` ve UI metin alanı yapılandırılmış, kanıta bağlı, kalıcı olmayan preview üretiyor. | `backend/src/katilim_analiz/application/preview.py`; `backend/src/katilim_analiz/api/app.py`; `web/src/components/ExtractionPreviewPanel.tsx` | `backend/tests/unit/application/test_preview.py`; `backend/tests/integration/api/test_extraction_preview.py`; `web/tests/app.test.tsx` — `runs a review-only text extraction preview from the coverage bank list`; `web/tests/api-client.test.ts` preview contract testi |

G-004 güvenlik sınırı:

- request yalnız `bank_id` ve `text` alır; ek alanlar reddedilir;
- metin en çok 20.000 karakter, ilk boş olmayan satır en çok 500 karakterdir;
- banka aktif BDDK registry'sinde değilse istek reddedilir;
- cevap sözleşmesi `human_verified=false` ve `persisted=false` değerlerini `const`
  olarak sabitler;
- ilk satır başlığı aynı alıntıya bağlanamazsa aday gösterilmez;
- prompt-injection bloğu karantinaya alınır;
- preview storage, collection, URL ve query yetkisi taşımaz.

## Hedefli test koşumu

| Komut | Sonuç |
|---|---|
| `uv run pytest -q tests/unit/application/test_preview.py tests/integration/api/test_extraction_preview.py tests/integration/demo/test_demo_seed_integration.py` (`backend/`) | PASS — 9 test, 3.20 s |
| `pnpm test -- tests/app.test.tsx tests/api-client.test.ts` (`web/`) | PASS — 2 dosya, 11 test, 2.63 s |
| `uv run --project backend python tools/export_openapi.py --check` | PASS — `OpenAPI snapshot is current` |
| `pnpm contract:check` (`web/`) | PASS — generated TypeScript contract current |

Koşum tarihi: 2026-07-19. Bunlar hedefli kod/test kapılarıdır; `D-*` canlı ürün
kanıtlarının yerine geçmez.

## Son onay

- [ ] Bütün `D-*` maddeleri gerçek referansla `PASS`.
- [ ] G-003 için insan-doğrulanmış uyumlu çift ve olumlu karşılaştırma E2E kanıtı var.
- [ ] Video seslendirmesi ekrandaki durumla birebir uyumlu.
- [ ] Preview bekleme kesintisi açıkça belirtilmiş; istek ve sonuç aynı koşuma ait.
- [ ] Ölçülmemiş performans veya doğruluk iddiası yok.
- [ ] Ham HTML, tam üçüncü taraf sayfa veya özel veri yeniden dağıtılmıyor.
- [ ] Login, bildirim ve kurum entegrasyonu V1 özelliği diye gösterilmiyor.
- [ ] İki video aynı commit'e bağlı ve SHA-256 değerleri kayıtlı.

Onaylayan:

Tarih/saat:
