import { useMemo, useState } from "react";

import type { CampaignChangeEvent, CampaignSummary } from "../api/contracts";
import { formatDateTime } from "../utils/format";
import { recordStatusLabel } from "../utils/labels";
import { ErrorState, LoadingState } from "./AsyncState";

interface NotificationFeedProps {
  notifications: readonly CampaignChangeEvent[];
  campaigns: readonly CampaignSummary[];
  error: unknown;
  loading: boolean;
  onRetry: () => void;
}
export function NotificationFeed({
  notifications,
  campaigns,
  error,
  loading,
  onRetry,
}: NotificationFeedProps) {
  const [seenIds, setSeenIds] = useState<Set<string>>(() => new Set());
  const campaignsByKey = useMemo(
    () => new Map(campaigns.map((campaign) => [campaign.campaign_key, campaign])),
    [campaigns],
  );
  const unseenCount = notifications.filter((item) => !seenIds.has(item.id)).length;

  function markSeen(id: string) {
    setSeenIds((current) => new Set(current).add(id));
  }

  return (
    <section className="content-card" aria-labelledby="notifications-heading">
      <div className="section-heading section-heading--compact">
        <div>
          <p className="eyebrow">Salt okunur değişiklik akışı</p>
          <h2 id="notifications-heading">Bildirimler</h2>
        </div>
        <p aria-live="polite">{unseenCount} yeni bildirim</p>
      </div>

      {loading && notifications.length === 0 ? (
        <LoadingState label="Bildirimler yükleniyor" />
      ) : null}
      {error ? (
        <ErrorState error={error} onRetry={onRetry} title="Bildirimler alınamadı" />
      ) : null}
      {!loading && !error && notifications.length === 0 ? (
        <p>Henüz kampanya değişikliği bildirilmedi.</p>
      ) : null}

      {notifications.length > 0 ? (
        <ol className="warning-list" aria-label="Kampanya değişiklik bildirimleri">
          {notifications.map((notification) => {
            const campaign = campaignsByKey.get(notification.campaign_key);
            const seen = seenIds.has(notification.id);
            return (
              <li key={notification.id}>
                <div>
                  <strong>
                    {campaign?.title ?? "Kampanya kaydı"} {notification.change_kind === "created" ? "eklendi" : "güncellendi"}
                  </strong>
                  <span className={`status-badge status-badge--${notification.record_status}`}>
                    {recordStatusLabel(notification.record_status)}
                  </span>
                </div>
                <small>
                  Sürüm {notification.record_version} · {formatDateTime(notification.observed_at)}
                </small>
                <button
                  className="text-button"
                  type="button"
                  disabled={seen}
                  onClick={() => markSeen(notification.id)}
                >
                  {seen ? "Bu oturumda görüldü" : "Bu oturumda görüldü olarak işaretle"}
                </button>
              </li>
            );
          })}
        </ol>
      ) : null}
    </section>
  );
}
