import type { CampaignSummary } from "../api/contracts";
import { formatDate, formatDateTime } from "../utils/format";
import {
  campaignTypeLabel,
  productFamilyLabel,
  recordStatusLabel,
  salesChannelLabel,
} from "../utils/labels";

interface CampaignTableProps {
  campaigns: CampaignSummary[];
  selectedIds: Set<string>;
  onToggleSelection: (id: string) => void;
  onOpenDetail: (id: string, trigger: HTMLButtonElement) => void;
  canLoadMore: boolean;
  loadingMore: boolean;
  onLoadMore: () => void;
}

function validityLabel(campaign: CampaignSummary): string {
  const validity = campaign.validity;
  if (!validity) {
    return "Tarih belirtilmemiş";
  }
  if (validity.starts_on && validity.ends_on) {
    return `${formatDate(validity.starts_on)} – ${formatDate(validity.ends_on)}`;
  }
  if (validity.ends_on) {
    return `${formatDate(validity.ends_on)} tarihine kadar`;
  }
  return validity.raw;
}

export function CampaignTable({
  campaigns,
  selectedIds,
  onToggleSelection,
  onOpenDetail,
  canLoadMore,
  loadingMore,
  onLoadMore,
}: CampaignTableProps) {
  return (
    <>
      <div className="table-scroll" tabIndex={0} aria-label="Kampanya tablosu, yatay kaydırılabilir">
        <table className="campaign-table" aria-label="Kampanya listesi">
          <caption className="visually-hidden">
            Filtrelenen kampanyalar ve karşılaştırma seçimleri
          </caption>
          <thead>
            <tr>
              <th scope="col">Seç</th>
              <th scope="col">Banka ve kampanya</th>
              <th scope="col">Tür</th>
              <th scope="col">Temel değer</th>
              <th scope="col">Bağlam</th>
              <th scope="col">Geçerlilik</th>
              <th scope="col">Doğrulama</th>
            </tr>
          </thead>
          <tbody>
            {campaigns.map((campaign) => {
              const selected = selectedIds.has(campaign.id);
              const selectionLimitReached = selectedIds.size >= 4 && !selected;
              return (
                <tr key={campaign.id} className={selected ? "campaign-row--selected" : undefined}>
                  <td>
                    <input
                      type="checkbox"
                      checked={selected}
                      disabled={selectionLimitReached}
                      aria-label={`${campaign.title} karşılaştırma seçimine ekle`}
                      onChange={() => onToggleSelection(campaign.id)}
                    />
                  </td>
                  <th scope="row">
                    <span className="bank-name">{campaign.bank_name}</span>
                    <button
                      className="campaign-title-button"
                      type="button"
                      aria-label={`${campaign.title} ayrıntısını aç`}
                      onClick={(event) => onOpenDetail(campaign.id, event.currentTarget)}
                    >
                      {campaign.title}
                    </button>
                    {campaign.summary ? <span className="campaign-summary">{campaign.summary}</span> : null}
                  </th>
                  <td>
                    <span>{productFamilyLabel(campaign.product_family)}</span>
                    <small>{campaignTypeLabel(campaign.campaign_type)}</small>
                  </td>
                  <td>
                    {campaign.primary_value ? (
                      <>
                        <strong className="primary-value">{campaign.primary_value.value}</strong>
                        <small>{campaign.primary_value.label}</small>
                      </>
                    ) : (
                      <span className="unknown-value">Belirtilmemiş</span>
                    )}
                  </td>
                  <td>
                    <span>{campaign.currency ?? "Para birimi belirsiz"}</span>
                    <small>{salesChannelLabel(campaign.sales_channel)}</small>
                    <small>{campaign.customer_segments.join(", ") || "Segment belirtilmemiş"}</small>
                  </td>
                  <td>
                    <span>{validityLabel(campaign)}</span>
                    <small>Gözlem: {formatDateTime(campaign.observed_at)}</small>
                  </td>
                  <td>
                    <span className={`status-badge status-badge--${campaign.status}`}>
                      {recordStatusLabel(campaign.status)}
                    </span>
                    <small>{campaign.evidence_count} alan kanıtı</small>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {canLoadMore ? (
        <div className="load-more">
          <button
            className="button button--secondary"
            type="button"
            disabled={loadingMore}
            onClick={onLoadMore}
          >
            {loadingMore ? "Daha fazla kayıt yükleniyor…" : "Daha fazla kampanya yükle"}
          </button>
        </div>
      ) : null}
    </>
  );
}
