import type { CampaignDetail } from "../api/contracts";
import { useEffect, useRef } from "react";
import { formatDateTime, shortHash } from "../utils/format";
import { evidenceStatusLabel, recordStatusLabel } from "../utils/labels";
import { ErrorState, LoadingState } from "./AsyncState";

interface CampaignDetailPanelProps {
  detail: CampaignDetail | undefined;
  error: unknown;
  loading: boolean;
  onClose: () => void;
  onRetry: () => void;
}

export function CampaignDetailPanel({
  detail,
  error,
  loading,
  onClose,
  onRetry,
}: CampaignDetailPanelProps) {
  const panelRef = useRef<HTMLElement>(null);

  useEffect(() => {
    panelRef.current?.focus();
  }, []);

  return (
    <aside ref={panelRef} className="detail-panel" aria-label="Kampanya ayrıntısı" tabIndex={-1}>
      <div className="panel-heading">
        <div>
          <p className="eyebrow">Kayıt inceleme</p>
          <h2>{detail?.campaign.title ?? "Kampanya ayrıntısı"}</h2>
        </div>
        <button className="text-button" type="button" onClick={onClose}>
          Ayrıntıyı kapat
        </button>
      </div>

      {loading ? <LoadingState label="Kampanya ayrıntısı yükleniyor" /> : null}
      {error ? <ErrorState error={error} onRetry={onRetry} title="Ayrıntı alınamadı" /> : null}
      {detail ? (
        <div className="detail-content">
          <div className="detail-summary">
            <div>
              <span>Banka</span>
              <strong>{detail.campaign.bank_name}</strong>
            </div>
            <div>
              <span>Kayıt durumu</span>
              <strong>{recordStatusLabel(detail.campaign.status)}</strong>
            </div>
            <div>
              <span>Son gözlem</span>
              <strong>{formatDateTime(detail.campaign.observed_at)}</strong>
            </div>
            <div>
              <span>Sürüm</span>
              <strong>v{detail.campaign.version}</strong>
            </div>
          </div>

          <section aria-labelledby="source-heading">
            <div className="section-heading section-heading--compact">
              <div>
                <p className="eyebrow">İzlenebilirlik</p>
                <h3 id="source-heading">Kaynak ve doğrulama</h3>
              </div>
              <a
                className="button button--secondary"
                href={detail.source_url}
                target="_blank"
                rel="noreferrer noopener"
              >
                Resmî kaynak sayfasını aç
              </a>
            </div>
            <dl className="provenance-list">
              <div>
                <dt>Kaynak başlığı</dt>
                <dd>{detail.source_title ?? "Başlık belirtilmemiş"}</dd>
              </div>
              <div>
                <dt>Belge kimliği</dt>
                <dd>{detail.source_document_id}</dd>
              </div>
              <div>
                <dt>Kayıt özeti</dt>
                <dd title={detail.record_sha256}>{shortHash(detail.record_sha256)}</dd>
              </div>
              <div>
                <dt>Çıkarım yöntemi</dt>
                <dd>
                  {detail.extraction.method} · şema {detail.extraction.schema_version}
                </dd>
              </div>
            </dl>
          </section>

          <section aria-labelledby="evidence-heading">
            <h3 id="evidence-heading">Alan kanıtları</h3>
            {detail.evidence.length > 0 ? (
              <ol className="evidence-list">
                {detail.evidence.map((evidence) => (
                  <li key={evidence.id}>
                    <div>
                      <code>{evidence.field_pointer}</code>
                      <span className={`status-badge status-badge--${evidence.status}`}>
                        {evidenceStatusLabel(evidence.status)}
                      </span>
                    </div>
                    <blockquote>{evidence.quote}</blockquote>
                    <small>Kaynak bloğu: {evidence.block_id}</small>
                  </li>
                ))}
              </ol>
            ) : (
              <p className="inline-warning">Bu kayıt için görüntülenebilir alan kanıtı yok.</p>
            )}
          </section>

          {detail.validation_issues.length > 0 ? (
            <section aria-labelledby="issues-heading">
              <h3 id="issues-heading">Doğrulama notları</h3>
              <ul className="warning-list">
                {detail.validation_issues.map((issue) => (
                  <li key={issue}>{issue}</li>
                ))}
              </ul>
            </section>
          ) : null}
        </div>
      ) : null}
    </aside>
  );
}
