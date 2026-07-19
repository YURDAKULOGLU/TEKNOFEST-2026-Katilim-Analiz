# V1 demo paketi

Bu klasör, Katılım Analiz V1'in kayda alınabilir davranışını anlatır. Metinler
`datasets/demo/v1/seed.json` sürüm `1.0.0` ve 18 Temmuz 2026 kaynak gözlemi
üzerinden hazırlanmıştır. Senaryo metinleri canlı kayıt veya ekran görüntüsü
değildir; hedefli test koşumları kanıt listesinde ayrıca yazılır.

Resmî dayanaklar:

- [TEKNOFEST 2026 Yapay Zekâ Dil Ajanları Yarışması sayfası](https://www.teknofest.org/tr/yarismalar/yapay-zeka-dil-ajanlari-yarismasi/)
- [2. Senaryo teknik şartnamesi](https://cdn.teknofest.org/media/upload/userFormUpload/2026_TYDA_SARTNAME_Ikinci_Senaryo_TR_1_1IAJb.pdf)

Şartnamenin 6. bölümü en fazla beş dakikalık bir demo videosu, 10. bölümü ise
sunum için bir dakikalık demo videosu ister. Bu nedenle iki ayrı akış tutulur.
Güncel KYS duyurusu veya resmî yarışma iletişimi farklı bir süre bildirirse o
bilgi uygulanır.

## Değişmeyen anlatı tabanı

| Konu | Gösterilebilen V1 gerçeği |
|---|---|
| Banka kapsamı | BDDK listesindeki 10 bankanın tamamı ayrı durumla temsil edilir. |
| Kaynak taraması | 6 başarılı, 4 erişim engelli gözlem vardır. |
| Demo kayıtları | 4 kısa, alan kanıtı bulunan kayıt PostgreSQL'e yüklenir. |
| İnsan incelemesi | Dört kaydın da durumu `needs_review`; insan doğrulaması yoktur. |
| Metin önizlemesi | Kapsamdaki banka ve operatör metniyle kanıta bağlı, kalıcı olmayan yapılandırılmış aday üretir. |
| Filtre örneği | `Kategori → Kart` seçimi 3 pending kaydı ayırır. |
| Ayrıntı | Alan işaretçisi, kaynak alıntısı, kanıt durumu ve kaynak blok kimliği görünür. |
| Karşılaştırma | Pending kayıtlar sıralanmaz; `record_not_validated` kuralı uygulanır. |
| Sohbet | Yalnız `validated` kayıtlarla yanıt üretir; mevcut seed ile güvenli geri çekilir. |
| Yerel çalışma | API, worker, PostgreSQL ve Ollama tek düğümlü Kind kümesinde çalışır. |

Başarılı tarama sayısı, doğrulanmış kayıt sayısı değildir. Coverage API'deki
`campaign_count`, insan incelemesi tamamlanmış güncel kayıt sayısıdır ve demo
seed'inde bütün bankalar için `0` döner. Dataset içindeki `source_candidate_count`
toplamı 8 kaynak adayı gözlemini korur; demo veritabanında ayrıca 4 pending kayıt
vardır. Sunucu bu üç sayıyı birbirinin yerine kullanmamalıdır.

Yerel profil `qwen3.5:4b` modelini tam
`2a654d98e6fba55d452b7043684e9b57a947e393bbffa62485a7aac05ee4eefd`
digest'iyle kabul eder. Model çözülmeyen alanlar için çağrılabilir; wall-clock
sınırı 120 saniyedir ve model `keep_alive=-1` ile bellekte tutulur.

## Metin önizleme girdisi

İki videoda Vakıf Katılım seçilir ve repo içindeki yayımlanabilir kısa kanıt
alıntıları kullanılır:

```text
MTV Ödemelerinde Vade Farksız 3 Taksit
Kredi Kartı
01 Temmuz 2026 - 31 Temmuz 2026
```

Bu üç satır ham HTML veya tam sayfa kopyası değildir. Önizleme sonucu
`unverified_preview`, `human_verified=false` ve `persisted=false` kalır. Çıktı
kampanya listesine, karşılaştırmaya veya kaynaklı asistana eklenmez. Ekrandaki
`Model katkısı` sayısı çekim sırasında ne dönerse o gösterilir; kabul edilen alan
sayısı önceden söylenmez.

## Kapsam durumu

Başarılı kaynak taraması:

- Albaraka Türk Katılım Bankası
- Dünya Katılım Bankası
- T.O.M. Katılım Bankası
- Türkiye Emlak Katılım Bankası
- Vakıf Katılım Bankası
- Ziraat Katılım Bankası

Erişim engeli kaydedilen bankalar:

- Adil Katılım Bankası
- Hayat Finans Katılım Bankası
- Kuveyt Türk Katılım Bankası
- Türkiye Finans Katılım Bankası

Demo kayıtları:

- Dünya Katılım — Network'te 4 Taksit Fırsatı
- T.O.M. Katılım — Restoran harcamalarında %10 İade Kazan
- Vakıf Katılım — MTV Ödemelerinde Vade Farksız 3 Taksit
- Ziraat Katılım — Puffy'de 6'ya varan Taksit

Bu başlıklar birer insan-doğrulanmış altın kayıt değildir. Ekranda ve seslendirmede
`kanıtlı aday`, `pending kayıt` veya `inceleme gerekli` denir.

## Dosyalar

- [Bir dakikalık senaryo](one-minute-script.md)
- [Beş dakikalık senaryo](five-minute-script.md)
- [Arıza ve geri dönüş akışı](failure-and-fallback.md)
- [Prova kontrol listesi](rehearsal-checklist.md)
- [Çekim listesi](../../artifacts/demo/shot-list.md)
- [Kanıt kontrol listesi](../../artifacts/demo/evidence-checklist.md)

## Kayıt kuralı

Ekrandaki sonuç senaryodan farklıysa kayıt durdurulur. Sayı, durum, performans,
lisans veya ağ davranışı seslendirmeyle düzeltilmez. Aynı commit üzerinde alınmış
gerçek ekran ve terminal çıktısı kullanılır; temsili çıktı yerleştirilmez.
