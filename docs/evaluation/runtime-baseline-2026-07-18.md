# Yerel model çalışma tabanı — 2026-07-18

Bu kayıt bir yarışma performans iddiası değil, ilk entegrasyon ölçümüdür. Nihai
`EVAL-011` ölçümü ayrıca 16 GB CPU laptop profilinde tekrarlanacaktır.

## Ölçüm ortamı

- Ana makine: AMD Ryzen 9 9950X3D, 64 GB RAM, Windows/Docker Desktop
- Kind düğümü: Kubernetes 1.34.8, 32 vCPU ve yaklaşık 48 GiB bellek kotası
- Model sunucusu: Ollama 0.32.0, Kubernetes podu, GPU aktarımı olmadan CPU
- Model: `qwen3.5:4b`, Q4_K_M, disk üzerinde yaklaşık 3.4 GB
- Model bağlamı: 4.096 token

## Gözlenen sonuç

İlk yapılandırılmış JSON çağrısı soğuk durumda toplam yaklaşık 83.4 saniye
sürdü. Bunun yaklaşık 23.7 saniyesi model yükleme, 7.5 saniyesi prompt
değerlendirme ve 52.2 saniyesi 12 token üretimiydi. Bu nedenle 4B model CPU
üzerinde her kayıt için varsayılan yol olamaz.

Boş prompt ile yükleme Job'ı tamamlandıktan sonra `ollama ps` çıktısı modeli
3.2 GB, `%100 CPU`, 4.096 bağlam ve `Until=Forever` olarak gösterdi. Aynı anda
Kind düğümünün toplam bellek kullanımı yaklaşık 4.38 GiB idi; bu sayı modelin
yanında Kubernetes ve PostgreSQL yükünü de içerir.

## Gerçek API entegrasyon seyri

Bu bölüm başarısız denemeleri de saklar; yalnız son başarılı çıktıyı raporlamak
model sınırını olduğundan iyi gösterirdi.

1. İlk yeni-client isteği hemen `HTTP 400` döndürdü. Neden, Ollama HTTP
   gövdesine `keep_alive` değerinin string `"-1"` gönderilmesiydi. İstemci,
   sonsuz süreyi JSON number `-1`; pozitif Go-duration değerlerini string
   gönderecek şekilde düzeltildi.
2. Genel Pydantic JSON Schema ile gerçek çağrı 73.77 saniyede tamamlandı fakat
   `model_schema_invalid` oldu. Tek alanlı tanı isteği 23.67 saniyede modelin
   `document_id` değerini değiştirebildiğini ve alan-bağımlı semantik ipucunu
   atlayabildiğini gösterdi. `model_validator` koşullarının genel şemaya otomatik
   kodlandığı varsayımı yanlıştı.
3. Bunun üzerine istenen alanlara özel Draft 2020-12 şeması eklendi:
   `document_id` bir `const`, yalnız istenen alanlar üretilebilir,
   `additionalProperties=false` ve alan-bağımlı semantik ipuçları zorunludur.
   Şema ayrıca `Draft202012Validator` ile test edilir.
4. Düzeltilmiş gerçek istemci çağrısı 52.86 saniyede şema-geçerli, doğru belge
   ve doğru kanıt aralığına bağlı çıktı üretti. Bununla birlikte model, finansman
   kampanyası için `product_family=investment` önerdi. Deterministik semantik
   doğrulayıcı bu öneriyi reddetti. Bu sonuç, modelin yalnız aday üretici olması
   ve kaynak/kanıt otoritesi olmaması kararını doğrudan doğrular.
5. 16.000 karakter/1.536 çıktı-token sınırının CPU deadline'ıyla uyumsuz olduğu
   gerçek en-kötü istekle görüldü. Yeni 2.500 UTF-8 byte/192 token kapısı aynı
   bütün-alan şemasında 802 prompt + 101 çıktı tokenıyla 54.81 saniyede
   `done_reason=stop` verdi. Ayrıntılı kayıt
   `docs/evaluation/context-budget-2026-07-18.md` içindedir.

Bu sayılar kontrollü bir performans benchmark'ı değil, aynı sözleşmenin gerçek
Ollama 0.32.0 API'sine ulaştığının entegrasyon kanıtıdır. Resmî davranış ve kod
eşlemesi `docs/references/implementation-sources.md` içinde tutulur.

## Ürün profili kararı

- `laptop`: deterministik/kural öncelikli; model yalnız çözülemeyen veya belirsiz
  alanlarda çağrılır. Süre aşımında alan uydurulmaz, açık eksik/belirsiz durumu
  döndürülür.
- `workstation`/kalite CPU profili: `qwen3.5:4b`, `keep_alive=-1` ve 120 saniye
  sert uygulama zaman aşımı; model podu yeniden başladıktan
  sonra kısıtlı Kubernetes warm-up Job'ı çalıştırılır.
- İleride GPU bulunursa aynı sözleşme korunarak yalnız model çalışma adaptörü
  değiştirilebilir. GPU hiçbir V1 kabul kriterinin ön koşulu değildir.

Fine-tune kararı bu ölçümden çıkarılmaz. Önce sürümlü altın veri ve tutulmuş
değerlendirme kümesinde ADR/EVAL kapısının geçilmesi gerekir.
