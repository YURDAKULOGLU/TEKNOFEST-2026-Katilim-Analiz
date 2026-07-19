# V1 demo çekim listesi

Durum: **PLAN — kayıt alınmadı**

Her çekim aynı commit ve demo seed ile alınır. Bu dosya ekran görüntüsü içermez;
çekim sırasında görülmesi gereken gerçek durumu tarif eder.

| Kimlik | Kadraj | İşlem | Beklenen görünür durum | 1 dk | 5 dk |
|---|---|---|---|:---:|:---:|
| S01 | PowerShell | `kubectl ... get pods` | `api`, `worker`, `postgres`, `ollama` hazır | ✓ | ✓ |
| S02 | PowerShell | `/health/ready`, `/api/tags`, `ollama ps` | Sağlık `ok`; `qwen3.5:4b` tam release digest'i; model bellekte |  | ✓ |
| S03 | Tarayıcı üst bölüm | Ana sayfa ve `Yerel çalışma` | Ürün başlığı; dış model servisi iddiası yok |  | ✓ |
| S04 | Genel görünüm | Kapsam kartları | 10 banka, 6/10 başarılı tarama, 0 doğrulanmış kampanya | ✓ | ✓ |
| S05 | Kapsam ayrıntısı | 4 kapsam notunu aç | Adil, Hayat, Kuveyt Türk, Türkiye Finans `Erişim engellendi` |  | ✓ |
| S06 | Çıkarım önizlemesi | Vakıf Katılım'ı seç; üç kısa kanıt satırını yapıştır; gönder | `Yerel çıkarım sürüyor…`; 120 saniye uyarısı görünür | ✓ | ✓ |
| S07 | Preview sonucu | Yalnız beklemeyi kes; sonucu ve kanıt listesini göster | `Doğrulanmamış çıkarım önizlemesi`, insan incelemesi/çekimserlik, model katkısı, SHA-256 | ✓ | ✓ |
| S08 | Kampanya tablosu | Dört satırı göster; `Kategori → Kart` seç | `Kanıt bağlı aday kayıtlar`; tümü `İnceleme gerekli`; filtrede 3 satır | ✓ | ✓ |
| S09 | Kampanya ayrıntısı | Vakıf MTV kaydını aç, kanıtı göster, paneli kapat | Alan işaretçisi, alıntı, kanıt durumu, blok kimliği |  | ✓ |
| S10 | Karşılaştırma seçimi | Vakıf ve Ziraat'ı seç | 2 pending kampanya seçildi | ✓ | ✓ |
| S11 | Karşılaştırma sonucu | `Vade` ile karşılaştır | `Karşılaştırma yapılamadı`, kayıtlar sıralanmadı | ✓ | ✓ |
| S12 | Kaynaklı asistan | `Kart kampanyalarını listele` | `Güvenli geri çekilme`, yeterli validated kanıt yok | ✓ | ✓ |
| S13 | Genel görünüm | Sabit kapanış | `Yerel çalışma`; iki saniye sabit | ✓ | ✓ |

## Preview girdisi

```text
MTV Ödemelerinde Vade Farksız 3 Taksit
Kredi Kartı
01 Temmuz 2026 - 31 Temmuz 2026
```

Bu satırlar demo datasetindeki kısa kanıt alıntılarıdır. Ham HTML ve tam sayfa
çekime veya repoya eklenmez.

## Çekim sırası

Bir dakikalık kesim:
`S01 → S04 → S06 → S07 → S08 → S10 → S11 → S12 → S13`.

Beş dakikalık kesim:
`S01 → S02 → S03 → S04 → S05 → S06 → S07 → S08 → S09 → S10 → S11 → S12 → S13`.

## Kurgu sınırı

- Preview ve diğer yükleme beklemeleri kesilebilir; istek, çıktı, sayı, rozet veya
  kanıt değiştirilemez.
- Preview kesintisinde `bekleme aralığı kesildi` açıklaması görünür.
- Terminal ve tarayıcı çekimleri aynı commit'ten değilse birleştirilemez.
- Eski ekran görüntüsü, mock API, geliştirici aracıyla değiştirilmiş DOM veya elle
  yazılmış terminal sonucu kullanılamaz.
- `Model katkısı` kabul sayısı önceden hazırlanmaz; gerçek koşumda görülen değer
  kullanılır.
- Kaynak sayfası üçüncü taraf içerik taşıdığı için videoda açılmaz; kayıt içindeki
  kanıt/provenance gösterilir.
- Başarılı karşılaştırma, login, bildirim veya kurum entegrasyonu için temsili
  çekim eklenmez.

## Kayıt sonrası dosya bilgisi

| Alan | 1 dakika | 5 dakika |
|---|---|---|
| Dosya adı |  |  |
| Süre |  |  |
| Boyut |  |  |
| SHA-256 |  |  |
| Commit SHA |  |  |
| Çekim tarihi |  |  |
