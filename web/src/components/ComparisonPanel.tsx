import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import type { AnalysisApi } from "../api/client";
import type { CampaignSummary, ComparisonDimension } from "../api/contracts";
import { formatDateTime, shortHash } from "../utils/format";
import { comparisonDimensionLabel, reasonCodeLabel } from "../utils/labels";
import { ErrorState } from "./AsyncState";

const allDimensions: ComparisonDimension[] = ["rate", "term", "fee", "reward", "eligibility"];

interface ComparisonPanelProps {
  client: AnalysisApi;
  selectedIds: string[];
  campaigns: CampaignSummary[];
  onClearSelection: () => void;
}

export function ComparisonPanel({
  client,
  selectedIds,
  campaigns,
  onClearSelection,
}: ComparisonPanelProps) {
  const [dimensions, setDimensions] = useState<ComparisonDimension[]>(allDimensions);
  const [comparisonRequested, setComparisonRequested] = useState(false);
  const comparison = useQuery({
    queryKey: ["comparison", selectedIds, dimensions],
    queryFn: () => client.compareCampaigns({ campaign_ids: selectedIds, dimensions }),
    enabled: comparisonRequested && selectedIds.length >= 2 && dimensions.length > 0,
  });
  const selectedCampaigns = new Map(
    campaigns.filter((campaign) => selectedIds.includes(campaign.id)).map((campaign) => [campaign.id, campaign]),
  );
  const hasUnvalidatedSelection = [...selectedCampaigns.values()].some(
    (campaign) => campaign.status !== "validated",
  );
  const canCompare = selectedIds.length >= 2 && dimensions.length > 0 && !comparison.isFetching;

  function toggleDimension(dimension: ComparisonDimension) {
    setDimensions((current) =>
      current.includes(dimension)
        ? current.filter((item) => item !== dimension)
        : [...current, dimension],
    );
  }

  function runComparison() {
    if (comparisonRequested) {
      void comparison.refetch();
    } else {
      setComparisonRequested(true);
    }
  }

  function clearSelection() {
    setComparisonRequested(false);
    onClearSelection();
  }

  const allIncompatible =
    comparison.data?.items.length && comparison.data.items.every((item) => !item.comparable);

  return (
    <section id="karsilastirma" className="content-card comparison-section" aria-labelledby="compare-heading">
      <div className="section-heading">
        <div>
          <p className="eyebrow">Aynı türü aynı bazda değerlendirin</p>
          <h2 id="compare-heading">Karşılaştırma</h2>
          <p>
            En fazla dört kampanya seçin. Uyumsuz oran türleri, ürün aileleri veya müşteri
            bağlamları sıralanmaz.
          </p>
        </div>
        {selectedIds.length > 0 ? (
          <button className="text-button" type="button" onClick={clearSelection}>
            Seçimi temizle
          </button>
        ) : null}
      </div>

      <div className="comparison-controls">
        {hasUnvalidatedSelection ? (
          <p className="inline-warning" role="status">
            Seçimde insan incelemesi tamamlanmamış kayıt var. Motor bu kayıtları sıralamaz ve
            sonucu <strong>kayıt doğrulanmadı</strong> gerekçesiyle açıklar.
          </p>
        ) : null}
        <div className="selection-summary" aria-live="polite">
          <strong>{selectedIds.length} kampanya seçildi</strong>
          <span>
            {selectedIds.length < 2
              ? "Karşılaştırma için en az iki kayıt seçin."
              : selectedIds.map((id) => selectedCampaigns.get(id)?.title ?? id).join(" · ")}
          </span>
        </div>

        <fieldset className="dimension-picker">
          <legend>Karşılaştırma boyutları</legend>
          {allDimensions.map((dimension) => (
            <label key={dimension}>
              <input
                type="checkbox"
                checked={dimensions.includes(dimension)}
                onChange={() => toggleDimension(dimension)}
              />
              {comparisonDimensionLabel(dimension)}
            </label>
          ))}
        </fieldset>

        <button
          className="button button--primary"
          type="button"
          disabled={!canCompare}
          aria-label={
            selectedIds.length >= 2
              ? `${selectedIds.length} kampanyayı karşılaştır`
              : "Karşılaştırmak için en az 2 kampanya seçin"
          }
          onClick={runComparison}
        >
          {comparison.isFetching ? "Karşılaştırılıyor…" : "Seçilenleri karşılaştır"}
        </button>
      </div>

      {comparison.error ? (
        <ErrorState
          error={comparison.error}
          onRetry={() => void comparison.refetch()}
          title="Karşılaştırma tamamlanamadı"
        />
      ) : null}

      {comparison.data ? (
        <div className="comparison-result" aria-live="polite">
          <div className="result-heading">
            <div>
              <p className="eyebrow">Kural seti {comparison.data.ruleset_version}</p>
              <h3>{allIncompatible ? "Karşılaştırma yapılamadı" : "Karşılaştırma sonucu"}</h3>
            </div>
            <span>{formatDateTime(comparison.data.generated_at)}</span>
          </div>

          {comparison.data.warnings.length > 0 ? (
            <ul className="warning-list">
              {comparison.data.warnings.map((warning) => (
                <li key={warning}>{warning}</li>
              ))}
            </ul>
          ) : null}

          <div className="table-scroll" tabIndex={0} aria-label="Karşılaştırma tablosu, yatay kaydırılabilir">
            <table className="result-table">
              <caption className="visually-hidden">Boyutlara göre açıklanabilir karşılaştırma sonucu</caption>
              <thead>
                <tr>
                  <th scope="col">Kampanya</th>
                  <th scope="col">Boyut</th>
                  <th scope="col">Değer</th>
                  <th scope="col">Sonuç ve gerekçe</th>
                </tr>
              </thead>
              <tbody>
                {comparison.data.items.map((item, index) => (
                  <tr key={`${item.campaign_id}-${item.dimension}-${index}`}>
                    <th scope="row">{selectedCampaigns.get(item.campaign_id)?.title ?? item.campaign_id}</th>
                    <td>{comparisonDimensionLabel(item.dimension)}</td>
                    <td>{item.display_value}</td>
                    <td>
                      <span
                        className={`status-badge ${item.comparable ? "status-badge--success" : "status-badge--blocked"}`}
                      >
                        {item.comparable
                          ? item.rank
                            ? `${item.rank}. sıra`
                            : "Karşılaştırılabilir"
                          : "Sıralanmadı"}
                      </span>
                      <small>{reasonCodeLabel(item.reason_code)}</small>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="technical-reference" title={comparison.data.canonical_sha256}>
            Sonuç özeti: {shortHash(comparison.data.canonical_sha256)}
          </p>
        </div>
      ) : null}
    </section>
  );
}
