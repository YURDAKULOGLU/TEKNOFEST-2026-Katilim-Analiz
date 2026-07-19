# ADR-010: PostgreSQL tabanlı yerel yönetici oturumu

- Durum: Kabul edildi; V1.2'de uygulanacak
- Tarih: 2026-07-19
- İlgili kapsam: ENH-002, EVAL-016

## Bağlam

Takım, çalışan V1 sonrasında ürüne giriş paneli eklenmesini istedi. Bu ürün tek
düğümlü yerel Kubernetes ve kurum içi Kubernetes profillerinde çalışır. Yarışma
teslimi için bir OAuth sağlayıcısı, Active Directory, Redis veya ayrı kimlik
servisi bulunacağı varsayılamaz. Buna rağmen bilinen parola, düz metin token,
yalnız arayüzde gizlenen yetki veya bellekte kaybolan oturum kabul edilemez.

Giriş, herkese açık okuma ve demo akışını kapatmaz. Yönetim mutasyonları,
periyodik taramayı elle tetikleme ve V1.1 bildirim okuma işlemleri yönetici
oturumu ister. İnsan doğrulama kararı ayrı bir değişmez karar modeli olmadan
`campaign_records.status` alanını yerinde güncelleyemez.

## Karar

### Parola

- `argon2-cffi` ile Argon2id v19 kullanılır.
- Parametreler `memory_cost=19456 KiB`, `time_cost=2`, `parallelism=1`,
  `salt_len=16`, `hash_len=32` olarak açıkça sabitlenir.
- Parola, NFC normalizasyonundan sonra 15–128 Unicode code point'tir. Başka
  kırpma, trim, case dönüşümü veya kompozisyon kuralı uygulanmaz; bütün değer
  doğrulanır ve hashlenir.
- Bootstrap sırasında aday parolanın NFC+casefold edilmiş bütün değeri,
  sürümlü yerel common/context-specific blocklist ile karşılaştırılır;
  substring eşleşmesi yapılmaz. Liste bulunamaz veya okunamazsa bootstrap
  fail-closed olur. Reddedilme nedeni operatöre söylenir.
- Bilinmeyen, pasif ve kilitli kullanıcı yolları geçerli sabit bir dummy PHC
  üzerinde de bir Argon2 doğrulaması yapar ve aynı genel hatayı döndürür.
- Başarılı girişte `check_needs_rehash()` kontrol edilir. Argon2 işi event-loop
  üzerinde değil, kapasitesi bir olan sınırlı bir worker thread üzerinde çalışır.
- Hedef donanımda ölçülmeyen hash süresi için performans iddiası yapılmaz.

Bu değerler [OWASP Password Storage][owasp-password] güncel asgari Argon2id
profilini izler. Kitaplığın gerçek API ve rehash davranışı
[argon2-cffi API][argon-api] ile doğrulanır. 15 karakter asgarisi, en az 64
karakter maksimum desteği, Unicode code-point sayımı, NFC, bütün-parola
doğrulaması ve blocklist seçimi [NIST SP 800-63B-4][nist-passwords] üzerinden
alınmıştır. Bu seçim tek başına bir NIST AAL/FIPS uygunluk iddiası değildir.
Python uygulaması NFC için [`unicodedata.normalize()`][python-unicode] kullanır.

### Oturum ve cookie

- Oturum ve CSRF tokenları ayrı ayrı `secrets.token_urlsafe(32)` ile üretilir.
- PostgreSQL yalnız tokenların SHA-256 özetini tutar; ham değer log, audit veya
  veritabanına yazılmaz.
- Oturum cookie'si `__Host-kas`; `Secure`, `HttpOnly`, `SameSite=Strict`,
  `Path=/` ve `Domain` olmadan gönderilir.
- CSRF cookie'si `__Host-kac`; aynı host/secure/samesite sınırında fakat
  JavaScript'in özel header'a kopyalayabilmesi için `HttpOnly=false` olur.
- Oturum 30 dakika boşta kalma ve 8 saat mutlak süreyle sunucuda sona erer.
  `last_seen_at` en fazla dakikada bir yazılır.
- Başarılı yeni giriş önceki aktif oturumları, logout ise mevcut oturumu iptal
  eder. JWT ve refresh token kullanılmaz.

Python 3.12, 32 baytı tipik token kullanımı için yeterli ve `secrets` modülünü
OS CSPRNG kaynağı olarak belgeler: [Python `secrets`][python-secrets]. Cookie ve
sunucu tarafı süre sınırları [OWASP Session Management][owasp-session] ile
uyumludur. `__Host-` önekinin `Secure`, kök path ve host-only şartları doğrudan
[IETF HTTPbis RFC6265bis taslağındaki][cookie-draft] sözleşmedir. Taslak
2026-07-19 tarihinde RFC Editor yayın kuyruğundadır; henüz yayımlanmış RFC diye
sunulmaz.

### CSRF ve aynı-origin sınırı

Her korumalı mutasyonda:

1. oturum cookie'si geçerli olmalıdır;
2. `X-CSRF-Token` header'ı CSRF cookie'siyle sabit zamanda eşleşmelidir;
3. header tokenının özeti oturum satırındaki `csrf_hash` ile eşleşmelidir;
4. `Origin`, yapılandırılmış public origin ile birebir aynı olmalıdır;
5. varsa `Sec-Fetch-Site` yalnız `same-origin` olabilir.

Login isteği de aynı origin ile `X-Auth-Intent: login` header'ı ister. Credential
CORS açılmaz ve tokenlar Web Storage'a yazılmaz. Bu, yalnız cookie eşitliğine
dayanan naive double-submit değildir; token sunucu oturumuna bağlıdır. Kaynak:
[OWASP CSRF Prevention][owasp-csrf].

### Brute-force ve audit

Redis olmadan PostgreSQL'de iki kayan pencere kullanılır:

- kullanıcı adı özeti başına 15 dakikada 10 giriş rezervasyonu;
- doğrulanmış istemci adresi özeti başına 60 saniyede 20 giriş rezervasyonu.

İstemci adresi uygulama tarafından serbest `X-Forwarded-For` header'ından
okunmaz. Yerel profilde doğrudan ASGI peer adresi kullanılır. Kurum ingress'i
ancak gerçek proxy CIDR'leri [Uvicorn `forwarded-allow-ips`][uvicorn-proxy]
listesine verilince forwarded adresi etkinleştirir; `*` kullanılmaz. Adres
SHA-256 özeti yalnız ham
değerin yanlışlıkla loglanmasını azaltır, anonimleştirme iddiası değildir.

Beş başarısız girişten sonra 30 saniyeden başlayıp en çok 15 dakikaya çıkan
hesap beklemesi uygulanır. İki API replikasında sınır aşılmaması için kullanıcı
ve istemci özetlerinin transaction advisory lock'ları sabit sırada alınır;
rezervasyon aynı PostgreSQL transaction'ında yazılır. Argon2 çalışırken DB lock
tutulmaz. Audit append-only'dir ve parola, ham IP/UA, token, CSRF ya da banka
metni içermez.

### Bootstrap

- Operatör parolayı echo kapalı TTY'de iki kez girerek yerelde Argon2 PHC üretir.
- Yalnız PHC, kısa ömürlü bir Kubernetes Secret ile tek seferlik bootstrap
  Job'una dosya olarak bağlanır.
- Job yalnız hiç kullanıcı yoksa tek `admin` hesabı oluşturur; var olan hesabın
  parolasını değiştirmez ve çelişkide fail-closed olur.
- API ve worker bootstrap Secret'ını mount etmez. Script Job sonucu alındıktan
  sonra Secret'ı siler.
- Repoda, imajda ve varsayılan manifestte bilinen parola bulunmaz.

Kubernetes, Secret verisinin varsayılan olarak etcd'de şifrelenmeden
saklanabileceğini açıkça belirtir; bu nedenle Secret kısa ömürlü ve en dar pod
sınırında tutulur: [Kubernetes Secrets][k8s-secrets] ve
[good practices][k8s-secrets-good].

### API sınırı

- `POST /api/v1/auth/login`
- `GET /api/v1/auth/session`
- `POST /api/v1/auth/logout`
- `POST /api/v1/admin/ingestion-runs`
- V1.1 bildirim okuma mutasyonu

Korumalı rotalar FastAPI `APIKeyCookie(auto_error=False)` ile OpenAPI cookie
security scheme üretir; kimliksiz istek `401`, yetkisiz oturum `403` alır.
Bütün auth yanıtlarında `Cache-Control: no-store` bulunur. Arayüzde buton
gizlemek yetkilendirme sayılmaz. Kaynak: [FastAPI Security][fastapi-security].

## Yerel HTTP kararı

Yerel canonical origin `http://localhost:8080` olur; `127.0.0.1` veya LAN IP
auth için vaat edilmez. `Secure` cookie davranışı pinned Chromium ile gerçek
tarayıcı E2E testinde kanıtlanır. Test başarısızsa `Secure=false` kaçışı
eklenmez; yerel TLS kurulur. W3C Secure Contexts `localhost` originini yalnız
adı loopback dışına çözülmüyorsa potansiyel olarak güvenilir sayar; bu standart
tarayıcı davranışının yerine test sonucu koymak için değil, localhost sınırını
açıklamak için kullanılır: [W3C Secure Contexts][secure-contexts]. Bu kanıt
olmadan EVAL-016 geçmez. Chromium'un kendi `SecureCookieLocalhost` regresyonu
ek bir uygulama kanıtıdır; ürünün pinned sürümü yine ayrıca çalıştırılır:
[Chromium cookie testi][chromium-cookie-test].

## Reddedilen seçenekler

- JWT/refresh token: iptal ve tek-oturum politikasını gereksiz zorlaştırır.
- Redis: yalnız bir yönetici ve PostgreSQL varken ek işletim bağımlılığıdır.
- Keycloak/OAuth sunucusu: yarışma ve laptop profili için gereksiz servistir.
- Active Directory taklidi: gerçek kurum sözleşmesi olmadan sahte entegrasyon
  iddiası oluşturur.
- Uygulama içinde default parola: teslim artefaktında credential sızıntısıdır.

## Doğrulama şartları

EVAL-016; gerçek Argon2 parametrelerini, eşzamanlı rate limitini, generic hata
eşitliğini, session/CSRF süresini, cookie flaglerini, ikinci login iptalini,
restart sonrası PostgreSQL oturumunu, audit redaksiyonunu, bootstrap create-once
davranışını, OpenAPI drift'ini ve gerçek Chromium localhost akışını kapsar.

[owasp-password]: https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html
[argon-api]: https://argon2-cffi.readthedocs.io/en/stable/api.html
[nist-passwords]: https://pages.nist.gov/800-63-4/sp800-63b.html#passwordver
[python-unicode]: https://docs.python.org/3.12/library/unicodedata.html#unicodedata.normalize
[python-secrets]: https://docs.python.org/3.12/library/secrets.html
[owasp-session]: https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html
[owasp-csrf]: https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html
[cookie-draft]: https://httpwg.org/http-extensions/draft-ietf-httpbis-rfc6265bis.html
[chromium-cookie-test]: https://chromium.googlesource.com/chromium/src/+/HEAD/net/cookies/cookie_monster_unittest.cc
[secure-contexts]: https://www.w3.org/TR/secure-contexts/#is-origin-trustworthy
[k8s-secrets]: https://kubernetes.io/docs/concepts/configuration/secret/
[k8s-secrets-good]: https://kubernetes.io/docs/concepts/security/secrets-good-practices/
[fastapi-security]: https://fastapi.tiangolo.com/reference/security/
[uvicorn-proxy]: https://www.uvicorn.org/settings/#http
