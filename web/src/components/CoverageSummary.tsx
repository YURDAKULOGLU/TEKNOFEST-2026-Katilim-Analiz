import type { CoverageResponse } from "../api/contracts";
import { coverageStatusLabel } from "../utils/labels";
import { formatDateTime } from "../utils/format";
import { ErrorState, LoadingState } from "./AsyncState";

interface CoverageSummaryProps {
  entries: CoverageResponse | undefined;
  error: unknown;
  loading: boolean;
  onRetry: () => void;
}

export function CoverageSummary({ entries, error, loading, onRetry }: CoverageSummaryProps) {
  if (loading) {
    return <LoadingState label="Banka kapsamı yükleniyor" />;
  }
  if (error) {
    return <ErrorState error={error} onRetry={onRetry} title="Kapsam bilgisi alınamadı" />;
  }

  const coverage = entries ?? [];
  const successful = coverage.filter((entry) => entry.status === "success").length;
  const campaignCount = coverage.reduce((total, entry) => total + entry.campaign_count, 0);
  const incomplete = coverage.filter((entry) => entry.status !== "success");

  return (
    <div className="coverage-layout">
      <dl className="metric-grid">
        <div className="metric-card">
          <dt>Kapsamdaki banka</dt>
          <dd>
            <strong>{coverage.length}</strong>
            <span>{coverage.length} banka gözlendi</span>
          </dd>
        </div>
        <div className="metric-card">
          <dt>Başarılı kaynak taraması</dt>
          <dd>
            <strong>
              {successful}/{coverage.length || 0}
            </strong>
            <span>Durum açıkça raporlanır</span>
          </dd>
        </div>
        <div className="metric-card">
          <dt>Doğrulanmış kampanya kaydı</dt>
          <dd>
            <strong>{campaignCount}</strong>
            <span>İnsan incelemesi tamamlanan güncel kayıtlar</span>
          </dd>
        </div>
      </dl>

      {incomplete.length > 0 ? (
        <details className="coverage-details">
          <summary>{incomplete.length} banka için kapsam notunu görüntüle</summary>
          <ul>
            {incomplete.map((entry) => (
              <li key={entry.bank_id}>
                <strong>{entry.bank_name}</strong>
                <span className={`status-badge status-badge--${entry.status}`}>
                  {coverageStatusLabel(entry.status)}
                </span>
                <span>{entry.reason ?? "Açıklama sağlanmadı."}</span>
                <small>Gözlem: {formatDateTime(entry.observed_at)}</small>
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </div>
  );
}
