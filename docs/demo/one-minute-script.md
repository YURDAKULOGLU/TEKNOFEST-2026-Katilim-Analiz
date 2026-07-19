# Bir dakikalık V1 demo senaryosu

Hedef süre: **55–60 saniye**. Kayıttan önce güncel KYS duyurusu kontrol edilir.
Preview çağrısındaki bekleme kesilebilir; istek, sonuç ve ekrandaki durum
değiştirilemez.

| Süre | Ekran | Seslendirme |
|---|---|---|
| 00:00–00:05 | `kubectl get pods`; `api`, `worker`, `postgres`, `ollama` hazır. | “Ben Ahmet Yurdakul. Katılım Analiz V1, PostgreSQL ve Ollama ile yerel Kubernetes kümesinde çalışıyor.” |
| 00:05–00:12 | **Genel görünüm**: 10 banka, 6/10 başarılı tarama, 0 doğrulanmış kampanya. | “On bankanın altısı taranmış, dördünde erişim engeli kaydedilmiş; insan-doğrulanmış kayıt sayısı sıfır.” |
| 00:12–00:29 | **Çıkarım önizlemesi**: Vakıf Katılım seçilir; üç satırlık kısa kanıt metni yapıştırılır, `Çıkarımı önizle` tıklanır. Bekleme kesilerek yapılandırılmış sonuç ve kanıt listesi gösterilir. | “Repo içindeki kısa kanıt alıntılarını giriyorum. Çıktı yapılandırılmış ve alan kanıtlı; fakat `unverified_preview`, insan doğrulaması yok ve veritabanına yazılmıyor. Yerel model yalnız çözülemeyen alanları dener; CPU çağrısı 120 saniyeye kadar sürebilir.” |
| 00:29–00:38 | **Kampanyalar**: dört `İnceleme gerekli` kayıt; `Kategori → Kart` ile üç kayıt. | “Kalıcı listede dört pending aday var. Kart filtresi üçünü ayırıyor.” |
| 00:38–00:47 | Vakıf ve Ziraat seçilir; **Karşılaştırma yapılamadı** ve **Sıralanmadı** görünür. | “İki pending kayıt seçildiğinde motor sıralama yapmıyor.” |
| 00:47–00:56 | Sohbete `Kart kampanyalarını listele` yazılır; **Güvenli geri çekilme** görünür. | “Asistan da doğrulanmış kaynak bulamayınca cevap uydurmuyor.” |
| 00:56–01:00 | Ürün başlığı ve `Yerel çalışma`; kare sabit. | “Eksik bilgi, sonuç diye sunulmuyor.” |

## Önizlemeye yapıştırılacak metin

```text
MTV Ödemelerinde Vade Farksız 3 Taksit
Kredi Kartı
01 Temmuz 2026 - 31 Temmuz 2026
```

## Uygulama notları

- Üç satır, demo datasetindeki kısa kanıt alıntılarıdır; ham HTML veya tam sayfa
  dağıtılmaz.
- Preview beklemesi kesildiğinde ekrana kısa bir `bekleme aralığı kesildi`
  açıklaması eklenir. İşlem süresi ölçülmediyse süre söylenmez.
- `Model katkısı` alanındaki sayı önceden okunmaz. `0` da geçerli ve gösterilmesi
  gereken bir sonuçtur.
- Önizleme çıktısı pending kampanya listesine eklenmiş gibi anlatılmaz.
- Karşılaştırma başarılı sonuç değil, doğrulanmamış kaydı sıralamayan güvenli rettir.
- Sohbet sorusu aynen `Kart kampanyalarını listele` olmalıdır.
- Kayıt 60 saniyeyi geçerse terminal geçişi veya son sabit kare kısaltılır;
  seslendirme hızlandırılmaz.
