# İnsan doğrulamalı (human_verified) elle veri girişi

Dört banka kaynağı otomatik toplanamıyor (`kuveyt-turk`, `turkiye-finans`,
`hayat-finans`: CAPTCHA/erişim engeli; `adil-katilim`: kampanya sayfası yok) ve
bazı sayfalar makine doğrulaması için gereken alanları hiç içermiyor. Bu kayıtlar
hiçbir zaman makine tarafından `validated` olamaz. Bu akış, bir insanın resmî
kaynaktan doğruladığı kampanya olgularını kanıt-öncelikli biçimde sisteme
girmesini sağlar.

Kanıt kuralı insanlar için de geçerlidir: beyan edilen her olgu için insanın
kaynak sayfadan **birebir kopyaladığı** bir alıntı (`quote`) zorunludur.
Alıntısız olgu reddedilir. Kayıtlar `extraction.method = manual` ve
`status = validated` olarak, `human_verified` + `attested_by:...` +
`attested_on:...` defter kayıtlarıyla saklanır; makine doğrulama kapısından
(`validation_policy`) asla türetilmez ve makine-doğrulanmış sayılmaz.

## İş akışı: şablonu doldur → komutu çalıştır → panelde doğrula

### 1. Şablonu doldur

Örnek şablon: `datasets/human-verified/ornek-sablon.json`. `ORNEK-DOLDUR` ile
işaretli tüm yer tutucuları gerçek, kaynaktan kopyalanmış değerlerle değiştirin.
Dosyayı yeni bir sürümle (`intake_version`) kaydedin; aynı sürüm üzerinde içerik
değişikliği gözlem çakışması hatası üretir.

Kampanya başına alanlar:

- `bank_id`: BDDK kayıt defterindeki banka kimliği.
- `source_url`: Bankanın izinli alan adındaki resmî kampanya sayfası.
- `observed_at`: Sayfanın görüldüğü an (saat dilimi zorunlu).
- `attested_by` / `attested_on`: Doğrulayan kişinin adı ve tarih.
- `title` + `title_quote`: Başlık ve birebir aynı alıntı.
- `product_family` + `product_family_quote`: Ürün ailesi ve onu adlandıran
  kaynak ifadesi (ör. "konut finansmanı", "kredi kartı", "katılma hesabı").
- `campaign_type` + `campaign_type_quote`: Kampanya türü ve onu adlandıran
  ifade (ör. "kâr payı dağıtım oranı", "puan", "masrafsız").
- `rates` / `terms` / `financing_amounts` / `fees` / `rewards` / `validity`:
  Geçerli olan olgular; her biri `quote` ister. Sayısal değerler alıntıdan
  determinist olarak türetilir; alıntı ayrıştırılamazsa dosya reddedilir.

### 2. Komutu çalıştır

Backend ortamında (veritabanı ayarları `.env` üzerinden):

```bash
cd backend
uv run python -m katilim_analiz human-verified-ingest \
  --intake ../datasets/human-verified/kayitlar.json
```

Komut idempotenttir; aynı dosya ikinci kez çalıştırıldığında
`campaigns_created: 0` döner. Çıktı örneği:

```json
{"intake_version": "1.0.0", "registry_version": "2026-07-18.2",
 "campaign_count": 4, "campaigns_created": 4, "human_verified_count": 4,
 "machine_validated_count": 0, "status": "ingested"}
```

`machine_validated_count` her zaman 0'dır: bu yol makine doğrulaması üretmez.

### 3. Panelde doğrula

Kayıtlar, seed kayıtlarıyla aynı okuma API'lerinden görünür:

- Kampanya listesinde kayıt `validated` durumunda, `extraction.method`
  alanı `manual` olarak listelenir.
- Kayıt ayrıntısında her olgunun kanıt alıntısı ve `human_verified`,
  `attested_by:...`, `attested_on:...` defter satırları görünür.
- Kapsam (coverage) görünümünde ilgili bankanın `campaign_count` değeri artar
  ve gerekçe `human_verified_manual_intake` olur.

## Reddedilen durumlar

- Beyan edilmiş bir olgu için `quote` eksikse.
- `title_quote` başlıkla birebir aynı değilse.
- `product_family_quote` / `campaign_type_quote` beyan edilen sınıfı
  adlandırmıyorsa.
- Alıntı determinist ayrıştırıcıdan geçmiyorsa (oran/vade/tutar/geçerlilik).
- Bilinmeyen alan, bilinmeyen enum değeri veya 1 MiB üzeri dosya.
- `bank_id` kayıt defterinde yoksa veya `source_url` bankanın izinli alan
  adları dışındaysa (yazma adaptörü reddeder).
