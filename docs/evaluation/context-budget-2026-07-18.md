# Model context ve çıktı bütçesi — 2026-07-18

Bu ölçüm `qwen3.5:4b` digest
`2a654d98e6fba55d452b7043684e9b57a947e393bbffa62485a7aac05ee4eefd`,
Ollama 0.32.0, CPU-only Kind podu ve `num_ctx=4096` ile yapılmıştır.

## Neden değişti?

İlk prompt sınırı 16.000 Unicode karakter ve model çıkış sınırı 1.536 tokendı.
Karakter sayısı token bütçesi değildir; ayrıca CPU profilinde yaklaşık 2 token/s
üretimle 1.536 token, 120 saniyelik ürün deadline'ıyla fiziksel olarak uyumsuzdu.

Eski sınırla bütün alanları isteyen sentetik koşumda Ollama logu:

- prompt processing: 2.050 token;
- prompt hızı: yaklaşık 58,6 token/s;
- 150 saniyede üretilen: 250 token, yaklaşık 2,1 token/s;
- toplam context kullanımı iptal anında: 2.300 token;
- `truncated=0`;
- istemci 150 saniyede isteği iptal etti; tamamlanmış yanıt yok.

Bu sonuç context taşması göstermedi fakat eski çıktı bütçesinin deadline'a
sığmadığını kanıtladı.

## Düzeltilmiş kapılar

- User mesajı, JSON escaping ve metadata dâhil **2.500 UTF-8 byte** ile sınırlı.
- Tek blok en çok 1.000 karakter; en çok 16 blok.
- Gerçek serialize edilmiş byte boyu her blok eklenirken binary search ile
  ölçülür; yüksek-entropili Unicode/escape içeriği karakter sınırını aşındıramaz.
- `num_predict=192`.
- Bütün HTTP response stream'i ayrıca 120 saniye wall-clock deadline içinde.
- Deadline veya yarım/şema-dışı çıktı, kuralları bozmaz; açık rules fallback olur.

## Düzeltilmiş gerçek koşum

Aynı bütün-alan şeması ve kaynak benzeri Türkçe sentetik metinle:

| Ölçü | Sonuç |
|---|---:|
| Serialize user mesajı | 2.500 byte / 2.325 karakter |
| JSON Schema | 9.957 byte |
| `prompt_eval_count` | 802 token |
| Prompt değerlendirme | 13,186 saniye |
| `eval_count` | 101 token |
| Üretim | 40,984 saniye |
| Toplam Ollama süresi | 54,626 saniye |
| Duvar saati | 54,807 saniye |
| HTTP / bitiş | 200 / `done=true`, `done_reason=stop` |
| Truncation belirtisi | Yok |
| Çıktı | Şemaya uygun abstention, 0 fact |

Model, tekrarlanan metinde gerçek kampanya ayrıntısı bulunmadığını belirterek
uydurma fact üretmedi. Bu tek ölçüm bütün Türkçe web sayfaları için performans
genellemesi değildir; byte/output kapılarının aynı gerçek API'de deadline'a
sığan tamamlanmış bir sonuç ürettiğinin entegrasyon kanıtıdır.

Resmî davranış kaynakları:

- [Ollama context length](https://docs.ollama.com/context-length)
- [Ollama chat API ve `prompt_eval_count`](https://docs.ollama.com/api/chat)
- [Python 3.12 `asyncio.timeout`](https://docs.python.org/3.12/library/asyncio-task.html#timeouts)
