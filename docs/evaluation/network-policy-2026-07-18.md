# NetworkPolicy enforcement kanıtı — 2026-07-18

Ortam:

- kind 0.32.0
- Kubernetes server 1.34.8
- kubectl 1.34.1 / Kustomize 5.7.1
- kindnetd `v20260528-9350166c`
- namespace `katilim-analiz`

Kubernetes dokümanı, NetworkPolicy kaynağının destekleyen bir network plugin
olmadan etkisiz olduğunu açıkça belirtir. Ayrıca pod-to-pod bağlantıda kaynak
egress ve hedef ingress tarafının ikisinin de izin vermesi gerekir:

- [Kubernetes 1.34 — Network Policies](https://v1-34.docs.kubernetes.io/docs/concepts/services-networking/network-policies/)
- [kind v0.24.0 — yerleşik NetworkPolicy desteğinin eklendiği resmî release](https://github.com/kubernetes-sigs/kind/releases/tag/v0.24.0)

Bu nedenle yalnız `kubectl apply` sonucu kanıt sayılmadı. Çalışan API podundan
TCP bağlantı probu uygulandı:

| Hedef | Beklenen | Gözlenen |
|---|---:|---:|
| `1.1.1.1:443` | engelli | `TimeoutError`, 4.00 saniye |
| `postgres:5432` | izinli | bağlantı başarılı |
| `ollama:11434` | izinli | bağlantı başarılı |

Sonuç: mevcut Kind/CNI profilinde default-deny ve gerekli iç servis izinleri
fiilen enforcement edildi. Kurum/OpenShift ortamı farklı CNI kullandığından aynı
negatif ve pozitif problar o ortamın release kapısında yeniden çalıştırılır.
