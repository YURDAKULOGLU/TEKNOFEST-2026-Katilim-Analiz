# Demo arıza ve geri dönüş akışı

Amaç, arızayı başarı gibi sunmadan aynı V1 davranışını gösterebilmektir. Geri
dönüş yolu yeni bir ürün iddiası eklemez.

## Çekimi durduran durumlar

Aşağıdakilerden biri görülürse kayıt veya canlı anlatım kesilir:

- `/health/ready` yanıtı `ok` değildir.
- Kapsam yanıtı 10 banka, kampanya yanıtı 4 kayıt döndürmez.
- Coverage API'deki doğrulanmış `campaign_count` toplamı `0` değildir.
- Dört demo kaydından biri `needs_review` dışında görünür.
- UI, pending kayıtları doğrulanmış diye sunar veya aday içerik toplamını
  doğrulanmış kayıt sayısı olarak etiketler.
- Preview sonucu `human_verified=true` veya `persisted=true` döner.
- Preview adayı ilk satır başlığını aynı alıntıya bağlamadan kabul eder.
- Karşılaştırma pending kayıtları sıralar.
- Sohbet pending kayıttan kaynaklı yanıt üretir.
- Terminalde beklenen commit, model adı/digest'i veya küme bağlamı doğrulanamaz.
- Senaryoda olmayan login, bildirim veya kurum entegrasyonu ekranı kullanılır.

Bu durumlar seslendirmeyle açıklanıp geçilmez; ürün veya senaryo düzeltilir ve
prova baştan alınır.

## Arıza matrisi

| Belirti | Önce kontrol et | İzin verilen geri dönüş | Yasak anlatı |
|---|---|---|---|
| Tarayıcı açılmıyor | `health/live`, `health/ready`, API pod logu | Aynı commit üzerinde API çıktısını göster; UI düzelmeden final video alma. | “Arayüz aslında çalışıyor.” |
| UI boş | `/api/v1/coverage`, `/api/v1/campaigns?limit=100`, `demo-seed` Job logu | Seed Job başarıyla yeniden çalıştırıldıktan sonra sayfayı yenile. | Elle kayıt eklemek veya sayı uydurmak. |
| Model soğuk | `ollama ps`, model adı ve digest | Model hazırsa `local-up.ps1 -SkipModelPull -SkipBuild`; değilse ısınmayı bekle. | İlk token süresini gizleyip performans metriği söylemek. |
| Preview 120 saniye sınırına ulaştı | Preview yanıtındaki `model_attempted`, `issues`, `candidate` | Kural çıktısı aday döndüyse timeout notuyla aynen göster; aday yoksa çekimser sonucu göster veya sorunu giderip yeniden çek. | Timeout'u model başarısı diye anlatmak veya sonucu elle tamamlamak. |
| Preview `Çekimser kalındı` döndürdü | İlk boş olmayan satır, başlık kanıtı, quarantine notları | Çekimser çıktıyı saklama; aynı girdiyi elle değiştirip başarılıymış gibi birleştirme. | Eski başarılı sonucu yeni istek sonucu gibi göstermek. |
| Preview sonucu kalıcı listede belirdi | `human_verified`, `persisted`, campaign listesi | Çekimi durdur ve veri sınırını incele. | Preview'ı onaylanmış kampanya diye tanıtmak. |
| Karşılaştırma HTTP hatası | API logu ve seçili iki kayıt | Hata giderilene kadar çekimi durdur. | HTTP hatasını güvenli ret diye anlatmak. |
| Sohbet beklenmeyen yanıt verdi | Yanıtın `insufficient_evidence` ve citations alanları | Yanıt kaynağı incelenene kadar çekimi durdur. | Kaynaksız metni model cevabı diye göstermek. |
| İnternet yok | Yerel sağlık ve seed | Resmî kaynak bağlantısını açmadan yerel kayıt/kanıt ekranını göster. | Güncel web taraması yapıldı demek. |
| Canlı demo kesildi | Yedek videonun commit ve SHA-256 kaydı | Aynı commit'ten, kesilmemiş ekran kaydı oynat. | Eski veya düzenlenmiş sonucu güncelmiş gibi sunmak. |

## Salt API geri dönüşü

Bu komutlar UI arızasını teşhis eder. Final videoda UI yerine kalıcı çözüm olarak
kullanılmaz; şartname kullanıcı arayüzü, dashboard ve chatbot gösterimi ister.

```powershell
$demoCoverage = Invoke-RestMethod `
  -Uri 'http://127.0.0.1:8080/api/v1/coverage'
$demoCampaigns = Invoke-RestMethod `
  -Uri 'http://127.0.0.1:8080/api/v1/campaigns?limit=100'

$demoPreviewText = @'
MTV Ödemelerinde Vade Farksız 3 Taksit
Kredi Kartı
01 Temmuz 2026 - 31 Temmuz 2026
'@
$demoPreviewBody = @{
  bank_id = 'vakif-katilim'
  text = $demoPreviewText
} | ConvertTo-Json
$demoPreview = Invoke-RestMethod `
  -Uri 'http://127.0.0.1:8080/api/v1/previews/extractions' `
  -Method Post `
  -ContentType 'application/json' `
  -Body $demoPreviewBody

$demoIds = @($demoCampaigns.items | Select-Object -First 2 -ExpandProperty id)
$demoComparison = @{
  campaign_ids = $demoIds
  dimensions = @('rate', 'term')
} | ConvertTo-Json
Invoke-RestMethod `
  -Uri 'http://127.0.0.1:8080/api/v1/comparisons' `
  -Method Post `
  -ContentType 'application/json' `
  -Body $demoComparison

$demoChat = @{ question = 'Kart kampanyalarını listele' } | ConvertTo-Json
Invoke-RestMethod `
  -Uri 'http://127.0.0.1:8080/api/v1/chat' `
  -Method Post `
  -ContentType 'application/json' `
  -Body $demoChat
```

Beklenen teşhis sonucu:

- `$demoCoverage.Count` değeri 10'dur.
- coverage `campaign_count` toplamı 0'dır.
- `$demoCampaigns.items.Count` değeri 4'tür ve durumların tamamı
  `needs_review` olur.
- `$demoPreview.scope` değeri `unverified_preview`, `human_verified` ve
  `persisted` değerleri `false` olur; çıktı kalıcı listeye eklenmez.
- karşılaştırma öğeleri sıralanmaz;
- sohbet yanıtında `insufficient_evidence=true` ve boş citation listesi olur.

Komut çalıştırılmadan bu sonuçlar kanıt sayılmaz.

## Jüri sorularında kısa yanıtlar

**“Neden dört banka eksik?”**

Eksik değiller; kapsamda erişim engelli durumuyla yer alıyorlar. Sistem o bankalar
için içerik uydurmuyor.

**“Neden karşılaştırma sıralamadı?”**

Demo kayıtları henüz insan doğrulamasından geçmedi. Karşılaştırma motoru
doğrulanmamış kayıtları sıralamıyor.

**“Chatbot neden cevap vermedi?”**

Yanıt kaynağı olabilecek `validated` kayıt yok. Bu nedenle kanıtsız cevap yerine
geri çekildi.

**“Metin girişi neden kaydedilmedi?”**

Bu ekran yalnız inceleme önizlemesidir. Sonuç insan doğrulaması olmadan kalıcı
kayda, karşılaştırmaya veya chatbot veri kaynağına dönüşmez.

**“Model katkısı neden sıfır olabilir?”**

Model yalnız çözülemeyen alanlar için aday üretir. Kanıta yeniden bağlanmayan
öneriler kabul edilmez; sıfır kabul güvenli ve geçerli bir sonuçtur.

**“Fine-tuning yaptınız mı?”**

Hayır. İnsan doğrulamasından geçmiş yeterli altın veri bulunmadan fine-tuning
başarı iddiası kurulmadı.

**“Login, bildirim ve kurum entegrasyonu nerede?”**

Bunlar V1 kapsamına dahil edilmedi; V1.1–V1.3 geliştirme sırasına ayrıldı.
