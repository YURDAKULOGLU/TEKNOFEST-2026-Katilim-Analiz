import type { ReactNode } from "react";

interface AppShellProps {
  children: ReactNode;
  asOf: string | null;
}

const navigation = [
  { href: "#genel-gorunum", label: "Genel görünüm" },
  { href: "#kampanyalar", label: "Kampanyalar" },
  { href: "#karsilastirma", label: "Karşılaştırma" },
  { href: "#asistan", label: "Kaynaklı asistan" },
];

export function AppShell({ children, asOf }: AppShellProps) {
  return (
    <div className="app-shell">
      <a className="skip-link" href="#ana-icerik">
        Ana içeriğe geç
      </a>
      <header className="site-header">
        <div className="brand-block">
          <span className="brand-mark" aria-hidden="true">
            KA
          </span>
          <div>
            <p className="eyebrow">Kanıt temelli analiz</p>
            <p className="brand-name">Katılım Analiz</p>
          </div>
        </div>
        <nav aria-label="Ana bölümler">
          <ul className="main-navigation">
            {navigation.map((item) => (
              <li key={item.href}>
                <a href={item.href}>{item.label}</a>
              </li>
            ))}
          </ul>
        </nav>
        <div className="runtime-state" aria-label="Çalışma durumu">
          <span className="runtime-dot" aria-hidden="true" />
          <span>Yerel çalışma</span>
        </div>
      </header>

      <main id="ana-icerik" className="main-content" tabIndex={-1}>
        <section className="hero" aria-labelledby="page-title">
          <div>
            <p className="eyebrow">Resmî web kaynakları · V1</p>
            <h1 id="page-title">Kampanyaları aynı bağlamda karşılaştırın</h1>
            <p className="hero-copy">
              Katılım bankalarının kamuya açık kampanya bilgilerini; güncellik, koşul ve kaynak
              kanıtlarıyla birlikte inceleyin. Sonuçlar finansal tavsiye değildir.
            </p>
          </div>
          <div className="freshness-card">
            <span>Veri görünümü</span>
            <strong>{asOf ?? "Henüz yüklenmedi"}</strong>
            <small>Kaynak gözlem zamanına göre</small>
          </div>
        </section>

        {children}
      </main>

      <footer className="site-footer">
        <p>
          Katılım Analiz, banka beyanlarını karşılaştırmalı sunar; uygunluk kararı veya finansal
          tavsiye üretmez.
        </p>
        <a href="#page-title">Sayfa başına dön</a>
      </footer>
    </div>
  );
}
