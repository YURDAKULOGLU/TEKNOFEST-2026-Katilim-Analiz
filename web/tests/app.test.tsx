import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { App } from "../src/app/App";
import { ApiProblem, type AnalysisApi } from "../src/api/client";
import {
  abstainingAnswer,
  campaignDetail,
  campaignList,
  coverage,
  emptyNotificationPage,
  extractionPreview,
  incompatibleComparison,
} from "./fixtures";

function createClient(overrides: Partial<AnalysisApi> = {}): AnalysisApi {
  return {
    listCampaigns: vi.fn().mockResolvedValue(campaignList),
    getCampaign: vi.fn().mockResolvedValue(campaignDetail),
    getCoverage: vi.fn().mockResolvedValue(coverage),
    compareCampaigns: vi.fn().mockResolvedValue(incompatibleComparison),
    sendChatMessage: vi.fn().mockResolvedValue(abstainingAnswer),
    previewExtraction: vi.fn().mockResolvedValue(extractionPreview),
    listNotifications: vi.fn().mockResolvedValue(emptyNotificationPage),
    ...overrides,
  };
}

function renderApp(client: AnalysisApi): void {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <App client={client} />
    </QueryClientProvider>,
  );
}

describe("V1 dashboard", () => {
  it("shows campaigns, coverage freshness, and field evidence without fabricated bank branding", async () => {
    const client = createClient();
    renderApp(client);

    expect(screen.getByRole("status", { name: "Kampanyalar yükleniyor" })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Kampanyalar" })).toBeInTheDocument();
    expect(await screen.findByText("Test Konut Finansmanı")).toBeInTheDocument();
    expect(screen.getByText("2 banka gözlendi")).toBeInTheDocument();
    expect(screen.getByText("Kanıt bağlı aday kayıtlar")).toBeInTheDocument();
    expect(screen.getByText("Doğrulanmış kampanya kaydı")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Test Konut Finansmanı ayrıntısını aç" }));

    expect(await screen.findByRole("heading", { name: "Kaynak ve doğrulama" })).toBeInTheDocument();
    expect(screen.getByText("Aylık kâr oranı %2,49")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Resmî kaynak sayfasını aç" })).toHaveAttribute(
      "href",
      "https://example.test/kampanya-a",
    );
  });

  it("applies a bank filter through the typed list adapter", async () => {
    const client = createClient();
    renderApp(client);
    await screen.findByText("Test Konut Finansmanı");

    fireEvent.change(screen.getByRole("combobox", { name: "Banka" }), {
      target: { value: "test-bank-a" },
    });

    await waitFor(() => {
      expect(client.listCampaigns).toHaveBeenLastCalledWith(
        expect.objectContaining({ bank_id: "test-bank-a" }),
        undefined,
        undefined,
      );
    });
  });

  it("pins every page to its first snapshot and refreshes loaded pages in place after a notification", async () => {
    vi.useFakeTimers();
    try {
      const oldAsOf = "2026-07-18T12:15:00+03:00";
      const newAsOf = "2026-07-19T12:15:00+03:00";
      const oldFirstPage = {
        ...campaignList,
        items: [campaignList.items[0]!],
        next_cursor: "old-page-2",
        as_of: oldAsOf,
      };
      const oldSecondPage = {
        ...campaignList,
        items: [campaignList.items[1]!],
        next_cursor: null,
        as_of: oldAsOf,
      };
      const updatedCampaign = {
        ...campaignList.items[0]!,
        id: "campaign-a-v2",
        version: 2,
      };
      const newFirstPage = {
        ...campaignList,
        items: [updatedCampaign],
        next_cursor: "new-page-2",
        as_of: newAsOf,
      };
      const newSecondPage = {
        ...campaignList,
        items: [campaignList.items[1]!],
        next_cursor: null,
        as_of: newAsOf,
      };
      const notification = {
        id: "00000000-0000-0000-0000-000000000001",
        campaign_key: updatedCampaign.campaign_key,
        record_id: updatedCampaign.id,
        record_version: 2,
        change_kind: "updated" as const,
        record_status: "validated" as const,
        previous_record_id: campaignList.items[0]!.id,
        observed_at: "2026-07-19T12:00:00Z",
        created_at: "2026-07-19T12:00:00Z",
      };
      const listCampaigns = vi
        .fn<AnalysisApi["listCampaigns"]>()
        .mockResolvedValueOnce(oldFirstPage)
        .mockResolvedValueOnce(oldSecondPage)
        .mockResolvedValueOnce(newFirstPage)
        .mockResolvedValueOnce(newSecondPage);
      const listNotifications = vi
        .fn<AnalysisApi["listNotifications"]>()
        .mockResolvedValueOnce({ items: [], next_cursor: "cursor-0" })
        .mockResolvedValueOnce({ items: [notification], next_cursor: "cursor-1" })
        .mockResolvedValue({ items: [], next_cursor: "cursor-1" });

      renderApp(createClient({ listCampaigns, listNotifications }));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1);
      });

      fireEvent.click(screen.getByRole("button", { name: "Daha fazla kampanya yükle" }));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1);
      });

      expect(listCampaigns.mock.calls[0]).toEqual([{}, undefined, undefined]);
      expect(listCampaigns.mock.calls[1]).toEqual([{}, "old-page-2", oldAsOf]);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(15_000);
      });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1);
      });

      // The notification refreshes every already-loaded page in place instead
      // of rotating the query key and resetting the list to page one.
      expect(listCampaigns.mock.calls[2]).toEqual([{}, undefined, undefined]);
      expect(listCampaigns.mock.calls[3]).toEqual([{}, "new-page-2", newAsOf]);
      expect(listCampaigns).toHaveBeenCalledTimes(4);
    } finally {
      vi.useRealTimers();
    }
  });

  it("does not rank context-incompatible campaigns", async () => {
    const client = createClient();
    renderApp(client);
    await screen.findByText("Test Konut Finansmanı");

    const table = screen.getByRole("table", { name: "Kampanya listesi" });
    const checkboxes = within(table).getAllByRole("checkbox");
    fireEvent.click(checkboxes[0]!);
    fireEvent.click(checkboxes[1]!);
    expect(screen.getByText(/insan incelemesi tamamlanmamış kayıt var/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "2 kampanyayı karşılaştır" }));

    expect(await screen.findByText("Karşılaştırma yapılamadı")).toBeInTheDocument();
    expect(screen.getByText("Ürün aileleri farklı olduğu için oranlar sıralanmadı.")).toBeInTheDocument();
    expect(screen.queryByText(/en iyi banka/i)).not.toBeInTheDocument();
  });

  it("renders an explicit grounded-chat abstention", async () => {
    const client = createClient();
    renderApp(client);
    await screen.findByText("Test Konut Finansmanı");

    fireEvent.change(screen.getByLabelText("Sorunuz"), {
      target: { value: "Test kampanyasında bilinmeyen koşul nedir?" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Soruyu gönder" }));

    expect(await screen.findByText("Yeterli kanıt bulunamadı")).toBeInTheDocument();
    expect(
      screen.getByText("Bu soruyu yanıtlamak için doğrulanmış kaynak kanıtı bulunamadı."),
    ).toBeInTheDocument();
  });

  it("runs a review-only text extraction preview from the coverage bank list", async () => {
    const client = createClient();
    renderApp(client);
    await screen.findByText("Test Konut Finansmanı");

    fireEvent.change(screen.getByRole("combobox", { name: "Önizleme bankası" }), {
      target: { value: "test-bank-a" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "Kampanya veya ürün metni" }), {
      target: { value: "Test Metin Kampanyası\n12 ay vadeli." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Çıkarımı önizle" }));

    await waitFor(() => {
      expect(client.previewExtraction).toHaveBeenCalledWith({
        bank_id: "test-bank-a",
        text: "Test Metin Kampanyası\n12 ay vadeli.",
      });
    });
    expect(await screen.findByText("İnsan incelemesi gerekli")).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "Test Metin Kampanyası" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Bu çıktı insan tarafından doğrulanmadı ve kaydedilmedi/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Seçilen banka kullanıcı tarafından beyan edilen bir atıftır/),
    ).toBeInTheDocument();
    expect(screen.getByText("Model çağrılmadı")).toBeInTheDocument();

    const fullData = screen.getByRole("textbox", { name: "Tam normalize kampanya verisi" });
    expect(fullData).toHaveAttribute("readonly");
    const structured = JSON.parse((fullData as HTMLTextAreaElement).value);
    expect(Object.keys(structured)).toEqual([
      "bank_id",
      "title",
      "product_family",
      "campaign_type",
      "summary",
      "rates",
      "financing_amounts",
      "terms",
      "fees",
      "rewards",
      "validity",
      "customer_segments",
      "eligibility_conditions",
      "comparison_context",
    ]);
    expect(structured.rates[0]).toMatchObject({
      value_percent: "1.99",
      kind: "financing_profit_rate",
      period: "monthly",
      term_months: 12,
    });
    expect(structured.financing_amounts[0]).toMatchObject({
      amount: "100000",
      currency: "TRY",
    });
    expect(structured.terms[0]).toMatchObject({ maximum_months: 12 });
    expect(structured.fees[0]).toMatchObject({
      kind: "allocation",
      basis: "one_time",
      money: { amount: "250", currency: "TRY" },
    });
    expect(structured.rewards[0]).toMatchObject({
      kind: "points",
      basis: "campaign_total",
      points: "1000",
    });
    expect(structured.validity).toMatchObject({
      starts_on: "2026-07-01",
      ends_on: "2026-07-31",
    });
    expect(structured.customer_segments).toEqual(["Bireysel"]);
    expect(structured.eligibility_conditions).toEqual(["Mobil başvuru gereklidir."]);
    expect(structured.comparison_context).toEqual({
      product_currency: "TRY",
      customer_segment_keys: ["bireysel"],
      sales_channel: "mobile",
      new_customer_only: false,
      product_mechanism: "konut_finansmani",
      secured: true,
    });
  });

  it("shows a useful empty state instead of placeholder campaigns", async () => {
    const client = createClient({
      listCampaigns: vi.fn().mockResolvedValue({
        items: [],
        next_cursor: null,
        facets: {},
        as_of: "2026-07-18T12:00:00+03:00",
      }),
    });
    renderApp(client);

    expect(await screen.findByText("Bu filtrelerle kampanya bulunamadı")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Filtreleri temizle" })).not.toHaveLength(0);
  });

  it("exposes an RFC 9457 error and its safe correlation reference", async () => {
    const client = createClient({
      listCampaigns: vi.fn().mockRejectedValue(
        new ApiProblem({
          type: "https://example.test/problems/unavailable",
          title: "Hizmet kullanılamıyor",
          status: 503,
          detail: "Kampanya deposuna ulaşılamadı.",
          instance: null,
          code: "campaign_store_unavailable",
          correlation_id: "corr-ui-test-1",
          errors: [],
        }),
      ),
    });
    renderApp(client);

    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("Kampanya deposuna ulaşılamadı.")).toBeInTheDocument();
    expect(within(alert).getByText("İzleme kodu: corr-ui-test-1")).toBeInTheDocument();
    expect(within(alert).getByRole("button", { name: "Yeniden dene" })).toBeInTheDocument();
  });

  it("polls duplicate-safely and refreshes active logical campaign IDs", async () => {
    vi.useFakeTimers();
    try {
      const refreshedList = {
        ...campaignList,
        items: campaignList.items.map((campaign) =>
          campaign.campaign_key === "test-bank-a:campaign-a"
            ? { ...campaign, id: "campaign-a-v2", version: 2 }
            : campaign,
        ),
      };
      const notification = {
        id: "00000000-0000-0000-0000-000000000001",
        campaign_key: "test-bank-a:campaign-a",
        record_id: "campaign-a-v2",
        record_version: 2,
        change_kind: "updated" as const,
        record_status: "validated" as const,
        previous_record_id: "campaign-a",
        observed_at: "2026-07-19T12:00:00Z",
        created_at: "2026-07-19T12:00:00Z",
      };
      const listCampaigns = vi
        .fn<AnalysisApi["listCampaigns"]>()
        .mockResolvedValueOnce(campaignList)
        .mockResolvedValue(refreshedList);
      const listNotifications = vi
        .fn<AnalysisApi["listNotifications"]>()
        .mockResolvedValueOnce({ items: [], next_cursor: "cursor-0" })
        .mockResolvedValueOnce({ items: [notification], next_cursor: "cursor-1" })
        .mockResolvedValue({ items: [notification], next_cursor: "cursor-1" });
      const getCampaign = vi
        .fn<AnalysisApi["getCampaign"]>()
        .mockImplementation(async (id) => ({
          ...campaignDetail,
          campaign:
            refreshedList.items.find((campaign) => campaign.id === id) ??
            campaignList.items.find((campaign) => campaign.id === id) ??
            campaignDetail.campaign,
        }));
      const compareCampaigns = vi
        .fn<AnalysisApi["compareCampaigns"]>()
        .mockResolvedValue(incompatibleComparison);
      const client = createClient({
        listCampaigns,
        listNotifications,
        getCampaign,
        compareCampaigns,
      });
      renderApp(client);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(1);
      });
      fireEvent.click(screen.getByRole("button", { name: "Test Konut Finansmanı ayrıntısını aç" }));
      const table = screen.getByRole("table", { name: "Kampanya listesi" });
      const checkboxes = within(table).getAllByRole("checkbox");
      fireEvent.click(checkboxes[0]!);
      fireEvent.click(checkboxes[1]!);
      fireEvent.click(screen.getByRole("button", { name: "2 kampanyayı karşılaştır" }));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1);
      });

      await act(async () => {
        await vi.advanceTimersByTimeAsync(15_000);
      });

      expect(listNotifications).toHaveBeenLastCalledWith("cursor-0", 100);
      expect(getCampaign).toHaveBeenCalledWith("campaign-a-v2");
      expect(compareCampaigns).toHaveBeenCalledWith(
        expect.objectContaining({ campaign_ids: ["campaign-a-v2", "campaign-b"] }),
      );
      expect(screen.getByText("Test Konut Finansmanı güncellendi")).toBeInTheDocument();
      fireEvent.click(
        screen.getByRole("button", { name: "Bu oturumda görüldü olarak işaretle" }),
      );
      expect(
        screen.getByRole("button", { name: "Bu oturumda görüldü" }),
      ).toBeDisabled();

      const campaignCallsAfterRefresh = listCampaigns.mock.calls.length;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(15_000);
      });
      expect(screen.getAllByText("Test Konut Finansmanı güncellendi")).toHaveLength(1);
      expect(listCampaigns).toHaveBeenCalledTimes(campaignCallsAfterRefresh);
    } finally {
      vi.useRealTimers();
    }
  });
});
