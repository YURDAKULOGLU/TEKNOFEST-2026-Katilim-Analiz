# ADR-011: OpenAPI ve CloudEvents tabanlı kurum entegrasyon sınırı

- Durum: Kabul edildi; V1.3'te uygulanacak
- Tarih: 2026-07-19
- İlgili kapsam: ENH-003, EVAL-017

## Bağlam

Takım ürünün kurum altyapısına eklenebilir olmasını istiyor. Fakat yarışma
ortamında gerçek AD/LDAP/OIDC, SMTP, event broker, kurum CA'sı, gateway etiketi,
OpenShift SCC'si veya yönetilen PostgreSQL sözleşmesi verilmedi. Bu dış
sistemleri taklit edip “entegre” demek doğrulanamaz. Aynı zamanda yalnız local
demo bırakmak da mimari entegrasyon beklentisini karşılamaz.

## Karar

Mevcut Kubernetes-first modüler monolit korunur. Tek OCI imajı ayrı `api`,
`worker` ve migration Job rollerinde çalışır. Yeni mikroservis, Kafka, Redis,
service mesh veya gRPC eklenmez.

### Giriş ve çıkış sözleşmeleri

- Kurumsal giriş sözleşmesi mevcut OpenAPI 3.1 `/api/v1` API'sidir.
- Kurumsal çıkış sözleşmesi CloudEvents 1.0.2 structured JSON over HTTPS'tir.
- FastAPI top-level `app.webhooks` tanımı olayı OpenAPI'de belgeler; bu bir
  inbound receiver route'u açmaz [FastAPI webhooks][fastapi-webhooks].
- Aynı Pydantic model, OpenAPI webhook şeması ve sürümlü JSON Schema artefaktı
  için tek kaynaktır.

CloudEvent şu invariantları taşır:

- `specversion="1.0"`;
- HTTP `Content-Type=application/cloudevents+json`; event içindeki
  `datacontenttype=application/json` ile karıştırılmaz;
- retry boyunca byte-identical `source`, `id` ve `data`;
- `type=io.github.yurdakuloglu.katilimanaliz.campaign.record.changed.v1`;
- `subject=campaign_key`;
- `datacontenttype=application/json`;
- veri: `campaign_key`, `record_id`, `record_version`, `change_kind`, gerçek
  record durumu, `previous_record_id`, `observed_at`;
- toplam gövde en çok 64 KiB;
- ham HTML, evidence metni, prompt, session/kullanıcı ve secret içermez.

Sözleşme [CloudEvents Core 1.0.2][ce-core], [JSON event format][ce-json] ve
[HTTP binding][ce-http] belgelerine dayanır. V1 içinde yalnız geriye uyumlu
opsiyonel alan eklenebilir; alan silme/ad/tip/anlam değişikliği yeni event
sürümü ister. 64 KiB sınırı keyfî bir payload tahmini değildir; CloudEvents
Core'un bütün aracıların iletmesi gereken wire-size sınırıdır.

### Transactional outbox ve teslimat

ADR-009 kampanya sürümüyle outbox kaydını aynı PostgreSQL transaction'ında
üretir. Publisher mevcut worker içinde bağımsız bir döngüdür ve ingestion ağı
kapalıyken de çalışabilir.

- Teslim garantisi at-least-once'dur; exactly-once iddiası yoktur.
- Tüketici `(source,id)` ile dedupe eder ve daha düşük `record_version`
  değerini yok sayabilir; global sıra garantisi verilmez.
- [RFC 9110][http-status] anlamıyla 2xx başarıdır. Timeout, transport ve non-2xx sonucu aynı
  event ID/payload ile
  retry edilir.
- Redirect izlenmez ve TLS doğrulaması kapatılmaz.
- “HTTP kabul etti, `mark_published` yazılamadı” halinde duplicate teslim
  beklenen ve test edilen davranıştır.

Çalışma profilleri:

| Profil | Davranış |
|---|---|
| local/laptop | `EVENT_PUBLISHER_MODE=log`; geçerli CloudEvent structured JSON olarak stdout'a yazılır. Harici teslimat iddiası yoktur. |
| institution taban | `disabled`; endpoint olmadan sahte başarı üretilmez. |
| institution HTTPS | Yalnız gerçek HTTPS endpoint, auth materyali, CA ve NetworkPolicy hedefi sağlanınca etkinleşir; eksik ayarda startup fail-closed olur. |

Geçici sink hatası API readiness'ini düşürmez ve worker'ı öldürmez; pending
outbox ve retry görünür kalır.

### Kubernetes release ve ağ sınırı

NetworkPolicy kuralları additive'dir; institution overlay dar bir policy
ekleyerek tabandaki geniş `allow-api-ingress` kuralını daraltamaz. Overlay önce
bu kuralı siler ve gerçek gateway namespace+pod selector verilene kadar ingress
kapalı kalır [Kubernetes NetworkPolicy][k8s-network-policy]. Standart
NetworkPolicy FQDN egress ifade etmez; dış sink için kurumun sabit CIDR'i ya da
belgelenmiş CNI FQDN özelliği gerekir.

Roller en az ayrı egress alır:

- API → PostgreSQL ve yapılandırıldıysa yerel model;
- worker → PostgreSQL, model ve yalnız etkinse onaylı HTTPS sink;
- migration → yalnız PostgreSQL.

ServiceAccount token varsayılan olarak mount edilmez. Gerçek workload identity
istenmeden RBAC eklenmez.

Sabit adlı migration Job, release sırasında açıkça silinip aynı immutable image
digest'iyle yeniden oluşturulur. Job tamamlanmadan API/worker rollout başarılı
sayılmaz. Migration fail olursa release durur. Bu akış Kubernetes Job,
`kubectl wait` ve rollout status sözleşmelerini izler
[Kubernetes Jobs][k8s-jobs], [kubectl wait][kubectl-wait],
[rollout status][kubectl-rollout].

### Kanıt sınırı

- AD/LDAP/OIDC provider metadata ve grup eşlemesi olmadan uygulanmış sayılmaz.
- SMTP gerekmez; CloudEvent tüketicisi isterse e-posta gönderir.
- Sabit `runAsUser` OpenShift random UID/SCC uyumluluğu kanıtı değildir;
  OpenShift küme testi olmadan destek iddiası yapılmaz.
- Kurum endpoint'i olmadan yalnız local log adapter conformance kanıtlanır.
- Yönetilen PostgreSQL TLS/HA/backup özellikleri kurum girdisi olmadan vaat
  edilmez.

## Reddedilen seçenekler

- Sırf “mikroservis” demek için event servisini ayırmak.
- Yerel Kafka/Redis kurup kurum broker'ı varmış gibi davranmak.
- Endpoint yokken başarılı teslim loglamak.
- TLS doğrulamasını veya Secure cookie'yi demo kolaylığı için kapatmak.
- Standart NetworkPolicy'ye FQDN yazarak çalışacağını varsaymak.

## Doğrulama şartları

EVAL-017; geçerli/geçersiz CloudEvent şemasını, retry kimliğini, local stdout
adapter'ını, 2xx/non-2xx/timeout davranışını, ingestion kapalı publisher'ı,
record+outbox atomikliğini, OpenAPI webhook drift'ini, institution Kustomize
render/dry-run'ını, secret'ın yalnız worker'a verilmesini, geniş ingress/egress
olmamasını, migration-before-rollout protokolünü ve Kind worker restart
senaryosunu doğrular. Rapor “local conformance passed” ile kurum endpoint,
identity ve OpenShift'in `not evaluated` durumunu ayrı gösterir.

[fastapi-webhooks]: https://fastapi.tiangolo.com/advanced/openapi-webhooks/
[ce-core]: https://github.com/cloudevents/spec/blob/v1.0.2/cloudevents/spec.md
[ce-json]: https://github.com/cloudevents/spec/blob/v1.0.2/cloudevents/formats/json-format.md
[ce-http]: https://github.com/cloudevents/spec/blob/v1.0.2/cloudevents/bindings/http-protocol-binding.md
[http-status]: https://www.rfc-editor.org/rfc/rfc9110.html#section-15.3
[k8s-network-policy]: https://v1-34.docs.kubernetes.io/docs/concepts/services-networking/network-policies/
[k8s-jobs]: https://v1-34.docs.kubernetes.io/docs/concepts/workloads/controllers/job/
[kubectl-wait]: https://v1-34.docs.kubernetes.io/docs/reference/kubectl/generated/kubectl_wait/
[kubectl-rollout]: https://v1-34.docs.kubernetes.io/docs/reference/kubectl/generated/kubectl_rollout/kubectl_rollout_status/
