# PostgreSQL çalışma sözleşmesi ve arama bakımı

Bu ürünün desteklenen veritabanı sözleşmesi PostgreSQL **17**, `pg_trgm`
**1.6** ve `unaccent` **1.1**'dir. İlk migration farklı ana sürüm veya extension
sürümünde fail-closed durur. API readiness ve worker başlangıcı ayrıca Türkçe
normalizasyon probunu çalıştırır:

```text
ÇĞİÖŞÜÂÎÛ çğıöşüâîû Hôtel Æ
→ CGIOSUAIU cgiosuaiu Hotel AE
```

Bu kilit keyfî değildir. PostgreSQL, stored generated column ifadesinde yalnız
immutable fonksiyonlara izin verir. `unaccent` ise kural dosyasıyla çalışan
`STABLE` bir fonksiyondur. Projedeki `immutable_unaccent` sarmalayıcısı ancak
extension sürümü ve kural semantiği çalışma sırasında değişmiyorsa doğrudur.

Birincil kaynaklar:

- [PostgreSQL 17 — Generated Columns](https://www.postgresql.org/docs/17/ddl-generated-columns.html)
- [PostgreSQL 17 — `unaccent`](https://www.postgresql.org/docs/17/unaccent.html)
- [PostgreSQL 17 — kurulu/kullanılabilir extension sürümleri](https://www.postgresql.org/docs/17/view-pg-available-extension-versions.html)
- [PostgreSQL 17 — `ALTER EXTENSION`](https://www.postgresql.org/docs/17/sql-alterextension.html)
- [PostgreSQL 17 — `REINDEX`](https://www.postgresql.org/docs/17/sql-reindex.html)
- [PostgreSQL 17 — `ANALYZE`](https://www.postgresql.org/docs/17/sql-analyze.html)
- [PostgreSQL 17 — `EXPLAIN`](https://www.postgresql.org/docs/17/using-explain.html)

## Salt okunur preflight

Kurum veritabanına migration uygulanmadan önce aşağıdaki sorgular yönetici
tarafından çalıştırılır. Çıktı release kanıtına eklenir; parola veya bağlantı
dizesi kaydedilmez.

```sql
SELECT current_setting('server_version'),
       current_setting('server_version_num');

SELECT extname, extversion
FROM pg_extension
WHERE extname IN ('pg_trgm', 'unaccent')
ORDER BY extname;

SELECT name, version, installed, trusted
FROM pg_available_extension_versions
WHERE name IN ('pg_trgm', 'unaccent')
ORDER BY name, version;
```

Extension'lar kurum yöneticisi tarafından önceden kurulabilir. Bu durumda
migration rolüne extension yönetim yetkisi vermek gerekmez. Yerel Kind profili
geliştirme kolaylığı için migration ve runtime bağlantısında aynı yerel hesabı
kullanır; kurum overlay'i bunları ayrı Secret anahtarları olarak bekler.

## Extension veya kural dosyası değişikliği

Normal deploy sırasında extension yükseltilmez. PostgreSQL paketinin,
`unaccent.rules` dosyasının veya extension sürümünün değişmesi ayrı ve planlı
bir bakım işlemidir:

1. Geri dönüşü doğrulanmış yedek alınır; API yazımları ve worker durdurulur.
2. Yeni paket önce eşdeğer staging veritabanında kurulur.
3. `ALTER EXTENSION ... UPDATE TO ...` yalnız kurumun migration/yönetim rolüyle
   çalıştırılır; yeni sürüm için kod ve migration sözleşmesi ayrıca güncellenir.
4. Yukarıdaki Türkçe probu ve arama regresyonları çalıştırılır.
5. Kural çıktısı değiştiyse stored generated kolonlar kaynak alanlara no-op
   update uygulanarak yeniden hesaplanır. Expression ve GIN indeksleri
   `REINDEX ... CONCURRENTLY` ile yeniden kurulur; bu komut transaction bloğu
   içinde çalıştırılmaz.
6. İlgili tablolar `ANALYZE` edilir; arama ve job-claim sorgularının gerçek
   `EXPLAIN` planları kaydedilir.
7. Readiness başarılı olmadan worker ve trafik açılmaz.

Tablo/indeks adları sürümler arasında değişebileceği için bakım SQL'i bu belgeye
kopyalanıp körlemesine çalıştırılmaz; o release'in migration şemasından üretilir
ve staging çıktısıyla onaylanır.

## Partial-index performans kapısı

PostgreSQL, partial index'i ancak sorgu koşulunun index predicate'ini planlama
anında ima ettiğini kanıtlayabilirse kullanır. Resmî dokümana göre parametreli
genel koşul bu garantiyi vermez. Job claim sorgusu bu nedenle güvenilir sabit
`status IN ('queued','running')` predicate'ini SQL metninde taşır; kullanıcı
girdisi bu sabite hiçbir zaman karışmaz.

Entegrasyon kapısı `plan_cache_mode=force_generic_plan` altında gerçek prepared
statement için `EXPLAIN (FORMAT JSON)` çalıştırır ve
`ix_durable_jobs_claim` planını arar. Bu kapı, yalnız küçük bir tabloda hızlı
çalışıyor olmayı index kullanımı kanıtı saymaz.

Birincil kaynaklar:

- [PostgreSQL 17 — Partial Indexes](https://www.postgresql.org/docs/17/indexes-partial.html)
- [PostgreSQL 17 — `PREPARE` ve generic/custom plan](https://www.postgresql.org/docs/17/sql-prepare.html)
