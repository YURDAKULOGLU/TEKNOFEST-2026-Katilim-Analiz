import {
  keepPreviousData,
  useInfiniteQuery,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";

import type { AnalysisApi } from "../api/client";
import type {
  CampaignChangeEvent,
  CampaignFilters as FilterValues,
  CampaignSummary,
} from "../api/contracts";
import { AppShell } from "../components/AppShell";
import { ErrorState, LoadingState } from "../components/AsyncState";
import { CampaignDetailPanel } from "../components/CampaignDetailPanel";
import { CampaignFilters } from "../components/CampaignFilters";
import { CampaignTable } from "../components/CampaignTable";
import { ChatPanel } from "../components/ChatPanel";
import { ComparisonPanel } from "../components/ComparisonPanel";
import { CoverageSummary } from "../components/CoverageSummary";
import { ExtractionPreviewPanel } from "../components/ExtractionPreviewPanel";
import { NotificationFeed } from "../components/NotificationFeed";
import { formatDateTime } from "../utils/format";

interface AppProps {
  client: AnalysisApi;
}

interface CampaignPageParam {
  cursor?: string;
  asOf?: string;
}

function uniqueCampaigns(
  pages: ReadonlyArray<{ readonly items: readonly CampaignSummary[] }> | undefined,
): CampaignSummary[] {
  const indexed = new Map<string, CampaignSummary>();
  for (const page of pages ?? []) {
    for (const campaign of page.items) {
      indexed.set(campaign.campaign_key, campaign);
    }
  }
  return [...indexed.values()];
}

export function App({ client }: AppProps) {
  const queryClient = useQueryClient();
  const [filters, setFilters] = useState<FilterValues>({});
  const [selectedKeys, setSelectedKeys] = useState<Set<string>>(() => new Set());
  const [detailKey, setDetailKey] = useState<string | null>(null);
  const [notifications, setNotifications] = useState<CampaignChangeEvent[]>([]);
  const detailTriggerRef = useRef<HTMLButtonElement | null>(null);
  const notificationCursorRef = useRef<string | undefined>(undefined);
  const processedNotificationIdsRef = useRef<Set<string>>(new Set());

  const campaignQuery = useInfiniteQuery({
    queryKey: ["campaigns", filters],
    queryFn: ({ pageParam }) =>
      client.listCampaigns(filters, pageParam.cursor, pageParam.asOf),
    initialPageParam: {} as CampaignPageParam,
    // Keep the already-loaded list on screen while a refetch or filter change
    // is in flight; without this the table drops to a loading state and the
    // user's pagination visibly resets (issue #7).
    placeholderData: keepPreviousData,
    getNextPageParam: (lastPage, allPages) =>
      lastPage.next_cursor
        ? {
            cursor: lastPage.next_cursor,
            asOf: allPages[0]?.as_of ?? lastPage.as_of,
          }
        : undefined,
  });
  const coverageQuery = useQuery({
    queryKey: ["coverage"],
    queryFn: () => client.getCoverage(),
  });
  const notificationQuery = useQuery({
    queryKey: ["notifications"],
    queryFn: async () => {
      const page = await client.listNotifications(notificationCursorRef.current, 100);
      if (page.next_cursor) {
        notificationCursorRef.current = page.next_cursor;
      }
      return page;
    },
    refetchInterval: 15_000,
    refetchIntervalInBackground: false,
    staleTime: 10_000,
  });

  useEffect(() => {
    const page = notificationQuery.data;
    if (!page) {
      return;
    }
    const fresh = page.items.filter((item) => {
      if (processedNotificationIdsRef.current.has(item.id)) {
        return false;
      }
      processedNotificationIdsRef.current.add(item.id);
      return true;
    });
    if (fresh.length === 0) {
      return;
    }
    setNotifications((current) => {
      const byId = new Map(current.map((item) => [item.id, item]));
      for (const item of fresh) {
        byId.set(item.id, item);
      }
      return [...byId.values()]
        .sort(
          (left, right) =>
            right.created_at.localeCompare(left.created_at) || right.id.localeCompare(left.id),
        )
        .slice(0, 100);
    });
    // Refetch the loaded campaign pages in place instead of rotating the query
    // key; a key change would discard the pagination the user already opened.
    void queryClient.invalidateQueries({ queryKey: ["campaigns"], refetchType: "active" });
    void queryClient.invalidateQueries({
      queryKey: ["campaign-detail"],
      refetchType: "active",
    });
    void queryClient.invalidateQueries({ queryKey: ["comparison"], refetchType: "active" });
  }, [notificationQuery.data, queryClient]);

  const campaigns = useMemo(
    () => uniqueCampaigns(campaignQuery.data?.pages),
    [campaignQuery.data?.pages],
  );
  const campaignsByKey = useMemo(
    () => new Map(campaigns.map((campaign) => [campaign.campaign_key, campaign])),
    [campaigns],
  );
  const campaignsById = useMemo(
    () => new Map(campaigns.map((campaign) => [campaign.id, campaign])),
    [campaigns],
  );
  const selectedIds = useMemo(
    () =>
      [...selectedKeys]
        .map((campaignKey) => campaignsByKey.get(campaignKey)?.id)
        .filter((id): id is string => id !== undefined),
    [campaignsByKey, selectedKeys],
  );
  const detailId = detailKey ? campaignsByKey.get(detailKey)?.id ?? null : null;
  const currentDetailQuery = useQuery({
    queryKey: ["campaign-detail", detailId],
    queryFn: () => client.getCampaign(detailId ?? ""),
    enabled: detailId !== null,
  });
  const latestPage = campaignQuery.data?.pages.at(-1);
  const firstPage = campaignQuery.data?.pages[0];
  const asOf = firstPage?.as_of ? formatDateTime(firstPage.as_of) : null;

  function changeFilters(next: FilterValues) {
    setFilters(next);
    setSelectedKeys(new Set());
    setDetailKey(null);
  }

  function toggleSelection(id: string) {
    const campaignKey = campaignsById.get(id)?.campaign_key;
    if (!campaignKey) {
      return;
    }
    setSelectedKeys((current) => {
      const next = new Set(current);
      if (next.has(campaignKey)) {
        next.delete(campaignKey);
      } else if (next.size < 4) {
        next.add(campaignKey);
      }
      return next;
    });
  }

  function openDetail(id: string, trigger: HTMLButtonElement) {
    const campaignKey = campaignsById.get(id)?.campaign_key;
    if (!campaignKey) {
      return;
    }
    detailTriggerRef.current = trigger;
    setDetailKey(campaignKey);
  }

  function closeDetail() {
    setDetailKey(null);
    detailTriggerRef.current?.focus();
  }

  return (
    <AppShell asOf={asOf}>
      <section id="genel-gorunum" className="content-section" aria-labelledby="overview-heading">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Kapsam görünürlüğü</p>
            <h2 id="overview-heading">Genel görünüm</h2>
          </div>
          <p>Her banka başarılı, kısmi veya gerekçeli erişim durumu ile temsil edilir.</p>
        </div>
        <CoverageSummary
          entries={coverageQuery.data}
          error={coverageQuery.error}
          loading={coverageQuery.isPending}
          onRetry={() => void coverageQuery.refetch()}
        />
      </section>

      <NotificationFeed
        notifications={notifications}
        campaigns={campaigns}
        error={notificationQuery.error}
        loading={notificationQuery.isPending}
        onRetry={() => void notificationQuery.refetch()}
      />

      <section id="cikarim-onizleme" className="content-section" aria-labelledby="preview-heading">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Canlı metin denemesi</p>
            <h2 id="preview-heading">Çıkarım önizlemesi</h2>
          </div>
          <p>
            Seçili bankaya ait metni kurallar ve yapılandırılmış yerel model hattında deneyin.
            Sonuç yalnız önizlemedir ve insan incelemesi olmadan kalıcı veriye dönüşmez.
          </p>
        </div>
        <ExtractionPreviewPanel
          client={client}
          banks={coverageQuery.data}
          banksLoading={coverageQuery.isPending}
        />
      </section>

      <section id="kampanyalar" className="content-section" aria-labelledby="campaigns-heading">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Kanıt bağlı aday kayıtlar</p>
            <h2 id="campaigns-heading">Kampanyalar</h2>
          </div>
          <p>{campaigns.length} kayıt görüntüleniyor</p>
        </div>

        <CampaignFilters
          facets={latestPage?.facets ?? firstPage?.facets}
          filters={filters}
          onChange={changeFilters}
        />

        {campaignQuery.isPending ? <LoadingState label="Kampanyalar yükleniyor" /> : null}
        {campaignQuery.error ? (
          <ErrorState
            error={campaignQuery.error}
            onRetry={() => void campaignQuery.refetch()}
            title="Kampanyalar alınamadı"
          />
        ) : null}
        {!campaignQuery.isPending && !campaignQuery.error && campaigns.length === 0 ? (
          <div className="empty-state">
            <p className="eyebrow">Açık veri durumu</p>
            <h3>Bu filtrelerle kampanya bulunamadı</h3>
            <p>
              Filtreleri genişletin veya banka kapsam notlarını inceleyin. Sistem eksik kampanya
              yerine örnek kayıt üretmez.
            </p>
            <button className="button button--secondary" type="button" onClick={() => changeFilters({})}>
              Filtreleri temizle
            </button>
          </div>
        ) : null}
        {campaigns.length > 0 ? (
          <CampaignTable
            campaigns={campaigns}
            selectedIds={new Set(selectedIds)}
            onToggleSelection={toggleSelection}
            onOpenDetail={openDetail}
            canLoadMore={Boolean(campaignQuery.hasNextPage)}
            loadingMore={campaignQuery.isFetchingNextPage}
            onLoadMore={() => void campaignQuery.fetchNextPage()}
          />
        ) : null}
      </section>

      {detailId ? (
        <CampaignDetailPanel
          key={detailId}
          detail={currentDetailQuery.data}
          error={currentDetailQuery.error}
          loading={currentDetailQuery.isPending}
          onClose={closeDetail}
          onRetry={() => void currentDetailQuery.refetch()}
        />
      ) : null}

      <ComparisonPanel
        client={client}
        selectedIds={selectedIds}
        campaigns={campaigns}
        onClearSelection={() => setSelectedKeys(new Set())}
      />

      <ChatPanel client={client} />
    </AppShell>
  );
}
