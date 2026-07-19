# Kurum overlay'i ve entegrasyon sözleşmesi

Kurum profili uygulamayı yapay biçimde mikroservislere bölmez. Aynı immutable
OCI imajı API ve worker rolleri olarak ayrı Deployment'larda çalışır; modüler
monolit sınırları korunur. PostgreSQL ve model servisi kurumun yönetilen
karşılıklarına bağlanabilir.

## Zorunlu dış sözleşmeler

| Bağımlılık | Beklenen sözleşme |
|---|---|
| PostgreSQL | PostgreSQL 17 uyumlu endpoint; TLS ve credential kurum Secret'ında; Alembic migration Job'ı aynı release imajından çalışır. |
| Model | Ollama `/api/chat` uyumlu, private `http://approved-model-service.ai-infrastructure.svc.cluster.local:11434`; model kimliği config'ten gelir. |
| Secret | `katilim-analiz-runtime` Secret'ında en az `database-url`; örnek dosya uygulanmaz. |
| Giriş trafiği | Kurum Ingress/Gateway katmanı API Service'e 8000/TCP yönlendirir; TLS sınırı kurum politikasındadır. |
| Gözlemlenebilirlik | Yapılandırılmış stdout logları; request/job/run correlation kimlikleri; ayrı live/ready uçları. |

Overlay çıktısı:

```powershell
kubectl kustomize deploy/k8s/overlays/institution
kubectl apply --dry-run=server -k deploy/k8s/overlays/institution
```

Gerçek secret, kurum secret manager/GitOps çözümüyle ayrıca sağlanır:
`deploy/k8s/overlays/institution/runtime-secret.example.yaml` yalnız alan
sözleşmesini gösterir.

## NetworkPolicy label sözleşmesi

Base namespace `default-deny` ile başlar. Kurum overlay'i şu dar cross-namespace
akışları ekler:

- `database-infrastructure` namespace'indeki
  `app.kubernetes.io/name=approved-postgres` podlarına 5432/TCP;
- `ai-infrastructure` namespace'indeki
  `app.kubernetes.io/name=approved-model-service` podlarına 11434/TCP.

Namespace isimleri Kubernetes'in otomatik
`kubernetes.io/metadata.name` label'ıyla eşleştirilir. Servis seçicilerinin
arkasındaki podlar yukarıdaki `app.kubernetes.io/name` label'ını taşımalıdır.

PostgreSQL küme dışı yönetilen bir endpoint ise standart Kubernetes
`NetworkPolicy` FQDN kuralı tanımlamaz. Kurum şu ikisinden birini açıkça sağlar:

1. sabit onaylı CIDR için overlay'e `ipBlock` patch'i;
2. Cilium/Calico/kurum CNI'sinin denetlenmiş FQDN egress policy'si.

Bu patch olmadan geniş `0.0.0.0/0` egress açılmaz ve "çalışıyor" diye iddia
edilmez.

## Ölçekleme ve migration sırası

- API iki replica ile başlar; worker başlangıçta tek replica'dır.
- Worker işi PostgreSQL lease/token ile alır; replica sayısı ancak lease yarış
  testleri geçtikten sonra artırılır.
- Migration Job tamamlanmadan yeni API/worker rollout'u hazır sayılmaz.
- API için PodDisruptionBudget vardır; tek-node yerel profile uygulanmaz.
- PostgreSQL/model servisinin HA, backup ve disaster-recovery politikası kurumun
  yönetilen servis sorumluluğudur; uygulama bunları varmış gibi simüle etmez.

## AD, SMTP ve OpenShift sınırı

AD/LDAP, SMTP ve OpenShift V1 yarışma şartı değildir. Entegrasyon portları
ilerleyen sürümlerde aynı sözleşmeye eklenecektir:

- yerel oturum güvenliği V1.2'de;
- kurumsal identity ve event/notification adaptörleri V1.3'te;
- OpenShift için restricted-v2/SCC uyumu, rastgele UID ve Route/Gateway patch'i
  kurum overlay'inde.

Bu servisler erişilebilir değilken sahte AD/SMTP sunucusu üretimmiş gibi
gösterilmez. Yerel adaptörler yalnız demo/test profili olarak açıkça etiketlenir.
