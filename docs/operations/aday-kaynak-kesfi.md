# Aday Kaynak Keşfi (öneri listesi — otomatik kayıt YOK)

`discover-candidates` komutu, kayıt defterindeki (registry) bankaların **zaten
izin listesinde olan** alan adlarında yeni aday ürün/kampanya sayfaları arar ve
bunları insan onayı için bir öneri raporuna yazar. Araç hiçbir sayfayı kendi
başına izlemeye almaz: veritabanına yazmaz, iş kuyruğuna iş eklemez, registry
dosyasını değiştirmez. Tek çıktısı `datasets/discovery/` altındaki tarihli
JSON rapordur.

Dört banka ADR-005 ve human-verified intake kararı gereği tamamen atlanır:
`kuveyt-turk`, `turkiye-finans`, `hayat-finans` (CAPTCHA/erişim engeli; toplayıcı
bunları asla aşmaz) ve `adil-katilim` (kampanya/ürün sayfası yok).

## Nasıl çalışır

Her uygun banka için sırasıyla:

1. Bankanın registry'de kayıtlı kaynak URL'lerinin ait olduğu izinli hostlarda
   `/robots.txt` okunur; `Sitemap:` satırlarındaki sitemap'ler (yoksa
   `/sitemap.xml`) indirilir (banka başına en çok 6 sitemap isteği).
2. İzlenen index/ürün sayfalarının HTML'indeki **aynı-host** iç bağlantılar
   toplanır (robots.txt'nin izin vermediği sayfa atlanır).
3. Her aday URL, deterministik slug sezgileriyle sınıflandırılır
   (`konut`, `tasit`, `arac`, `ihtiyac`, `finansman`, `kampanya`, `kart`
   kökleri; Türkçe çoğul/iyelik ekleri tanınır).
4. Registry'de zaten bulunan URL'ler (index_url, evidence_url, static_pages;
   sondaki `/` farkı gözetilmeden) elenir.
5. Kalanlar puana göre sıralanır; banka başına en çok 30 öneri tutulur ve
   (dry-run değilse) her biri için robots.txt kontrolü sonrası tek bir HEAD
   isteğiyle HTTP durumu not edilir.

İstekler mevcut toplama nezaket kurallarını izler: `INGEST_USER_AGENT`,
host başına `INGEST_PER_HOST_DELAY_SECONDS` bekleme, `INGEST_MAX_BYTES` boyut
sınırı, yalnızca HTTPS ve yalnızca bankanın `allowed_hosts` listesi.

## Çalıştırma

Ağ erişimi bilinçli olarak kapalı geldiğinden önce açılmalıdır:

```bash
cd backend
INGEST_NETWORK_ENABLED=true uv run python -m katilim_analiz discover-candidates
```

Faydalı seçenekler:

- `--dry-run`: aday sayfalara HEAD isteği atmadan yalnızca sıralı listeyi yazar.
- `--max-candidates 10`: banka başına tutulacak (ve kontrol edilecek) öneri
  sayısını düşürür (1..30).
- `--output-dir datasets/discovery`: rapor dizini (varsayılan budur).
- `--registry` / `--campaign-registry`: farklı registry dosyalarıyla deneme.

Komut şimdilik elle çalıştırılır; ekip çıktıya güvenene kadar CronJob'a
bağlanmamıştır (takip işi).

## Raporu okuma

Rapor `datasets/discovery/source-candidates-YYYY-MM-DD.json` dosyasına yazılır.
Banka başına alanlar:

- `examined_urls`: sınıflandırmadan geçirilen aday URL sayısı.
- `suggestions[]`: sıralı öneriler; her biri `url`, `guessed_label`
  (konut/tasit/ihtiyac/kampanya/kart/finansman tahmini), `matched_tokens`
  (eşleşen kök kanıtı), `discovered_via` (`sitemap` veya `internal_link`),
  `score` ve `http_status` (dry-run'da `null`) taşır.
- `notes[]`: ulaşılamayan sitemap/sayfa ve robots.txt reddi gibi uyarılar.

`auto_enrollment: "never"` alanı sözleşmenin parçasıdır: rapordaki hiçbir satır
kendiliğinden izlemeye girmez.

## Bir öneriyi registry'ye terfi ettirme

Terfi her zaman insan kararıdır ve PR #62'deki akışı aynen izler:

1. Öneri URL'sini tarayıcıda doğrulayın: resmi sayfa mı, içerik ürün/kampanya mı,
   etiket (`konut`/`tasit`/`ihtiyac`) doğru mu?
2. `data/registry/monitored-campaign-sources-<eski-tarih>.json` dosyasını yeni
   gözlem tarihiyle kopyalayın
   (örn. `monitored-campaign-sources-2026-08-01.json`); `registry_version`
   (`YYYY-MM-DD.1`), `source_observed_on` ve her satırın `observed_on` alanını
   yeni tarihe çekin.
3. URL'yi ilgili bankanın `static_pages` listesine `{"url": ..., "label": ...}`
   olarak ekleyin (veya yeni bir index içinse `detail_links` satırı açın).
4. `backend/src/katilim_analiz/runtime/registry.py` içindeki
   `DEFAULT_CAMPAIGN_REGISTRY_PATH` sabitini yeni dosyaya çevirin ve
   `backend/tests/unit/runtime/test_scanning.py` ile
   `backend/tests/unit/candidates/test_heuristics.py` içindeki dosya yolu
   referanslarını güncelleyin.
5. `uv run python -m pytest tests/unit -q` ile doğrulayıp değişikliği PR olarak
   açın; sonraki `scan` çalışması yeni sayfayı izlemeye alır.
