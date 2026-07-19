# Demo prova kontrol listesi

Bu liste her çekim ve canlı gösterim öncesinde yeniden doldurulur. İşaretlenmemiş
madde geçmiş bir başarı varsayılmaz.

## 1. Resmî kapsam ve kayıt kimliği

- [ ] KYS, yarışma e-postası ve resmî yarışma sayfasındaki son video süresi kontrol edildi.
- [ ] Gösterilecek commit SHA kaydedildi.
- [ ] Çalışma ağacındaki değişikliklerin kayda etkisi açıklandı veya temiz commit kullanıldı.
- [ ] Video dosya adı commit SHA ve çekim tarihini içeriyor.
- [ ] Takım/ürün adı son başvuru bilgisiyle eşleşiyor.

## 2. Yerel ortam

- [ ] Docker Engine çalışıyor.
- [ ] Bağlam `kind-katilim-analiz`; yanlış Kubernetes kümesi seçili değil.
- [ ] `api`, `worker`, `postgres` ve `ollama` podları hazır.
- [ ] Migration ve `demo-seed` Job'ları başarıyla tamamlandı.
- [ ] `/health/live` yanıt veriyor.
- [ ] `/health/ready` durumu `ok`.
- [ ] Ollama envanterindeki `qwen3.5:4b` digest'i `2a654d98e6fba55d452b7043684e9b57a947e393bbffa62485a7aac05ee4eefd` ile birebir eşleşiyor.
- [ ] `ollama ps` modelin bellekte kaldığını gösteriyor.
- [ ] Ürün `http://127.0.0.1:8080` adresinde açılıyor.

## 3. Veri doğruluğu

- [ ] Coverage API tam 10 benzersiz banka döndürüyor.
- [ ] Durum dağılımı 6 `success`, 4 `blocked`.
- [ ] Coverage API'deki doğrulanmış `campaign_count` toplamı 0.
- [ ] Dataset içindeki `source_candidate_count` toplamı 8; public doğrulanmış kayıt sayısına eklenmiyor.
- [ ] Kampanya API tam 4 demo kaydı döndürüyor.
- [ ] Dört kaydın durumu da `needs_review`.
- [ ] Her kayıtta en az bir görüntülenebilir alan kanıtı var.
- [ ] Kart filtresi üç kayıt gösteriyor.
- [ ] Preview banka listesi coverage yanıtındaki 10 bankadan geliyor.
- [ ] Vakıf Katılım ve üç satırlık kanıt metniyle preview isteği gönderiliyor.
- [ ] Preview `scope=unverified_preview`, `human_verified=false`, `persisted=false` gösteriyor.
- [ ] Preview başlığı ilk satır alıntısına, yapılandırılmış alanlar kendi kanıtlarına bağlı.
- [ ] Preview sonucu kampanya sayısını değiştirmiyor ve karşılaştırma/chat kaynağı olmuyor.
- [ ] `Model katkısı` değeri çekim sırasında görüldüğü gibi okunuyor; önceden sayı yazılmıyor.
- [ ] 120 saniyelik yerel model uyarısı ekranda görünür ve bekleme kesintisi açıkça belirtiliyor.
- [ ] Vakıf MTV ayrıntısında başlık, ürün ailesi, kampanya türü ve geçerlilik kanıtları açılıyor.
- [ ] Vakıf ve Ziraat seçimi sıralanmıyor; `record_not_validated` nedeni korunuyor.
- [ ] `Kart kampanyalarını listele` sorusu güvenli geri çekilme ve boş citation listesi döndürüyor.

## 4. Görüntü ve mahremiyet

- [ ] Bildirimler, e-posta, mesajlaşma ve kişisel tarayıcı sekmeleri kapalı.
- [ ] Terminalde token, parola, kullanıcı dizini veya özel registry bilgisi görünmüyor.
- [ ] Tarayıcı yakınlaştırması metinleri kesmeden okunabilir.
- [ ] Kayıt çözünürlüğü UI metnini ve terminal durumunu okunur tutuyor.
- [ ] İmleç hareketleri prova edildi; hızlı kaydırma yok.
- [ ] Resmî kaynak bağlantısı çevrimdışı provada açılmıyor.
- [ ] Preview girdisi yalnız üç kısa yayımlanabilir kanıt alıntısı; ham HTML veya tam sayfa yok.
- [ ] Ses düzeyi, dip gürültüsü ve Türkçe telaffuz kontrol edildi.

## 5. Anlatı doğruluğu

- [ ] “Doğrulanmış kampanya” yerine “pending/inceleme gerekli kanıtlı aday” deniyor.
- [ ] Dataset'teki 8 kaynak adayı, public 0 doğrulanmış kayıt ve 4 pending demo kaydıyla karıştırılmıyor.
- [ ] Başarılı tarama, başarılı çıkarım veya insan doğrulaması sayılmıyor.
- [ ] Preview sonucu kalıcı kayıt, insan doğrulaması veya altın veri diye sunulmuyor.
- [ ] Preview ile sürümlü demo seed kaydı birbirinden ayrılıyor.
- [ ] Karşılaştırma ekranı güvenli ret olarak anlatılıyor; başarı sonucu denmiyor.
- [ ] Sohbet cevabı LLM üretimi diye tanıtılmıyor.
- [ ] Fine-tuning, performans, doğruluk veya maliyet metriği uydurulmuyor.
- [ ] Login, bildirim ve kurum entegrasyonu V1 özelliği diye gösterilmiyor.
- [ ] Finansal tavsiye veya katılım ilkelerine uygunluk kararı verilmiyor.

## 6. Süre provası

- [ ] Bir dakikalık akış normal konuşma hızında 55–60 saniye.
- [ ] Beş dakikalık akış 4:40–5:00 aralığında.
- [ ] Preview beklemesi kesildiyse ekranda bu kesinti belirtiliyor; istek ve sonuç kesintisiz aynı koşuma ait.
- [ ] Her tıklamanın yeri sunucu tarafından ezberlenmiş değil, prova edilmiş.
- [ ] Yükleme beklemeleri kesilebilir; sonuç ekranı kesilmez veya değiştirilmez.
- [ ] Son kare en az iki saniye sabit.

## 7. Geri dönüş

- [ ] Aynı commit'ten alınmış yedek video yerel diskte açılıyor.
- [ ] Yedek videonun SHA-256 değeri kaydedildi.
- [ ] API teşhis komutları ayrı PowerShell sekmesinde hazır.
- [ ] Preview API teşhis gövdesi ve üç satırlık girdi ayrı PowerShell sekmesinde hazır.
- [ ] Arıza halinde kimin konuşacağı ve kimin tanı koyacağı belirlendi.
- [ ] Sayı veya durum uyuşmazlığında demoyu durdurma kararı ekipçe kabul edildi.

## Prova kaydı

| Alan | Değer |
|---|---|
| Tarih/saat |  |
| Commit SHA |  |
| Sunucu |  |
| Gözlemci |  |
| 1 dk gerçek süre |  |
| 5 dk gerçek süre |  |
| Hata/not |  |
| Tekrar gerekli mi? |  |
