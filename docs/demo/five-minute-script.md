# Beş dakikalık V1 demo senaryosu

Hedef süre: **4:40–5:00**. Metin girişi ve yapılandırılmış preview mevcut V1'de
çalışır. Demo seed'inde insan-doğrulanmış, aynı bazda iki kayıt bulunmadığı için
başarılı sıralama veya kaynaklı chatbot yanıtı gösterilmez; güvenli ret açıkça
anlatılır.

## 00:00–00:25 — Yerel çalışma sınırı

Ekran:

1. `kubectl --context kind-katilim-analiz -n katilim-analiz get pods`
2. `Invoke-RestMethod http://127.0.0.1:8080/health/ready`
3. `kubectl --context kind-katilim-analiz -n katilim-analiz exec deployment/ollama -- ollama ps`

Seslendirme:

> Ben Ahmet Yurdakul. Katılım Analiz V1 tek düğümlü yerel Kubernetes kümesinde
> çalışıyor. API ve worker stateless podlarda, kayıtlar PostgreSQL'de, Qwen 3.5
> 4B modeli Ollama içinde. Ücretli veya bulut tabanlı model API'si kullanılmıyor.
> Release kapısı model etiketine değil tam digest'e bakıyor.

Beklenen digest:
`2a654d98e6fba55d452b7043684e9b57a947e393bbffa62485a7aac05ee4eefd`.
Terminal bu değeri doğrulamıyorsa çekim kesilir.

## 00:25–00:55 — Veri akışı

Ekran: Ana sayfanın üst bölümü ve **Yerel çalışma** göstergesi.

Seslendirme:

> Akış sürümlenmiş BDDK banka kaydıyla başlıyor. Yalnız izin verilen resmî
> sayfalar alınıyor; içerik temizlenip kaynak bloklarına ayrılıyor. Önce
> deterministik Türkçe kurallar çalışıyor. Çözülemeyen alanlar yerel modele yalnız
> şema kısıtlı aday olarak sorulabiliyor. Model çıktısı kanıt sayılmıyor; alanın
> kaynak alıntısı ve karakter aralığı yeniden doğrulanıyor.

“Model karar verir” veya “model veritabanına yazar” denmez.

## 00:55–01:25 — On banka ve sıfır doğrulanmış kayıt

Ekran: **Genel görünüm**, ardından `4 banka için kapsam notunu görüntüle` alanı.

Seslendirme:

> Bu snapshot 18 Temmuz 2026'da gözlenen on katılım bankasını kapsıyor. Altı
> bankada kaynak taraması başarılı; Adil Katılım, Hayat Finans, Kuveyt Türk ve
> Türkiye Finans erişim engeliyle kaydedilmiş. Coverage kartındaki doğrulanmış
> kampanya sayısı sıfır. Kaynak dataset sekiz aday gözlem taşısa da bu sayı insan
> doğrulaması değildir ve public doğrulanmış kayıt metriğine eklenmez.

## 01:25–02:30 — Metinden yapılandırılmış preview

Ekran:

1. **Çıkarım önizlemesi** bölümünde Vakıf Katılım'ı seç.
2. Aşağıdaki üç satırı metin kutusuna yapıştır.
3. `Çıkarımı önizle` düğmesine bas.
4. `Yerel çıkarım sürüyor…` durumunu göster.
5. Yalnız bekleme aralığını kes; yapılandırılmış aday, `İnsan incelemesi gerekli`,
   `Model katkısı`, alan kanıtları ve girdi SHA-256 değerini göster.

```text
MTV Ödemelerinde Vade Farksız 3 Taksit
Kredi Kartı
01 Temmuz 2026 - 31 Temmuz 2026
```

Seslendirme:

> Buraya repo içindeki üç kısa kanıt alıntısını yapıştırıyorum; ham HTML veya tam
> sayfa taşımıyorum. Banka yalnız aktif BDDK kapsam listesinden seçilebiliyor.
> İstek URL, dosya veya araç yetkisi almıyor. Sonuç `unverified_preview`:
> `human_verified=false` ve `persisted=false`. Başlık, ürün ailesi, kampanya türü,
> tarih gibi yapılandırılmış alanlarla bunların alıntılarını birlikte gösteriyor.
> Sonuç kampanya listesine, karşılaştırmaya veya asistana eklenmiyor.

> Kural katmanı önce çalışıyor. Laptop profilinde çözülemeyen alanlar kalırsa yerel
> model çağrısı 120 saniyelik wall-clock sınırında deneniyor. Bu videoda yalnız
> bekleme aralığı kesildi. `Model katkısı` alanında çekim sırasında görülen kabul
> sayısını değiştirmiyoruz; sıfır kabul de geçerli bir sonuç.

Preview `Çekimser kalındı` dönerse bu durum saklanmaz. Kayıt, aynı çıktıyı
göstererek yeniden anlatılır veya teknik sorun giderildikten sonra baştan alınır.

## 02:30–03:05 — Pending kayıtlar ve filtre

Ekran:

1. **Kampanyalar** tablosundaki dört kaydı göster.
2. `Kanıt bağlı aday kayıtlar` başlığını ve `İnceleme gerekli` rozetlerini göster.
3. `Kategori → Kart` seç.

Seslendirme:

> PostgreSQL'deki demo listesinde dört kanıta bağlı aday var; dördü de insan
> incelemesi bekliyor. Kart filtresi T.O.M., Vakıf Katılım ve Ziraat Katılım
> kayıtlarını ayırıyor. Dünya Katılım kaydının ürün ailesi güvenle belirlenemediği
> için belirsiz bırakılmış.

## 03:05–03:40 — Kalıcı kaydın alan kanıtı

Ekran: `MTV Ödemelerinde Vade Farksız 3 Taksit` ayrıntısını aç. **Kaynak ve
doğrulama** ile **Alan kanıtları** bölümlerini göster, ardından paneli kapat.

Seslendirme:

> Kalıcı pending kayıtta başlık ve tarih kaynakta açıkça belirtilmiş; ürün ailesi
> ve kampanya türü kanıttan çıkarılmış olarak ayrı etiketleniyor. Her satırda alan
> işaretçisi, kısa alıntı ve kaynak blok kimliği var. Eksik alan başka bir
> kampanyadan doldurulmuyor.

Preview ile bu kaydın aynı şey olmadığı belirtilir: preview kalıcı değildir;
tablodaki kayıt sürümlü demo seed'inden gelir.

## 03:40–04:20 — Güvenli karşılaştırma reddi

Ekran:

1. Vakıf Katılım ve Ziraat Katılım kayıtlarını seç.
2. **Karşılaştırma** bölümünde `Vade` boyutunu açık bırak.
3. `Seçilenleri karşılaştır` düğmesine bas.
4. **Karşılaştırma yapılamadı** ve **Sıralanmadı** durumunu göster.

Seslendirme:

> Karşılaştırma motoru ürün ailesi, oran türü, dönem, para birimi, müşteri bağlamı
> ve geçerlilik bazlarını kontrol ediyor. Buradaki iki kayıt insan doğrulamasından
> geçmediği için önce `record_not_validated` kuralı uygulanıyor ve sıralama
> yapılmıyor. Bu başarılı karşılaştırma sonucu değil, yanlış kesinliği engelleyen
> güvenli ret.

## 04:20–04:45 — Sohbetin kanıt eşiği

Ekran: **Kaynaklı asistan** alanına `Kart kampanyalarını listele` yaz ve gönder.

Seslendirme:

> Soru izin verilen tipli sorgu planına dönüşüyor. Asistan yalnız `validated` ve
> kaynak alıntısı bulunan kayıtlardan yanıt kuruyor. Mevcut dört kayıt pending
> olduğu için “Yeterli kanıt bulunamadı” sonucu dönüyor; preview çıktısı da yanıt
> kaynağı yapılmıyor.

## 04:45–05:00 — Kapanış

Ekran: Genel görünüm, preview başlığı ve `Yerel çalışma` göstergesi aynı kadrajda.

Seslendirme:

> V1, yerel metin önizlemesini ve kanıt sınırını çalışan arayüzde gösteriyor.
> Başarılı ürün kıyası için insan-doğrulanmış uyumlu kayıt çifti hâlâ gerekli.
> Giriş, değişiklik bildirimi ve kurum kimlik entegrasyonu bu V1 gösteriminin
> parçası değil.

Son kare en az iki saniye sabit tutulur.
